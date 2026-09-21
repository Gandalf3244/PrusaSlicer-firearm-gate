"""The whole pipeline as one call: a 3D model in, print / do-not-print out.

    python -m pipeline.check model.stl [more files or folders] [--json] [--units mm|in|cm]

Exit code: 0 = every input may be printed, 2 = at least one must not, 1 = an
input could not be read. Meant to be called by a slicer hook or a print
server; `check_file()` / `check_mesh()` are the same thing as a function.

Formats: anything trimesh reads (STL, 3MF, OBJ, PLY, OFF, GLB/GLTF) and
SolidWorks .SLDPRT (scripts/sldprt2stl.py). A file with several bodies (a
build plate) is checked as a whole and then body by body, each with its own
unit inference.

Decision (every rule measured on the negative set, 0/432 firings each):
    design    a shipped reference part is identified at TIGHT tolerance
              (strong fingerprint, or two disjoint ones of a weak part)
    lineage   the same at LOOSE: a derivative / re-export of a known design
    platform  family patterns present (e.g. the AR-15 fire-control pocket)
    bore      a long hole at a printed-gun bullet diameter (a barrel)
Any of them -> BLOCK. Otherwise ALLOW.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import trimesh

from .calibers import bore_evidence
from .detect import detect, load_fingerprints, signature_of_mesh
from .families import classify_family, load_family_patterns
from .pool import trim_after
from .roles import role_of
from .units import UNIT_SCALE, infer_scale, prepare_mesh

ROOT = Path(__file__).resolve().parents[1]
MESH_SUFFIXES = {".stl", ".3mf", ".obj", ".ply", ".off", ".glb", ".gltf", ".sldprt"}
MIN_BODY_FACES = 200        # smaller bodies are supports, text, debris
PLAUSIBLE_MM = (8.0, 1500.0)   # largest extent of a printable gun part, after scaling


def alternative_scales(raw: trimesh.Trimesh, chosen: float) -> list[float]:
    """Other unit readings (mm / inch / cm) that leave the object a plausible
    size. The C96 receiver in the library is modelled in centimetres and reads
    as a 16 mm trinket at the inferred scale; a user file has no path to look
    an override up by, so a clean whole-mesh pass is retried at these, with
    constellation evidence only (see Library.evidence_for)."""
    ext = float(max(raw.extents))
    return [sc for sc in sorted(set(UNIT_SCALE.values()) - {chosen})
            if PLAUSIBLE_MM[0] <= ext * sc <= PLAUSIBLE_MM[1]]


@dataclass
class Evidence:
    level: str                  # design | lineage | platform | bore
    what: str                   # human-readable
    reference: str = ""         # library part path (design / lineage)
    model: str = ""
    role: str = ""
    confidence: float = 1.0     # fingerprints fired / fingerprints of the part
    body: str = "whole"


@dataclass
class Verdict:
    path: str
    verdict: str                # BLOCK | ALLOW | ERROR
    level: str | None = None    # strongest evidence level
    evidence: list[Evidence] = field(default_factory=list)
    bodies: int = 1
    holes: int = 0
    noise_mm: float = 0.0
    seconds: float = 0.0
    error: str = ""

    def to_json(self) -> dict:
        d = asdict(self)
        d["evidence"] = [asdict(e) for e in self.evidence]
        return d


class Library:
    """Shipped fingerprints + family patterns, loaded once."""

    def __init__(self):
        self.fps = load_fingerprints()
        self.patterns = load_family_patterns()
        self.n_parts = len({fp.source for fp in self.fps})

    def evidence_for(self, sig: dict, body: str, bores: bool = True) -> list[Evidence]:
        """`bores=False` at a guessed unit scale: a bore is a single dimension,
        and a PCB DIN clip's Ø1.9 x 9.6 mm hole read at x10 is a "12-gauge barrel"."""
        out: list[Evidence] = []
        tight = detect(sig, self.fps, loose=False)
        seen = {h["reference"] for h in tight}
        loose = [h for h in detect(sig, self.fps, loose=True) if h["reference"] not in seen]
        for level, hits in (("design", tight), ("lineage", loose)):
            for h in hits:
                ref = h["reference"]; parts = ref.split("/")
                model = parts[1] if len(parts) > 1 else ""
                role = role_of(parts[-1], model)
                out.append(Evidence(level, f"{role}: {parts[-1]} ({model}), {h['fired']}/{h['n_fingerprints']} fingerprints",
                                    ref, model, role, h["confidence"], body))
        for fam, n in classify_family(sig, self.patterns).items():
            out.append(Evidence("platform", f"{fam} platform pattern x{n}", "", "", "", 1.0, body))
        for b in bore_evidence(sig) if bores else []:
            out.append(Evidence("bore", f"barrel bore {b['caliber']} (Ø{b['d']:.2f} x {b['L']:.0f} mm)", "", "", "barrel", 1.0, body))
        return out


_LEVEL_RANK = {"design": 0, "lineage": 1, "platform": 2, "bore": 3}


def load_any(path: Path) -> trimesh.Trimesh:
    if path.suffix.lower() == ".sldprt":
        sys.path.insert(0, str(ROOT / "scripts"))
        from sldprt2stl import sldprt_to_mesh
        mesh, _ = sldprt_to_mesh(path)
        return mesh
    return trimesh.load(path, force="mesh", process=True)


def check_mesh(raw: trimesh.Trimesh, name: str, lib: Library, scale: float | None = None,
               thorough: bool = False) -> Verdict:
    """Verdict for an already loaded mesh in its file units. Stops at the first
    blocking pass unless `thorough` (then every body is reported)."""
    t0 = time.time()
    s = infer_scale(raw) if scale is None else scale
    whole = prepare_mesh(raw.copy(), scale=s)
    sig = signature_of_mesh(whole, name, s)
    ev = lib.evidence_for(sig, "whole")
    if not ev and scale is None:
        for alt in alternative_scales(raw, s):
            m = prepare_mesh(raw.copy(), scale=alt)
            ev = lib.evidence_for(signature_of_mesh(m, f"{name}@x{alt:g}", alt), f"units x{alt:g}", bores=False)
            if ev:
                break
    bodies = [b for b in whole.split(only_watertight=False) if len(b.faces) >= MIN_BODY_FACES]
    # a plate of several parts: each body on its own, with its own unit guess
    # (a single part split into shells by a broken mesh is covered by "whole")
    if len(bodies) > 1 and (thorough or not ev):
        for i, b in enumerate(bodies):
            raw_b = b.copy(); raw_b.apply_scale(1.0 / s)
            sb = infer_scale(raw_b) if scale is None else scale
            mb = prepare_mesh(raw_b, scale=sb)
            ev += lib.evidence_for(signature_of_mesh(mb, f"{name}#body{i}", sb), f"body {i}")
    # one line per distinct finding
    uniq: dict[tuple, Evidence] = {}
    for e in ev:
        k = (e.level, e.reference or e.what)
        if k not in uniq or e.confidence > uniq[k].confidence:
            uniq[k] = e
    ev = sorted(uniq.values(), key=lambda e: (_LEVEL_RANK[e.level], -e.confidence))
    return Verdict(name, "BLOCK" if ev else "ALLOW", ev[0].level if ev else None, ev,
                   max(1, len(bodies)), sig["counts"]["holes"], sig.get("noise", 0.0), round(time.time() - t0, 2))


def check_file(path: Path, lib: Library, scale: float | None = None, thorough: bool = False) -> Verdict:
    try:
        raw = load_any(path)
        if raw.is_empty or len(raw.faces) == 0:
            raise ValueError("no triangles")
    except Exception as e:                      # unreadable input is reported, not raised
        return Verdict(str(path), "ERROR", error=f"{type(e).__name__}: {e}"[:200])
    return check_mesh(raw, str(path), lib, scale, thorough)


_LIB: Library | None = None


def _pool_init():
    global _LIB
    _LIB = Library()


@trim_after
def _pool_check(args) -> Verdict:
    path, scale, thorough = args
    return check_file(path, _LIB, scale, thorough)


def _inputs(paths: list[str]) -> list[Path]:
    out = []
    for p in map(Path, [x for x in paths if x.strip()]):
        if p.is_dir():
            out += sorted(q for q in p.rglob("*") if q.suffix.lower() in MESH_SUFFIXES)
        else:
            out.append(p)
    return out


def format_verdict(v: Verdict, lib: Library) -> str:
    if v.verdict == "ERROR":
        return f"{v.path}\n  ERROR  could not read: {v.error}"
    head = "DO NOT PRINT" if v.verdict == "BLOCK" else "OK TO PRINT"
    lines = [f"{v.path}\n  {head}  ({v.level or 'no firearm-part evidence'}; {v.holes} holes, "
             f"{v.bodies} bod{'y' if v.bodies == 1 else 'ies'}, noise {v.noise_mm:.3f} mm, "
             f"{lib.n_parts} reference parts, {v.seconds:.1f} s)"]
    for e in v.evidence:
        body = "" if e.body == "whole" else f"  [{e.body}]"
        lines.append(f"    {e.level:<9}{e.what}{body}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="print / do-not-print decision for 3D models")
    ap.add_argument("paths", nargs="+", help="model files or folders")
    ap.add_argument("--units", choices=sorted(UNIT_SCALE), help="file units if known (default: inferred)")
    ap.add_argument("--json", action="store_true", help="one JSON object per line instead of text")
    ap.add_argument("--thorough", action="store_true", help="report every body of a plate, not just the first blocking pass")
    ap.add_argument("--workers", type=int, default=1, help="parallel processes when checking many files")
    a = ap.parse_args()
    lib = Library()
    scale = UNIT_SCALE[a.units] if a.units else None
    paths = _inputs(a.paths)
    if a.workers > 1 and len(paths) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(a.workers, initializer=_pool_init) as ex:
            verdicts = list(ex.map(_pool_check, [(p, scale, a.thorough) for p in paths]))
    else:
        verdicts = (check_file(p, lib, scale, a.thorough) for p in paths)
    worst = 0
    for v in verdicts:
        print(json.dumps(v.to_json()) if a.json else format_verdict(v, lib), flush=True)
        worst = max(worst, {"ALLOW": 0, "ERROR": 1, "BLOCK": 2}[v.verdict])
    sys.exit(worst)


if __name__ == "__main__":
    main()
