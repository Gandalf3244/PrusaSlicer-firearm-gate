"""Per-part primitive-composition signature (report: Choice 1, recommended option).

A signature is the full list of fitted primitives in a pose-independent form,
plus global shape descriptors. It is stored as JSON so fingerprints (Choice 2:
compound dimension sets) can be defined and re-evaluated later without
re-fitting meshes.

Pose independence: every quantity is either scalar (diameter, length, area,
angle) or a *relation* between two primitives (centre distance, axis angle),
never an absolute coordinate.
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import trimesh

from .primitives import Segmentation, segment
from .units import infer_scale, load_mesh

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / "fosscad-repo"
DATA = ROOT / "data"

MIN_HOLE_AREA = 3.0        # mm^2: ignore text/engraving-scale cylinders
MIN_COVERAGE = 0.45        # a hole must be at least a half-circle to carry a dimension


def _r(x, n=4):
    return round(float(x), n)


def global_features(mesh: trimesh.Trimesh) -> dict:
    obb = mesh.bounding_box_oriented.primitive.extents
    ext = np.sort(np.asarray(obb))[::-1]
    f = {
        "obb": [_r(e, 3) for e in ext],
        "obb_ratios": [_r(ext[1] / ext[0]), _r(ext[2] / ext[0])],
        "area": _r(mesh.area, 2),
        "n_faces": int(len(mesh.faces)),
        "watertight": bool(mesh.is_watertight),
        "n_bodies": int(mesh.body_count),
    }
    if mesh.is_watertight and mesh.volume > 0:
        f["volume"] = _r(mesh.volume, 2)
        f["fill"] = _r(mesh.volume / float(np.prod(ext)))       # volume / OBB volume
        try:
            I = np.linalg.eigvalsh(mesh.moment_inertia)
            I = np.sort(np.abs(I))[::-1]
            f["inertia_ratios"] = [_r(I[1] / I[0]), _r(I[2] / I[0])]
        except Exception:  # noqa: BLE001
            pass
    return f


def primitive_features(seg: Segmentation, concavity_ambiguous: bool = False) -> dict:
    A = seg.total_area
    holes = [c for c in seg.cylinders if c.concave and c.area >= MIN_HOLE_AREA and c.coverage >= MIN_COVERAGE]
    bosses = [c for c in seg.cylinders if not c.concave and c.area >= MIN_HOLE_AREA and c.coverage >= MIN_COVERAGE]
    holes.sort(key=lambda c: -c.area); bosses.sort(key=lambda c: -c.area)
    planes = sorted(seg.planes, key=lambda p: -p.area)
    cones = sorted(seg.cones, key=lambda c: -c.area)

    def cyl(c):
        # centre/axis are in mesh coordinates (pose-dependent): kept for
        # localisation and verification only, never compared directly
        d = {"d": _r(c.diameter, 3), "L": _r(c.length, 2), "cov": _r(c.coverage, 2),
             "area": _r(c.area, 1), "rms": _r(c.rms, 4),
             "c": [_r(x, 3) for x in c.center], "a": [_r(x, 5) for x in c.axis]}
        if concavity_ambiguous:
            d["amb"] = True          # hole / boss not decidable for this mesh
        return d

    # pairwise hole relations: the raw material for compound fingerprints
    rel = []
    for i in range(len(holes)):
        for j in range(i + 1, len(holes)):
            a, b = holes[i], holes[j]
            ang = np.degrees(np.arccos(np.clip(abs(a.axis @ b.axis), 0, 1)))
            rel.append({"i": i, "j": j, "dist": _r(np.linalg.norm(a.center - b.center), 2),
                        "axis_deg": _r(ang, 1)})

    # distinct plane normal directions (folding +/-) with their summed area
    dirs = []
    for p in planes:
        n = p.normal if p.normal[np.argmax(np.abs(p.normal))] > 0 else -p.normal
        for d in dirs:
            if abs(d["n"] @ n) > np.cos(np.radians(2)):
                d["area"] += p.area; d["count"] += 1
                break
        else:
            dirs.append({"n": n, "area": p.area, "count": 1})
    dirs.sort(key=lambda d: -d["area"])

    area_by = {
        "plane": sum(p.area for p in seg.planes),
        "cyl_hole": sum(c.area for c in seg.cylinders if c.concave),
        "cyl_boss": sum(c.area for c in seg.cylinders if not c.concave),
        "cone": sum(c.area for c in seg.cones),
        "freeform": sum(f.area for f in seg.freeform),
    }
    area_by["other"] = max(A - sum(area_by.values()), 0.0)

    return {
        "counts": {"planes": len(planes), "holes": len(holes), "bosses": len(bosses),
                   "cones": len(cones), "freeform": len(seg.freeform),
                   "plane_dirs": len(dirs)},
        "area_frac": {k: _r(v / A) for k, v in area_by.items()},
        "holes": [cyl(c) for c in holes],
        "bosses": [cyl(c) for c in bosses],
        "hole_pairs": rel,
        "cones": [{"half_deg": _r(c.half_angle_deg, 1), "d_min": _r(2 * c.r_min, 3),
                   "d_max": _r(2 * c.r_max, 3), "L": _r(c.length, 2), "concave": c.concave,
                   "cov": _r(c.coverage, 2), "area": _r(c.area, 1),
                   "c": [_r(x, 3) for x in c.apex], "a": [_r(x, 5) for x in c.axis]}
                  for c in cones if c.area >= MIN_HOLE_AREA and c.coverage >= MIN_COVERAGE],
        "planes": [{"area": _r(p.area, 1), "ext": [_r(e, 2) for e in p.extents],
                    "n": [_r(x, 5) for x in p.normal], "p": [_r(x, 3) for x in p.point]}
                   for p in planes if p.area >= 5.0],
        "plane_dirs": [{"area": _r(d["area"], 1), "count": d["count"]} for d in dirs[:12]],
    }


def signature_for(rel_path: str) -> dict:
    mesh = load_mesh(REPO / rel_path, rel_path)
    seg = segment(mesh)
    out = {"path": rel_path, "noise": _r(seg.noise, 5),
           "concavity_ambiguous": bool(mesh.metadata.get("concavity_ambiguous", False))}
    out.update(global_features(mesh))
    out.update(primitive_features(seg, out["concavity_ambiguous"]))
    return out


def _worker(rel_path: str) -> dict:
    try:
        return signature_for(rel_path)
    except Exception as e:  # noqa: BLE001
        return {"path": rel_path, "error": f"{type(e).__name__}: {e}"[:300]}


def main(out: Path = DATA / "signatures.jsonl", workers: int = 16, limit: int | None = None,
         update: bool = False):
    """Fit every unique geometry in the inventory. With `update`, only files
    not yet in the signature file are fitted and files no longer in the
    inventory are dropped - adding a new model / family to fosscad-repo is
    then: python -m pipeline.inventory && python -m pipeline.signature --update
    && python -m pipeline.curate && python -m pipeline.families."""
    import pandas as pd
    inv = pd.read_csv(DATA / "inventory.csv")
    # one representative per unique geometry (ascii/binary duplicates share a hash)
    inv = inv[inv.error.isna()].drop_duplicates("geom_hash")
    # big meshes first so the pool tail is short
    paths = list(inv.sort_values("n_faces", ascending=False).path)
    if limit:
        paths = paths[:limit]
    existing: dict[str, str] = {}
    if update and out.exists():
        for line in out.open():
            r = json.loads(line)
            if "error" not in r:
                existing[r["path"]] = line
        keep = {p: existing[p] for p in paths if p in existing}
        paths = [p for p in paths if p not in existing]
        print(f"{len(keep)} signatures kept, {len(existing) - len(keep)} dropped, {len(paths)} new", file=sys.stderr)
    else:
        keep = {}
    print(f"{len(paths)} unique geometries to fit", file=sys.stderr)
    done = 0
    t0 = time.time()
    with out.open("w") as fh, ProcessPoolExecutor(workers) as ex:
        for line in keep.values():
            fh.write(line if line.endswith("\n") else line + "\n")
        futs = {ex.submit(_worker, p): p for p in paths}
        for fut in as_completed(futs):
            fh.write(json.dumps(fut.result()) + "\n"); fh.flush()
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(paths)}", file=sys.stderr)
    print(f"wrote {out} ({done} fitted in {time.time() - t0:.0f} s)", file=sys.stderr)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(limit=int(args[0]) if args else None, update="--update" in sys.argv)
