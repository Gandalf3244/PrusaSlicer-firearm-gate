"""End-to-end detection: STL -> list of reference parts whose fingerprint is
present in the mesh (multi-label), each with an auditable residual table.

    python -m pipeline.detect <stl path> [--loose]

Ordering is dimension-first (report: "Implementation note"): every mined
fingerprint is first filtered by element kind + diameter against the target's
primitive list, then verified as a rigid constellation. A fused mesh that
contains several reference parts returns several labels.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import trimesh

import numpy as np

from .anisotropy import axis_scales
from .fingerprint import PLUG_OVERSIZE, Fingerprint, elements, match
from .signature import global_features, primitive_features
from .primitives import segment
from .tolerances import LOOSE, TIGHT
from .units import infer_scale, load_mesh

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def load_fingerprints(path: Path = DATA / "fingerprints.json") -> list[Fingerprint]:
    fps = []
    return fingerprints_from(json.load(path.open()))


def fingerprints_from(results: list[dict]) -> list[Fingerprint]:
    """One Fingerprint per mined constellation. A part may carry several
    (primary + `alternates`, on disjoint element sets); they share `source`
    and are counted together by detect()."""
    fps = []
    for r in results:
        if r["status"] not in ("ok", "corroborate"):
            continue
        variants = [r] + r.get("alternates", [])
        for i, v in enumerate(variants):
            name = r["path"].split("/")[-1] + (f"#{i}" if i else "")
            fp = Fingerprint(name, r["path"], v["holes"])
            fp.relations = {tuple(int(x) for x in k.split("-")): tuple(val) for k, val in v["relations"].items()}
            fp.contained_in = v.get("contained_in", [])   # library parts known to include this one
            fp.shared_with = v.get("shared_with", [])     # ... or the same kind of part in the same family
            fp.variant, fp.n_variants = i, len(variants)
            fp.min_fired = 2 if r["status"] == "corroborate" else 1   # weak parts need two independent hits
            fp.tight_only = bool(v.get("tight_only"))       # slab-only (drill-proof) fingerprints: design level only
            fps.append(fp)
    return fps


def signature_of_file(stl: Path) -> dict:
    raw = trimesh.load(stl, force="mesh", process=True)
    scale = infer_scale(raw)
    return signature_of_mesh(load_mesh(stl), str(stl), scale)


def signature_of_mesh(mesh: trimesh.Trimesh, name: str, scale: float = 1.0, describe: bool = True) -> dict:
    """Signature of a mesh already prepared by units.prepare_mesh (mm, repaired).
    `describe=False` skips the oriented bounding box, which is reported but
    never used in a decision (the checker's hot path)."""
    seg = segment(mesh)
    amb = bool(mesh.metadata.get("concavity_ambiguous", False))
    sig = {"path": name, "scale": scale, "noise": round(seg.noise, 5), "concavity_ambiguous": amb,
           "stretch": axis_scales(mesh, seg)}
    sig.update(global_features(mesh, describe))
    sig.update(primitive_features(seg, amb))
    return sig


SCALED_ALONE = 3   # elements a single fingerprint needs to identify a part at a free scale


def detect(sig: dict, fps: list[Fingerprint], loose: bool = False,
           scale_range: tuple[float, float] | None = None) -> list[dict]:
    """Labels for one signature. Every fingerprint is matched; hits are
    grouped per reference part and reported with `fired` = how many of that
    part's independent fingerprints were found (k of n): a fused input that
    kept only one region of a part still gets the label, at 1/n confidence.

    `scale_range` = (lo, hi): match the references at any uniform scale in
    that range (a part resized in the slicer). A free scale makes a
    two-element constellation much easier to hit by chance (one ratio and an
    angle are left), so a part then needs a fingerprint of >= SCALED_ALONE
    elements, or two of its fingerprints at one common scale."""
    band, name = (LOOSE, "LOOSE") if loose else (TIGHT, "TIGHT")
    lo_s, hi_s = scale_range or (1.0, 1.0)
    per_part: dict[str, dict] = {}
    # a fingerprint can only match if each of its elements has a target element
    # of an acceptable kind within the diameter band (fingerprint.same_kind):
    # the same test match() starts with, on a sorted index instead of every pair
    by_kind: dict[str, np.ndarray] = {}
    for e in elements(sig, 0.9):
        by_kind.setdefault("plug" if e.get("plug") else e.get("kind", "hole"), []).append(e["d"])
    by_kind = {k: np.sort(np.asarray(v)) for k, v in by_kind.items()}
    tol = band.diameter + 1e-9            # never tighter than same_kind's test
    scaled_hits: dict[str, list] = {}     # part -> [(elements, [scale intervals])] per fired fingerprint

    def possible(fh: dict) -> bool:
        k = fh.get("kind", "hole")
        kinds = (k, "boss" if k == "hole" else "hole") if fh.get("amb") and k in ("hole", "boss") else (k,)
        for kk in kinds:
            arr = by_kind.get(kk)
            if arr is not None:
                i = int(np.searchsorted(arr, lo_s * fh["d"] - tol))
                if i < len(arr) and arr[i] <= hi_s * fh["d"] + tol:
                    return True
        arr = by_kind.get("plug") if k == "hole" and scale_range is None else None     # plug stand-ins: d .. d + PLUG_OVERSIZE
        if arr is not None:
            i = int(np.searchsorted(arr, fh["d"] - tol))
            if i < len(arr) and arr[i] <= fh["d"] + PLUG_OVERSIZE + tol:
                return True
        return False

    for fp in fps:
        if (loose or scale_range is not None) and getattr(fp, "tight_only", False):
            continue
        if not all(possible(fh) for fh in fp.holes):
            continue
        ms = match(fp, sig, band, name, scale_range=scale_range)
        if not ms:
            continue
        best = min(ms, key=lambda m: m.worst)
        scaled_hits.setdefault(fp.source, []).append((len(fp.holes), [m.scale for m in ms]))
        n_var = getattr(fp, "n_variants", 1)
        h = per_part.setdefault(fp.source, {
            "reference": fp.source, "n_elements": 0, "fired": 0, "n_fingerprints": n_var,
            "min_fired": getattr(fp, "min_fired", 1), "tier": "corroborate" if getattr(fp, "min_fired", 1) > 1 else "strong",
            "worst_residual_frac": 9.9, "residuals": {}, "band": name, "contained_in": set(), "shared_with": set()})
        h["fired"] += 1
        h.setdefault("variants", []).append(getattr(fp, "variant", 0))
        h["contained_in"] |= set(getattr(fp, "contained_in", []))
        h["shared_with"] |= set(getattr(fp, "shared_with", []))
        h.setdefault("matched_d", []).append([elements(sig, 0.9)[k]["d"] for k in best.assignment])
        if (len(fp.holes), -best.worst) > (h["n_elements"], -h["worst_residual_frac"]):
            h["n_elements"], h["worst_residual_frac"], h["residuals"] = len(fp.holes), round(best.worst, 3), best.residuals
    out = []
    for h in per_part.values():
        if scale_range is not None:
            h["fired"], h["scale"] = _common_scale(scaled_hits[h["reference"]])
            alone = any(n >= SCALED_ALONE for n, _ in scaled_hits[h["reference"]])
            if h["fired"] < max(h["min_fired"], 1 if alone else 2):
                continue
        if h["fired"] < h["min_fired"]:
            continue
        h["contained_in"] = sorted(h["contained_in"])
        h["shared_with"] = sorted(h["shared_with"])
        h["confidence"] = round(h["fired"] / h["n_fingerprints"], 2)
        out.append(h)
    return sorted(out, key=lambda r: (-r["confidence"], -r["n_elements"], r["worst_residual_frac"]))


def _common_scale(hits: list[tuple[int, list[tuple[float, float]]]]) -> tuple[int, float]:
    """Most fingerprints of one part that fit at one common scale, and that
    scale. Each fingerprint may fit at several (one interval per match)."""
    best, s_best = 0, 1.0
    for s in sorted({iv[0] for _, ivs in hits for iv in ivs}):
        n = sum(any(iv[0] <= s <= iv[1] for iv in ivs) for _, ivs in hits)
        if n > best:
            best, s_best = n, s
    return best, round(s_best, 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stl")
    ap.add_argument("--loose", action="store_true")
    ap.add_argument("--audit", action="store_true", help="print per-constraint residuals")
    a = ap.parse_args()
    from .families import classify_family, load_family_patterns
    fps = load_fingerprints()
    sig = signature_of_file(Path(a.stl))
    fam_hits = classify_family(sig, load_family_patterns())
    from .calibers import bore_evidence
    hits = detect(sig, fps, a.loose)
    if not a.loose:
        # design-level labels first; anything more that appears at LOOSE is
        # lineage / platform level and is listed as such
        seen = {h["reference"] for h in hits}
        extra = [h for h in detect(sig, fps, True) if h["reference"] not in seen]
    else:
        extra = []
    print(f"  platform evidence (family patterns present): {fam_hits or 'none'}")
    bores = bore_evidence(sig, load_mesh(a.stl))
    if bores:
        print("  bore evidence: " + "; ".join(f"{b['caliber']} (Ø{b['d']:.2f} x {b['L']:.0f} mm)" for b in bores))
    print(f"{a.stl}\n  scale x{sig['scale']}  obb {sig['obb']} mm  "
          f"{sig['counts']['holes']} holes / {sig['counts']['bosses']} bosses / {sig['counts']['cones']} cones")
    n_parts = len({fp.source for fp in fps})
    print(f"  noise {sig.get('noise', 0):.4f} mm; {len(fps)} fingerprints of {n_parts} parts checked, "
          f"{len(hits)} parts matched ({hits[0]['band'] if hits else ''}):")
    for h in hits:
        n_comp = len(set(h["contained_in"]) - set(h["shared_with"]))
        tag = (f"  (also present in {n_comp} composite library parts)" if n_comp else "") + \
              (f"  (shared with {len(h['shared_with'])} same-family parts of this kind)" if h["shared_with"] else "")
        tier = "" if h["tier"] == "strong" else "  (corroborated weak)"
        print(f"    [{h['fired']}/{h['n_fingerprints']} fp, {h['n_elements']} elem, worst {h['worst_residual_frac']:.2f} of tol]  {h['reference']}{tag}{tier}")
    if extra:
        print(f"  additionally at LOOSE (lineage / platform level): {len(extra)} parts")
        for h in extra:
            tier = "" if h["tier"] == "strong" else "  (corroborated weak)"
            print(f"    [{h['fired']}/{h['n_fingerprints']} fp, {h['n_elements']} elem, worst {h['worst_residual_frac']:.2f} of tol]  {h['reference']}{tier}")
        if a.audit:
            for k, (v, r, t) in h["residuals"].items():
                print(f"        {k:<14s} {v:9.3f} vs {r:9.3f}  (tol {t})")


if __name__ == "__main__":
    main()
