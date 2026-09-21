"""Curate the reference library: which parts are identifiable *and* unlikely
to be confused with non-gun objects, and which fingerprints are strong enough
to use.

The mined fingerprints are discriminating *within* the library, but the
world outside the library is full of brackets and enclosures with 4 mm and
1/4" holes. So each fingerprint element is classified with
`calibers.classify_diameter` and the fingerprint graded:

  strong   has a bore (long hole at a bullet diameter), a receiver interface
           (buffer-tube thread, deep grip-screw hole, muzzle thread), a known
           platform pin pair (AR fire-control pins), >= 3 elements, or 2
           elements of which at least one is a hole/boss/cone with a
           non-standard diameter (slot/wall widths never count as specific)
  weak     2 elements, both at standard drill / metric / fillet sizes:
           a random mechanical part could satisfy it

Parts are graded:

  hardware        looks like generic hardware (pins, springs, screws, caps,
                  pegs, rollers...): name keyword + small + featureless.
                  Excluded from the identification library regardless of
                  fingerprint, since any pin matches any pin.
  unidentifiable  fewer than 2 axis-bearing elements -> no fingerprint
  weak / strong   per the fingerprint grade above (after re-mining with the
                  strength requirement)

Outputs data/curation.csv and data/fingerprints.json (strong only).
"""
from __future__ import annotations

import itertools
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .calibers import (BORE_MIN_L, BORE_MIN_LD, INTERFACES, classify_diameter, magazine_depth_match,
                       magazine_match, platform_pair_match)
from .fingerprint import Fingerprint, dedupe_coaxial, elements, hole_relation, match
from .mine import MIN_COV, _cands, _diam_index
from .roles import role_of
from .similarity import load
from .tolerances import LOOSE, TIGHT

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

HARDWARE_WORDS = re.compile(
    r"\b(pin|pins|spring|screw|screws|washer|spacer|peg|pegs|roller|nut|bolt_?head|cap|plug|"
    r"insert|detent|plunger|key|rod|dowel|rivet|shim|bearing|clip|shaft)\b", re.I)
# Magazines are excluded from identification: manufacturers make them in many
# sizes, so a magazine body/follower/baseplate is not an exact-match target.
# Receiver-side parts (magazine catch, release, well) stay.
MAGAZINE_MODEL = re.compile(r"(magazine|\bmag\b|magblock|drum|stripper|feed_?lip)", re.I)
MAGAZINE_KEEP = re.compile(r"(catch|release|well|retainer|lower|receiver|frame|pistol|rifle|jig)", re.I)
MAGAZINE_PART = re.compile(r"\b(follower|baseplate|base_?plate|floor_?plate|floorplate|feed_?lips?|"
                           r"mag_?body|magazine_?body|spring_?plate|locking_?plate)\b", re.I)
HARDWARE_MAX_EXTENT = 40.0     # mm
HARDWARE_MAX_ELEMS = 3
MAX_SIZE = 4
WINDOW = 20                 # best-ranked elements the constellation search runs over
                            # (12 lost the G19 BTB frames and the VZ 61 once the gate had 2 versions)


# --------------------------------------------------------------------------- #
def _facing_pairs(planes):
    """(sep, plane_a, plane_b, normal) for anti-parallel plane pairs whose
    normals point at each other (a channel / well, not a block)."""
    for a, b in itertools.combinations(planes, 2):
        na, nb = np.asarray(a["n"]), np.asarray(b["n"])
        if na @ nb > -0.999:
            continue
        sep = float((np.asarray(b["p"]) - np.asarray(a["p"])) @ na)
        if sep > 0:
            yield sep, a, b, na


def magazine_wells(sig: dict) -> list[dict]:
    """A magazine well: facing side walls at a magazine width (owner's table)
    AND perpendicular facing front/back walls at a cartridge-OAL-derived depth,
    AND at least one distinctive feature inside/around the well:

      rails   ribs or guide rails on the side walls: elongated planes parallel
              to a wall, set 0.5-4 mm inward from it, lying within the well
      holes   a catch / latch / bolt-catch hole through a side or end wall:
              a hole whose axis is parallel to a wall normal and whose centre
              lies within the well footprint
      flare   a chamfered mouth: planes tilted 10-60 deg to the well's axis
              (or cones) with a point within the footprint

    A plain channel with the right width and depth (stock tube, grip cavity,
    handguard half) has none of these and is rejected.
    """
    planes_all = [p for p in sig.get("planes", []) if "n" in p]
    big = [p for p in planes_all if p["area"] >= 400 and min(p["ext"]) >= 20]
    widths = [(sep, a, b, na) for sep, a, b, na in _facing_pairs(big) if magazine_match(sep)]
    if not widths:
        return []
    depths = [(sep, a, b, na) for sep, a, b, na in _facing_pairs(big) if magazine_depth_match(sep)]
    out = []
    for w, wa, wb, nw in widths:
        for d, da, db, nd in depths:
            if abs(nw @ nd) > 0.05:
                continue
            # well footprint: slab between the width walls x slab between depth walls
            pw, pd = np.asarray(wa["p"]), np.asarray(da["p"])
            axis = np.cross(nw, nd); axis /= np.linalg.norm(axis)

            def inside(pt, slack=3.0):
                u = (np.asarray(pt) - pw) @ nw
                v = (np.asarray(pt) - pd) @ nd
                return -slack <= u <= w + slack and -slack <= v <= d + slack

            rails = [p for p in planes_all if p["area"] >= 10 and max(p["ext"]) / max(min(p["ext"]), 0.1) >= 2.5
                     and abs(abs(np.asarray(p["n"]) @ nw) - 1) < 1e-3 and inside(p["p"])
                     and 0.3 <= min((np.asarray(p["p"]) - pw) @ nw, w - (np.asarray(p["p"]) - pw) @ nw) <= 6.0]
            holes = [h for h in sig["holes"] if h["cov"] >= 0.9 and h["area"] >= 3
                     and (abs(abs(np.asarray(h["a"]) @ nw) - 1) < 0.02 or abs(abs(np.asarray(h["a"]) @ nd) - 1) < 0.02)
                     and inside(h["c"], slack=6.0)]
            flare = [p for p in planes_all if p["area"] >= 15 and inside(p["p"], slack=6.0)
                     and 0.17 <= abs(np.asarray(p["n"]) @ axis) <= 0.87]
            feats = {"rails": len(rails), "holes": len(holes), "flare": len(flare)}
            # chamfers alone are not distinctive; need a catch/latch hole or a rail
            if feats["holes"] + feats["rails"] == 0:
                continue
            out.append({"width": round(w, 2), "depth": round(d, 2), "kind": magazine_match(w),
                        "area": round(min(wa["area"], wb["area"]), 1), "features": feats,
                        "hole_list": holes})
    return out


def grade_elements(elems: list[dict]) -> list[str]:
    """Slot/wall widths are never 'specific': arbitrary wall thicknesses are
    what every non-gun object has. Slabs count toward the >=3-element rule
    and can pair with an anchored hole, but never carry identity alone."""
    return ["generic" if e["kind"] in ("slot", "wall") else classify_diameter(e["d"], e["kind"], e.get("L", 0.0), e.get("area"))[0]
            for e in elems]


def platform_pairs(elems: list[dict]) -> list[str]:
    out = []
    for a, b in itertools.combinations(elems, 2):
        if a["kind"] == b["kind"] == "hole":
            ang, sep = hole_relation(a, b)
            name = platform_pair_match(a, b, ang, sep)
            if name:
                out.append(name)
    return out


def informative_relation(e1: dict, e2: dict) -> bool:
    """Does the pair's relation constrain geometry beyond the two dimensions?
    line-line: always (angle + separation). slab-slab: only when parallel
    (spacing). line-slab: only when the axis lies in the slab plane (offset);
    a hole *through* a wall has no invariant offset, so "hole d in a wall of
    thickness t" is all that pair says - which every plate with a hole says."""
    k1, k2 = e1["kind"] in ("slot", "wall"), e2["kind"] in ("slot", "wall")
    ang, _ = hole_relation(e1, e2)
    if not k1 and not k2:
        return True
    if k1 and k2:
        return ang < 1.0
    return ang > 89.0


def fingerprint_strength(elems: list[dict], has_magwell: bool) -> str:
    """Strength rules, revised against the negative set (printer parts,
    data/analysis_negatives.txt): every 2-element fingerprint that fired on a
    bracket was 'round feature + wall it passes through' or two same-size
    holes with a generic-at-LOOSE diameter.

      * every element must take part in at least one informative relation
      * a stack of wall thicknesses / channel widths is what extruded
        brackets and rails have too: at least one round feature
      * a bore (long hole at a printed-gun bullet diameter) is an anchor by
        itself and exempt from the relation rule
      * 2 elements: a platform pin pair, or two round elements of which one
        is an interface / specific diameter (never round + slab) and that
        anchor is not a fragile ring: a 1 mm step on a shaft is every turned
        pin (the M4A1 bolt's Ø12.39 x 1.35 step matched an AK drum spring key)
      * >= 3 elements: at least one non-generic element or two round ones
    """
    round_ = [e for e in elems if e["kind"] in ("hole", "boss", "cone")]
    if not round_:
        return "weak"
    if all(is_fragile(e) for e in round_):
        return "weak"            # chamfer rings alone: fragile and shared by every turned pin
    classes = grade_elements(elems)
    if any(c == "bore" for c in classes):
        return "strong"          # a bore is an anchor on its own; the partner corroborates
    for i, e in enumerate(elems):
        if not any(informative_relation(e, o) for j, o in enumerate(elems) if j != i):
            return "weak"
    if platform_pairs(elems):
        return "strong"
    if len(elems) == 2:
        if len(round_) < 2:
            return "weak"
        anchors = [e for e, c in zip(elems, classes) if c in ("interface", "specific") and not is_fragile(e)]
        return "strong" if anchors else "weak"
    if any(c != "generic" for c in classes) or len(round_) >= 2:
        return "strong"
    # NOTE: a detected magazine-well cavity is deliberately *not* an anchor.
    # Well walls in this library are drafted/ribbed (few planar pairs) and
    # magazine sizes vary by manufacturer; the identity of a well comes from
    # the features around it (catch/pin holes, grip screw, rails), which the
    # constellation already carries. `magwells` is kept as a diagnostic.
    return "weak"


def is_magazine(sig: dict) -> bool:
    model = sig["model"].replace("-", "_")
    part = sig["part"].replace("-", "_").replace(" ", "_")
    if MAGAZINE_MODEL.search(model) and not MAGAZINE_KEEP.search(model):
        return True
    return bool(MAGAZINE_PART.search(part))


def is_hardware(sig: dict, n_axis_elems: int) -> bool:
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", sig["part"]).replace("_", " ").replace("-", " ")   # CamelCase -> words
    if not HARDWARE_WORDS.search(name):
        return False
    return max(sig["obb"]) <= HARDWARE_MAX_EXTENT and n_axis_elems <= HARDWARE_MAX_ELEMS


# --------------------------------------------------------------------------- #
def _elem_key(e: dict) -> str:
    return f"{e['kind']}|{e['d']:.3f}|{e['c'][0]:.2f},{e['c'][1]:.2f},{e['c'][2]:.2f}"


def _load_survival() -> dict:
    p = DATA / "survival.json"
    return json.load(p.open()) if p.exists() else {}


SURVIVAL = _load_survival()


def _load_perturbed() -> dict:
    p = DATA / "signatures_perturbed.jsonl"
    out: dict = {}
    if p.exists():
        for line in p.open():
            r = json.loads(line)
            out.setdefault(r["path"], []).append(r)
    return out


PERTURBED = _load_perturbed()
N_FINGERPRINTS = 3          # up to this many disjoint fingerprints per part
FRAGILE_MIN_L = 1.5         # mm: a cone / cylinder shorter than this is a chamfer edge
FRAGILE_MIN_AREA = 10.0     # mm^2


def is_fragile(e: dict) -> bool:
    """An element that a remesh, decimation or scan is likely to destroy:
    sub-millimetre chamfer rings and tiny patches. The miner avoids them when
    a sturdier constellation is also discriminating (they are still allowed
    as a last resort, since a 0.4 mm chamfer *is* a real design feature)."""
    if e["kind"] in ("slot", "wall"):
        return False
    return e.get("L", 0.0) < FRAGILE_MIN_L or e.get("area", 0.0) < FRAGILE_MIN_AREA


def mine_strong(r_i: int, rows: list[dict], idx, band=TIGHT, n_fp: int = N_FINGERPRINTS) -> dict:
    """Like mine.mine_part, but a constellation must also be 'strong', and up
    to `n_fp` fingerprints on *disjoint* element sets are mined per part so a
    fused or damaged input that loses one region is still caught by another.
    Each fingerprint is the smallest strong constellation available among the
    elements not yet used; among equals, the one with the fewest fragile
    elements, most round features, most gun-specific elements, largest area."""
    r = rows[r_i]
    elems = dedupe_coaxial(elements(r, MIN_COV), band)
    wells = magazine_wells(r)
    well_holes = {tuple(h["c"]) for w in wells for h in w.get("hole_list", [])}
    for e in elems:
        e["in_magwell"] = tuple(e["c"]) in well_holes
    same_model = {i for i, o in enumerate(rows) if o["model"] == r["model"]}
    base = {"path": r["path"], "model": r["model"], "part": r["part"], "n_elems": len(elems),
            "magwells": wells}
    if is_magazine(r):
        return {**base, "status": "magazine"}
    n_axis = sum(e["kind"] in ("hole", "boss", "cone") for e in elems)
    if is_hardware(r, n_axis):
        return {**base, "status": "hardware"}
    if len(elems) < 2:
        return {**base, "status": "unidentifiable"}
    step = band.diameter
    cand = [_cands(idx, h, step) - same_model for h in elems]
    classes = grade_elements(elems)
    rank = {"bore": 0, "interface": 0, "specific": 1, "generic": 2}
    is_slab = [e["kind"] in ("slot", "wall") for e in elems]
    fragile = [is_fragile(e) for e in elems]
    # survival under remesh / decimation / jitter (scripts/element_survival.py);
    # unknown -> assume it survives so the miner is not starved
    sv = SURVIVAL.get(r["path"], {})
    surv = [sv.get(_elem_key(e), 3) for e in elems]
    for e, k in zip(elems, surv):
        e["surv"] = k
    order = sorted(range(len(elems)), key=lambda i: (rank[classes[i]], is_slab[i], fragile[i], -surv[i], len(cand[i])))[:WINDOW]
    sig_elems = {"path": r["path"], "holes": elems}
    core = Fingerprint.from_reference(r["part"], sig_elems, order)
    is_container = {}

    def container(o_i):
        if o_i not in is_container:
            is_container[o_i] = bool(match(core, rows[o_i], band, "TIGHT", MIN_COV))
        return is_container[o_i]

    def search(pool_order):
        """Smallest-k strong constellation over `pool_order`; returns
        (k, best_entry) or (k, weak_fallback) with a 'weak' flag."""
        weak_fallback = None
        for k in range(2, min(MAX_SIZE, len(pool_order)) + 1):
            best = None
            for subset in itertools.combinations(pool_order, k):
                sub = [elems[i] for i in subset]
                strength = fingerprint_strength(sub, bool(wells))
                pool = set.intersection(*(cand[i] for i in subset))
                fp = Fingerprint.from_reference(r["part"], sig_elems, list(subset))
                hits, containers = [], []
                for o_i in pool:
                    if match(fp, rows[o_i], band, "TIGHT", MIN_COV):
                        (containers if container(o_i) else hits).append(rows[o_i]["path"])
                        if hits:
                            break
                if hits:
                    continue
                # must still be found on the part's own remeshed / decimated /
                # jittered versions (LOOSE), else it is not a reliable fingerprint
                if not all(match(fp, ps, LOOSE, "LOOSE", MIN_COV) for ps in PERTURBED.get(r["path"], [])):
                    continue
                score = (-sum(fragile[i] for i in subset), min(surv[i] for i in subset),
                         sum(elems[i]["kind"] in ("hole", "boss", "cone") for i in subset),
                         sum(classes[i] != "generic" for i in subset), sum(elems[i]["area"] for i in subset))
                entry = (score, subset, fp, containers)
                if strength == "strong":
                    if best is None or score > best[0]:
                        best = entry
                elif weak_fallback is None or score > weak_fallback[0]:
                    weak_fallback = entry
            if best is not None:
                return k, best, False
        if weak_fallback is not None:
            return len(weak_fallback[1]), weak_fallback, True
        return 0, None, True

    k, entry, weak = search(order)
    if entry is None:
        return {**base, "status": "shared"}
    status = "weak" if weak else "strong"
    out = _result(base, status, k, entry, elems, classes)
    # further fingerprints on the elements not used so far. For a weak part
    # these are the corroborating set: detect() labels a weak part only when
    # at least two of its independent weak fingerprints fire (one generic
    # constellation is what any bracket has; two disjoint ones on the same
    # object at the right relative dimensions is not).
    used = set(entry[1])
    alternates = []
    while len(alternates) + 1 < n_fp:
        rest = [i for i in order if i not in used]
        if len(rest) < 2:
            break
        k2, e2, weak2 = search(rest)
        if e2 is None or (weak2 and status == "strong"):
            break
        alternates.append(_result({}, "weak" if weak2 else "strong", k2, e2, elems, classes))
        used |= set(e2[1])
    out["alternates"] = alternates
    return out


def _anchor_min_length(e: dict, cls: str) -> float | None:
    """Minimum length a target element must have to count as this anchor
    (matching otherwise ignores length): a bore is >= 25 mm and 4 diameters,
    an interface has its table minimum."""
    if cls == "bore":
        return max(BORE_MIN_L, BORE_MIN_LD * e["d"])
    if cls == "interface":
        for lo, hi, min_L in INTERFACES.values():
            if lo <= e["d"] <= hi and e.get("L", 0) >= min_L:
                return min_L
    return None


def _result(base, status, k, entry, elems, classes):
    _, subset, fp, containers = entry
    holes = []
    for i in subset:
        h = dict(elems[i])
        m = _anchor_min_length(h, classes[i])
        if m:
            h["min_L"] = round(m, 2)
        holes.append(h)
    return {**base, "status": status, "size": k,
            "holes": holes,
            "element_classes": [classes[i] for i in subset],
            "fragile": int(sum(is_fragile(elems[i]) for i in subset)),
            "relations": {f"{a}-{b}": [round(v, 3) for v in rel] for (a, b), rel in fp.relations.items()},
            "contained_in": sorted(containers)}


_ROWS = None; _IDX = None


def _init_pool():
    global _ROWS, _IDX
    _ROWS = load(); _IDX = _diam_index(_ROWS, TIGHT)


def _mine(r_i: int) -> dict:
    return mine_strong(r_i, _ROWS, _IDX)


def main():
    import argparse
    from concurrent.futures import ProcessPoolExecutor
    ap = argparse.ArgumentParser(); ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()
    rows = load()
    with ProcessPoolExecutor(a.workers, initializer=_init_pool) as ex:
        results = list(ex.map(_mine, range(len(rows)), chunksize=4))

    df = pd.DataFrame([{
        "path": r["path"], "model": r["model"], "part": r["part"], "status": r["status"],
        "n_elems": r["n_elems"], "size": r.get("size"),
        "elements": "; ".join(f"{h['kind']} {h['d']:.2f}x{h['L']:.1f} [{c}]"
                              for h, c in zip(r.get("holes", []), r.get("element_classes", []))),
        "gun_specific": "; ".join(sorted({classify_diameter(h['d'], h['kind'], h.get('L', 0))[1]
                                         for h in r.get("holes", []) if classify_diameter(h['d'], h['kind'], h.get('L', 0))[1]}
                                        | {w["kind"] for w in r["magwells"]}
                                        | set(platform_pairs(r.get("holes", []))))),
        "magwell_widths": "; ".join(f"{w['width']}x{w['depth']} {w['features']}" for w in r["magwells"]),
        "magwells": None,
        "contained_in": len(r.get("contained_in", [])),
        "n_fingerprints": (1 + len(r.get("alternates", []))) if r["status"] in ("strong", "weak") else 0,
        "role": role_of(r["part"], r["model"]),
        "fragile_elements": r.get("fragile"),
    } for r in results])
    df.to_csv(DATA / "curation.csv", index=False)
    strong = [r for r in results if r["status"] == "strong"]
    # weak parts with >= 2 disjoint fingerprints are shipped as "corroborate"
    # (detect requires two of them to fire); weak parts with one are not
    corroborate = [r for r in results if r["status"] == "weak" and len(r.get("alternates", [])) >= 1]
    shipped = [{**r, "status": "ok"} for r in strong] + [{**r, "status": "corroborate"} for r in corroborate]
    json.dump(shipped, (DATA / "fingerprints_all.json").open("w"), indent=0)
    json.dump(shipped, (DATA / "fingerprints.json").open("w"), indent=0)   # scripts/reliability.py narrows this
    print(f"  weak parts with >= 2 disjoint fingerprints (corroboration tier): {len(corroborate)}")
    for r in results:
        for w in r["magwells"]:
            w.pop("hole_list", None)
    json.dump(results, (DATA / "curation_all.json").open("w"), indent=0)

    print("== curation of", len(df), "unique parts (library after removals)")
    print(df.status.value_counts().to_string())
    print("\n  strong fingerprints by size:", df[df.status == "strong"]["size"].value_counts().sort_index().to_dict())
    print("  fingerprints per strong part:", df[df.status == "strong"]["n_fingerprints"].value_counts().sort_index().to_dict(),
          " total:", int(df.n_fingerprints.sum()))
    print("  primary fingerprints using a fragile element:", int((df[df.status == "strong"].fragile_elements > 0).sum()))
    print("  strong with a bore / interface / magwell anchor:", (df[df.status == "strong"].gun_specific != "").sum())
    print("  parts with a detected magazine well (with rails/holes/flare):", (df.magwell_widths != "").sum())
    print("  magazine parts excluded:", (df.status == "magazine").sum())
    print("\n  by category:")
    df["category"] = df.path.str.split("/").str[0]
    print(pd.crosstab(df.category, df.status).to_string())
    print("\n  weak examples (would fire on generic hardware):")
    for _, r in df[df.status == "weak"].head(8).iterrows():
        print(f"    {r.path}\n        {r.elements}")
    print("\n  hardware examples:")
    print("    " + "\n    ".join(df[df.status == "hardware"].part.head(15).tolist()))
    print(f"\nwrote data/curation.csv, data/fingerprints.json ({len(strong)} strong fingerprints)")


if __name__ == "__main__":
    main()
