"""Unit normalization: the reference library mixes mm and inch exports.

All downstream geometry is in millimetres. STL carries no unit metadata, so we
infer it from scale: no firearm part in this library is smaller than ~12 mm in
its largest dimension, and none is larger than ~12 inches, so the largest
bounding-box extent separates the two hypotheses cleanly in almost all cases.
Ambiguous files (12 <= max extent <= 20) are decided by a second heuristic:
inch-native CAD has plane spacings that fall on multiples of 1/32", mm-native
CAD on multiples of 0.5 mm. `OVERRIDES` pins any file the heuristics get wrong.
"""
from __future__ import annotations

import numpy as np

INCH = 25.4
AMBIG_LO, AMBIG_HI = 12.0, 20.0

# rel path -> "in" | "mm" | "cm"
OVERRIDES: dict[str, str] = {
    # 15.9 x 11.7 x 2.5 "mm" with 51 planar facets and 29 cones: a C96
    # receiver modelled in centimetres (159 x 117 x 25 mm)
    "Pistols/Mauser_C96_Receiver-bolo/STL/Receiver.stl": "cm",
}
UNIT_SCALE = {"in": INCH, "mm": 1.0, "cm": 10.0}


def _grid_score(values: np.ndarray, step: float, tol: float) -> float:
    """Fraction of values lying within `tol` of a multiple of `step`."""
    if len(values) == 0:
        return 0.0
    frac = np.abs(values / step - np.round(values / step)) * step
    return float(np.mean(frac < tol))


def infer_scale(mesh, rel_path: str | None = None) -> float:
    """Return the multiplier that converts this mesh's coordinates to mm."""
    if rel_path in OVERRIDES:
        return UNIT_SCALE[OVERRIDES[rel_path]]
    ext = float(np.max(mesh.extents))
    if ext < AMBIG_LO:
        return INCH
    if ext > AMBIG_HI:
        return 1.0
    # ambiguous band: compare how well pairwise plane offsets snap to each grid
    v = mesh.vertices
    vals = np.concatenate([v[:, 0], v[:, 1], v[:, 2]])
    vals = vals - vals.min()
    s_in = _grid_score(vals, 1 / 32, 2e-4)
    s_mm = _grid_score(vals, 0.5, 5e-3)
    return INCH if s_in > s_mm else 1.0


def load_mesh(path, rel_path: str | None = None):
    """Load an STL as one mesh in millimetres with consistent, outward face
    winding (see prepare_mesh)."""
    import trimesh
    return prepare_mesh(trimesh.load(path, force="mesh", process=True), rel_path)


def prepare_mesh(mesh, rel_path: str | None = None, scale: float | None = None):
    """Repair winding and bring an already loaded mesh to millimetres. 27
    library files (e.g. `1911_Slide_Model.stl`, 23 % of its faces flipped)
    have inconsistent winding, which turns holes into bosses for the
    concavity test; `trimesh.repair.fix_normals` propagates a consistent
    orientation over face adjacency and points it outward. `scale` (units
    known from the caller, e.g. the slicer) skips unit inference."""
    import trimesh
    mesh.metadata["concavity_ambiguous"] = False
    if not mesh.is_winding_consistent or (mesh.is_watertight and mesh.volume < 0):
        trimesh.repair.fix_normals(mesh)
        # an open mesh made of several shells (the 1911 slide: 31 shells, 7188
        # open edges) has no defined inside: after repair each shell is
        # consistently wound but hole-vs-boss stays a guess. Flag it so the
        # matcher accepts either kind for this mesh's cylinders.
        mesh.metadata["concavity_ambiguous"] = not mesh.is_watertight
    mesh.apply_scale(infer_scale(mesh, rel_path) if scale is None else scale)
    return mesh
