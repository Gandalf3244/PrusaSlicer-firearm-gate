"""Per-part primitive-composition signature (report: Choice 1, recommended option).

A signature is the full list of fitted primitives in a pose-independent form,
plus global shape descriptors. Fingerprints (Choice 2: compound dimension
sets) are defined over signatures, so a reference part is fitted once.

Pose independence: every quantity is either scalar (diameter, length, area,
angle) or a *relation* between two primitives (centre distance, axis angle),
never an absolute coordinate.
"""
from __future__ import annotations

import numpy as np
import trimesh

from .primitives import Segmentation, segment

MIN_HOLE_AREA = 3.0        # mm^2: ignore text/engraving-scale cylinders
MIN_COVERAGE = 0.45        # a hole must be at least a half-circle to carry a dimension


def _r(x, n=4):
    return round(float(x), n)


OBB_MAX_POINTS = 20000     # trimesh's exact OBB is quadratic in the hull size: 35 s for a 1.2 M
                           # face sphere. The OBB is descriptive only (not used in any decision).


def _obb_extents(mesh: trimesh.Trimesh) -> np.ndarray:
    if len(mesh.vertices) <= OBB_MAX_POINTS:
        return np.asarray(mesh.bounding_box_oriented.primitive.extents)
    pts = mesh.vertices[:: len(mesh.vertices) // OBB_MAX_POINTS + 1]
    _, ext = trimesh.bounds.oriented_bounds(pts)
    return np.asarray(ext)


def global_features(mesh: trimesh.Trimesh, describe: bool = True) -> dict:
    # describe=False: axis-aligned extents stand in for the oriented box (the
    # box is descriptive only; the checker skips its cost)
    obb = _obb_extents(mesh) if describe else np.asarray(mesh.extents)
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
