"""Make the "must be refused" test part for CI - without any firearm model.

    python make_test_part.py <firearm-check dir> <out.stl>

The repository ships no gun parts, but the installer tests need an object the
gate must refuse. This takes a shipped fingerprint that consists only of holes
(diameters, positions and axis angles measured on a reference part) and drills
exactly those holes through a plain block. The result is not a gun part - it is
a box with holes - but it carries the fingerprint's constellation, so the
checker must identify it at design level. Candidates are tried in a fixed order
and the first one the checker (the bundled pipeline) blocks is written.

Needs numpy, scipy, trimesh and manifold3d (for the boolean difference).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import trimesh

MARGIN = 6.0          # mm of material around the hole pattern
MAX_TRIES = 40


def drilled_block(holes: list[dict]) -> trimesh.Trimesh:
    c = np.array([h["c"] for h in holes], float)
    r = max(h["d"] for h in holes) / 2
    lo, hi = c.min(0) - r - MARGIN, c.max(0) + r + MARGIN
    block = trimesh.creation.box(extents=hi - lo)
    block.apply_translation((lo + hi) / 2)
    span = float(np.linalg.norm(hi - lo)) * 2
    for h in holes:
        cyl = trimesh.creation.cylinder(radius=h["d"] / 2, height=span, sections=96)
        a = np.asarray(h["a"], float); a /= np.linalg.norm(a)
        cyl.apply_transform(trimesh.geometry.align_vectors([0, 0, 1], a))
        cyl.apply_translation(h["c"])
        block = block.difference(cyl, engine="manifold")
    return block


def main():
    checker, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    sys.path.insert(0, str(checker))
    from pipeline.check import Library, check_mesh

    lib = Library()
    shipped = json.load(open(checker / "data" / "fingerprints.json"))
    # strong parts whose primary fingerprint is holes only, frames/receivers first
    cands = [r for r in shipped if r["status"] == "ok" and r["holes"]
             and all(h["kind"] == "hole" for h in r["holes"])]
    cands.sort(key=lambda r: ("receiver" not in r["path"].lower() and "frame" not in r["path"].lower(), r["path"]))
    for r in cands[:MAX_TRIES]:
        try:
            mesh = drilled_block(r["holes"])
        except Exception as e:                       # noqa: BLE001 - try the next one
            print(f"  skip {r['path']}: {e}")
            continue
        v = check_mesh(mesh, "test_part", lib, scale=1.0)
        design = [e for e in v.evidence if e.level == "design" and e.reference == r["path"]]
        print(f"  {v.verdict:<5} {r['path']}  ({len(r['holes'])} holes)")
        if v.verdict == "BLOCK" and design:
            out.parent.mkdir(parents=True, exist_ok=True)
            mesh.export(out)
            print(f"wrote {out}: block {np.round(mesh.extents, 1)} mm with the hole pattern of {r['path']}")
            return
    sys.exit("no candidate fingerprint produced a part the checker blocks")


if __name__ == "__main__":
    main()
