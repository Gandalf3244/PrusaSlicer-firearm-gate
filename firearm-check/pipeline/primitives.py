"""Segment a triangle mesh into fitted primitives: planes, cylinders, cones.

Approach (deterministic, threshold-driven so every decision is auditable):

1. Coplanar facets: connected faces whose shared-edge dihedral angle is below
   `coplanar_deg`. On a tessellated cylinder each quad (2 triangles) becomes
   its own tiny facet; on a real flat face the whole face becomes one facet.
2. Curved membership: a facet belongs to a curved surface when most of its
   boundary length is shared with facets at a *small but non-zero* angle
   (below `smooth_deg`). A true plane's neighbours sit at sharp angles.
3. Curved patches: connected components over adjacent curved facets whose
   dihedral angle is below `smooth_deg`. Planes act as barriers, so a slot
   (two flats + two half-cylinders) still splits into four primitives.
4. Fit each curved patch. Face normals of a cylinder lie in a plane through
   the origin (n . a = 0); those of a cone lie in an offset plane
   (n . a = sin(half_angle)). So one PCA over the normals gives the axis for
   both, and the offset separates them. Radius/apex come from the patch
   vertices, which sit exactly on the true surface (tessellation chords are
   inside it, so face centroids would bias the radius low).
5. Concavity: a cylinder is a hole when its normals point toward the axis,
   a boss when they point away. Holes are the dimension carriers the report
   cares about most, so this flag is first-class.

Noise handling (added after the robustness benchmark, scripts/robustness.py):

* The vertex noise level sigma is estimated from the mesh itself (residuals
  of its largest flat regions). CAD-native STL gives sigma ~ 0 and every
  threshold below collapses to its original value, so native results are
  unchanged.
* The coplanar test is per edge: the dihedral angle two faces can show from
  vertex noise alone is sigma / (triangle height), so the threshold scales
  with it. Facets that merged too far are caught by a planarity residual.
* Circle / cone fits use only *crease* vertices (vertices on an edge with a
  non-zero dihedral angle). Vertices interior to a planar facet were added by
  a remesher and lie on the chord plane, inside the true surface; using them
  biases diameters low by the sagitta.
* Revolved-profile splitting smooths the per-face n.axis offsets before gap
  clustering, with a gap that scales with the normal noise.
* Adjacent coaxial cylinders (same radius) and adjacent cones (same axis,
  half-angle, apex) are merged and refitted, so a surface shattered by noise
  or a seam reports as one primitive with the full coverage.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components


# --------------------------------------------------------------------------- #
# result types
# --------------------------------------------------------------------------- #
@dataclass
class Plane:
    normal: np.ndarray
    point: np.ndarray            # a point on the plane (area-weighted centroid)
    area: float
    extents: np.ndarray          # 2D bounding extents in the plane (sorted desc)
    faces: np.ndarray = field(repr=False)

    kind: str = "plane"


@dataclass
class Cylinder:
    axis: np.ndarray             # unit vector
    center: np.ndarray           # point on axis at mid-length
    radius: float
    length: float                # extent of the patch along the axis
    coverage: float              # fraction of full circle covered (0..1]
    concave: bool                # True = hole / inner surface
    rms: float                   # radial residual of inlier vertices (mm)
    inliers: float               # fraction of patch vertices on the cylinder
    area: float
    faces: np.ndarray = field(repr=False)

    kind: str = "cylinder"

    @property
    def diameter(self) -> float:
        return 2 * self.radius


@dataclass
class Cone:
    axis: np.ndarray             # unit vector, pointing from small to large radius
    apex: np.ndarray
    half_angle_deg: float        # angle between axis and surface
    r_min: float
    r_max: float
    length: float
    coverage: float
    concave: bool
    rms: float
    area: float
    faces: np.ndarray = field(repr=False)

    kind: str = "cone"


@dataclass
class Freeform:
    area: float
    faces: np.ndarray = field(repr=False)
    kind: str = "freeform"


@dataclass
class Segmentation:
    planes: list[Plane]
    cylinders: list[Cylinder]
    cones: list[Cone]
    freeform: list[Freeform]
    total_area: float
    noise: float = 0.0           # estimated vertex noise sigma (mm)

    def summary(self) -> str:
        ncyl_in = sum(c.concave for c in self.cylinders)
        return (f"{len(self.planes)} planes, {len(self.cylinders)} cylinders "
                f"({ncyl_in} holes), {len(self.cones)} cones, "
                f"{len(self.freeform)} freeform, noise {self.noise:.4f} mm")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _components(n: int, pairs: np.ndarray) -> np.ndarray:
    """Connected-component label per node given an edge list. Labels are
    numbered by each component's smallest node, exactly as scipy's
    connected_components numbers them; this min-label propagation with
    pointer jumping avoids building a sparse matrix per call, which cost
    more than the labelling on the small graphs the fitter passes."""
    if len(pairs) == 0:
        return np.arange(n)
    a, b = pairs[:, 0], pairs[:, 1]
    lab = np.arange(n)
    while True:
        la, lb = lab[a], lab[b]
        diff = la != lb
        if not diff.any():
            break
        lo, hi = np.minimum(la[diff], lb[diff]), np.maximum(la[diff], lb[diff])
        np.minimum.at(lab, hi, lo)
        while True:                      # pointer jumping: every node to its root
            nxt = lab[lab]
            if np.array_equal(nxt, lab):
                break
            lab = nxt
    return np.unique(lab, return_inverse=True)[1]


def _fit_circle_2d(p: np.ndarray) -> tuple[np.ndarray, float]:
    """Algebraic (Kasa) circle fit: returns (center, radius)."""
    x, y = p[:, 0], p[:, 1]
    A = np.column_stack([x, y, np.ones_like(x)])
    b = x * x + y * y
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = sol[0] / 2, sol[1] / 2
    r = np.sqrt(max(sol[2] + cx * cx + cy * cy, 0.0))
    return np.array([cx, cy]), float(r)


def _robust_circle(p: np.ndarray, iters: int = 4, k: float = 3.0):
    """Circle fit with iterative outlier trimming (fillets/chamfers that get
    merged into a cylinder patch show up as a few off-radius vertices).
    Returns (center, radius, inlier_mask, inlier_rms)."""
    mask = np.ones(len(p), bool)
    for _ in range(iters):
        c, r = _fit_circle_2d(p[mask])
        res = np.linalg.norm(p - c, axis=1) - r
        mad = np.median(np.abs(res[mask] - np.median(res[mask]))) * 1.4826
        new = np.abs(res) <= max(k * mad, 1e-3 * max(r, 1.0))
        if new.sum() < 3 or np.array_equal(new, mask):
            mask = new if new.sum() >= 3 else mask
            break
        mask = new
    c, r = _fit_circle_2d(p[mask])
    res = np.linalg.norm(p[mask] - c, axis=1) - r
    return c, float(r), mask, float(np.sqrt(np.mean(res ** 2)))


def _basis_perp(a: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    h = np.array([1.0, 0, 0]) if abs(a[0]) < 0.9 else np.array([0, 1.0, 0])
    u = np.cross(a, h); u /= np.linalg.norm(u)
    v = np.cross(a, u)
    return u, v


def _angular_coverage(theta: np.ndarray) -> float:
    """Fraction of the circle spanned by the given angles: one minus the
    largest angular gap between consecutive samples, with the typical sample
    spacing added back so a fully tessellated circle reports 1.0."""
    t = np.sort(np.unique(np.round(theta, 6)))
    if len(t) < 2:
        return 0.0
    gaps = np.diff(np.append(t, t[0] + 2 * np.pi))
    step = np.median(gaps)
    return float(min(1.0, (2 * np.pi - gaps.max() + step) / (2 * np.pi)))


# --------------------------------------------------------------------------- #
# noise estimate
# --------------------------------------------------------------------------- #
K_NOISE = 3.0            # thresholds are k-sigma
COP_MAX_DEG = 5.0        # cap on the noise-widened coplanar threshold
WILD_TILT = 0.1          # rad: a face whose noise tilt exceeds this is orientation-less
NOISE_FLOOR = 1e-3       # mm: below this the mesh is treated as CAD-native
FRACTION_TIE = 1e-9      # slack on boundary-length fractions (pose-independent ties)


def _plane_residual(verts: np.ndarray) -> np.ndarray:
    """Signed distance of points to their best-fit (SVD) plane."""
    c = verts.mean(0)
    _, _, vt = np.linalg.svd(verts - c, full_matrices=False)
    return (verts - c) @ vt[2]


def estimate_noise(mesh: trimesh.Trimesh, adj: np.ndarray, ang: np.ndarray,
                   coplanar_deg: float = 0.5, min_faces: int = 30, n_regions: int = 20) -> float:
    """Vertex noise sigma (mm) of a mesh.

    1. CAD-native / remeshed / decimated meshes contain many *exactly*
       coplanar face pairs (fan triangulations of flat faces, quad
       diagonals on cylinders): >= 20 % of edges at zero dihedral angle in
       this library. Noise destroys that: < 0.5 % at sigma = 0.005 mm. So a
       zero-angle fraction above 5 % means sigma = 0 and every threshold stays
       at its validated native value.
    2. Otherwise each edge's dihedral angle implies a displacement
       ang / sqrt(1/h_a^2 + 1/h_b^2) (h = triangle height over the shared
       edge); its lower quartile tracks sigma (calibrated on jittered library
       parts: 1.3 x q25 within +-30 % for sigma 0.005..0.05).
    3. That estimate is refined on the largest flat regions grown with the
       widened threshold: the robust scatter of their vertices about a fitted
       plane *is* the positional noise (lower quartile over regions, so a
       region that absorbed a fine cylinder does not inflate it).
    """
    if len(adj) == 0:
        return 0.0
    if float(np.mean(ang < 1e-4)) >= 0.05:
        return 0.0
    edges = mesh.face_adjacency_edges
    elen = np.linalg.norm(mesh.vertices[edges[:, 0]] - mesh.vertices[edges[:, 1]], axis=1)
    farea = mesh.area_faces
    h_a = np.maximum(2 * farea[adj[:, 0]] / np.maximum(elen, 1e-9), 1e-9)
    h_b = np.maximum(2 * farea[adj[:, 1]] / np.maximum(elen, 1e-9), 1e-9)
    se = ang / np.sqrt(1 / h_a ** 2 + 1 / h_b ** 2)
    s0 = 1.3 * float(np.quantile(se, 0.25))
    if s0 <= 1e-6:
        return 0.0
    cop_e = np.clip(K_NOISE * s0 * (1 / h_a + 1 / h_b), np.deg2rad(coplanar_deg), np.deg2rad(COP_MAX_DEG))
    lab = _components(len(mesh.faces), adj[ang < cop_e])
    counts = np.bincount(lab)
    big = [f for f in np.argsort(-counts)[:n_regions] if counts[f] >= min_faces]
    if not big:
        return s0
    sig = []
    for fid in big:
        vid = np.unique(mesh.faces[lab == fid])
        d = _plane_residual(mesh.vertices[vid])
        sig.append(1.4826 * np.median(np.abs(d - np.median(d))))
    # the edge-based estimate is within +-30 %; the region refinement can only
    # be fooled upward (a region that absorbed a whole cylinder), so clamp it
    return float(np.clip(np.quantile(sig, 0.25), 0.5 * s0, 1.25 * s0))


def _grow_planar_facets(mesh, facet_of, adj, wild_e, sigma, rms_k=2.5, max_k=4.0):
    """Merge adjacent facets whose vertices all lie on the seed facet's plane
    within the noise (rms <= rms_k sigma, 99th pct <= max_k sigma). Seeds are
    taken by decreasing area; a facet absorbed into a seed never seeds. The
    seed plane is refitted as it grows, so a fine cylinder can only be
    absorbed for the couple of steps whose sagitta is below the noise."""
    V = mesh.vertices
    F = mesh.faces
    n = facet_of.max() + 1
    area = np.bincount(facet_of, weights=mesh.area_faces, minlength=n)
    fa, fb = facet_of[adj[:, 0]], facet_of[adj[:, 1]]
    ok = fa != fb            # wild edges included: the distance test decides, not orientation
    nbrs: list[set] = [set() for _ in range(n)]
    for a, b in zip(fa[ok], fb[ok]):
        nbrs[a].add(b); nbrs[b].add(a)
    faces_of = [[] for _ in range(n)]
    for f, lab in enumerate(facet_of):
        faces_of[lab].append(f)
    verts_of = [np.unique(F[fl]) for fl in faces_of]
    label = np.arange(n)
    absorbed = np.zeros(n, bool)
    tol_rms, tol_max = rms_k * sigma, max_k * sigma
    for seed in np.argsort(-area):
        if absorbed[seed] or len(verts_of[seed]) < 3:
            continue
        pts = [verts_of[seed]]
        c, nrm = _fit_plane(V[verts_of[seed]])
        frontier = set(nbrs[seed]); seen = {seed}
        since_refit = 0
        while frontier:
            nb = frontier.pop()
            if nb in seen:
                continue
            seen.add(nb)
            if absorbed[nb]:
                continue
            d = (V[verts_of[nb]] - c) @ nrm
            if np.sqrt(np.mean(d * d)) > tol_rms or np.quantile(np.abs(d), 0.99) > tol_max:
                continue
            absorbed[nb] = True; label[nb] = seed
            pts.append(verts_of[nb]); frontier |= nbrs[nb]
            since_refit += 1
            if since_refit >= 8:
                c, nrm = _fit_plane(V[np.concatenate(pts)]); since_refit = 0
    # relabel to a dense range
    _, dense = np.unique(label, return_inverse=True)
    return dense[facet_of]


def _fit_plane(pts: np.ndarray):
    c = pts.mean(0)
    _, _, vt = np.linalg.svd(pts - c, full_matrices=False)
    return c, vt[2]


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def segment(mesh: trimesh.Trimesh, noise: float | None = None, **kw) -> Segmentation:
    """Segment a mesh. The noise level is estimated from the mesh; when it is
    non-zero the mesh is segmented both as CAD-native (sigma = 0) and with the
    noise-adaptive thresholds, and the segmentation that explains more surface
    area with planes / cylinders / cones wins. Native meshes that happen to
    lack exactly-coplanar face pairs (organic grips, some remeshed exports)
    would otherwise be degraded by thresholds tuned for noise they do not have."""
    if noise is not None:
        return _segment_once(mesh, noise=noise, **kw)
    m = mesh.copy(); m.merge_vertices()
    sigma = estimate_noise(m, m.face_adjacency, m.face_adjacency_angles)
    native = _segment_once(m, noise=0.0, **kw)
    if sigma < NOISE_FLOOR:
        return native
    noisy = _segment_once(m, noise=sigma, **kw)
    return _union(native, noisy, sigma)


def _usable_round(seg: Segmentation):
    return [c for c in seg.cylinders + seg.cones if c.coverage >= 0.45 and c.area >= 3.0]


def _union(native: Segmentation, noisy: Segmentation, sigma: float) -> Segmentation:
    """Combine the two segmentations of a noisy mesh. Finely tessellated holes
    survive the native path (many faces per step, each dihedral step far above
    the noise) while its planes shatter; the noise path recovers the planes
    but can lose small holes to wild-sliver merges. So: planes from whichever
    path explains more plane area, and the union of both paths' cylinders and
    cones with coaxial duplicates removed (larger patch wins)."""
    pa = sum(p.area for p in native.planes if p.area >= 20.0)
    pb = sum(p.area for p in noisy.planes if p.area >= 20.0)
    planes = noisy.planes if pb > pa else native.planes
    tol_r, tol_pos = max(0.02, 3 * sigma), max(0.05, 3 * sigma)
    cyl: list[Cylinder] = []
    for c in sorted(native.cylinders + noisy.cylinders, key=lambda c: -c.area):
        dup = False
        for k in cyl:
            if (k.concave == c.concave and abs(k.radius - c.radius) <= tol_r
                    and abs(k.axis @ c.axis) >= np.cos(np.deg2rad(1.0))
                    and _line_distance(k.center, k.axis, c.center, c.axis) <= tol_pos
                    and abs((c.center - k.center) @ k.axis) <= 0.5 * (k.length + c.length)):
                dup = True; break
        if not dup:
            cyl.append(c)
    cones: list[Cone] = []
    for c in sorted(native.cones + noisy.cones, key=lambda c: -c.area):
        dup = False
        for k in cones:
            if (k.concave == c.concave and abs(k.half_angle_deg - c.half_angle_deg) <= 1.0
                    and abs(k.axis @ c.axis) >= np.cos(np.deg2rad(1.0))
                    and np.linalg.norm(k.apex - c.apex) <= max(0.3, 3 * tol_pos)):
                dup = True; break
        if not dup:
            cones.append(c)
    free = noisy.freeform if len(_usable_round(noisy)) >= len(_usable_round(native)) else native.freeform
    return Segmentation(planes, cyl, cones, free, native.total_area, sigma)


def _explained(seg: Segmentation) -> float:
    """Surface area fraction explained by *usable* primitives: planes big
    enough to form slab elements, cylinders / cones with enough arc to carry
    a diameter. Fragments do not score, so a shattered plane or a bore in
    pieces loses to the intact version."""
    a = sum(p.area for p in seg.planes if p.area >= 20.0)
    a += sum(c.area for c in seg.cylinders if c.coverage >= 0.45 and c.area >= 3.0)
    a += sum(c.area for c in seg.cones if c.coverage >= 0.45 and c.area >= 3.0)
    return a / max(seg.total_area, 1e-9)


def _segment_once(
    mesh: trimesh.Trimesh,
    coplanar_deg: float = 0.5,
    smooth_deg: float = 35.0,
    min_plane_area: float = 1.0,       # mm^2
    min_patch_faces: int = 6,
    cyl_rel_rms: float = 0.01,         # radial rms / radius
    cyl_abs_rms: float = 0.05,         # mm
    min_inliers: float = 0.6,          # fraction of patch vertices that must fit
    noise: float | None = None,        # vertex noise sigma (mm); None = estimate
    merge: bool = True,                # merge adjacent coaxial pieces after fitting
) -> Segmentation:
    mesh = mesh.copy()
    mesh.merge_vertices()
    F = len(mesh.faces)
    adj = mesh.face_adjacency                     # (E,2) face pairs
    ang = mesh.face_adjacency_angles              # dihedral angle per pair
    edges = mesh.face_adjacency_edges             # (E,2) vertex idx of shared edge
    elen = np.linalg.norm(mesh.vertices[edges[:, 0]] - mesh.vertices[edges[:, 1]], axis=1)
    fnorm = mesh.face_normals
    farea = mesh.area_faces
    fcent = mesh.triangles_center

    cop = np.deg2rad(coplanar_deg)
    smo = np.deg2rad(smooth_deg)
    if noise is None:
        noise = estimate_noise(mesh, adj, ang)
    sigma = float(noise)
    # per-face normal tilt variance from vertex noise: sigma^2 * sum_i 1/h_i^2
    # (h_i = height over the edge opposite vertex i)
    tilt2 = np.zeros(F)
    if sigma > 1e-5:
        tri = mesh.triangles
        for i in range(3):
            e = np.linalg.norm(tri[:, (i + 1) % 3] - tri[:, (i + 2) % 3], axis=1)
            h = 2 * farea / np.maximum(e, 1e-9)
            tilt2 += (sigma / np.maximum(h, 1e-6)) ** 2
    # "wild" faces: slivers whose normal is noise (tilt > ~6 deg). They carry
    # no orientation information, so they neither split nor join anything:
    # each is attached to one neighbour (longest shared edge with a reliable
    # face) and its other edges are ignored everywhere below.
    wild = np.sqrt(tilt2) > WILD_TILT
    wild_e = wild[adj[:, 0]] | wild[adj[:, 1]]
    attach = np.zeros(len(adj), bool)
    if wild.any():
        # per wild face, its best edge: longest, preferring a reliable neighbour
        # (vectorised: one pass over both sides of every adjacency row)
        rows = np.concatenate([np.arange(len(adj))] * 2)
        face = np.concatenate([adj[:, 0], adj[:, 1]])
        other = np.concatenate([adj[:, 1], adj[:, 0]])
        sel = wild[face]
        rows, face, other = rows[sel], face[sel], other[sel]
        score = elen[rows] + np.where(wild[other], 0.0, 1e9)
        order = np.lexsort((score, face))            # by face, then score ascending
        # highest-score row per face = last entry of each face group
        grp_end = np.flatnonzero(np.r_[face[order][1:] != face[order][:-1], True])
        attach[rows[order[grp_end]]] = True

    # per-edge coplanar threshold: a vertex displaced by sigma tilts its face
    # by sigma / height, so noisy slivers get a wider band (capped)
    if sigma > 1e-5:
        h_a = np.maximum(2 * farea[adj[:, 0]] / np.maximum(elen, 1e-9), 1e-9)
        h_b = np.maximum(2 * farea[adj[:, 1]] / np.maximum(elen, 1e-9), 1e-9)
        cop_e = np.clip(K_NOISE * sigma * (1 / h_a + 1 / h_b), cop, np.deg2rad(COP_MAX_DEG))
    else:
        cop_e = np.full(len(adj), cop)

    # 1. coplanar facets
    facet_of = _components(F, adj[((ang < cop_e) & ~wild_e) | attach])
    if sigma > 1e-5:
        # 1b. under noise, thin strips (triangle heights ~ 30 sigma) fragment
        # because their normals are unreliable even though their vertices lie
        # within sigma of one plane. Re-grow facets by *distance to plane*,
        # largest facet first, so orientation noise cannot break a flat face.
        facet_of = _grow_planar_facets(mesh, facet_of, adj, wild_e, sigma)
    n_facets = facet_of.max() + 1
    facet_area = np.bincount(facet_of, weights=farea, minlength=n_facets)

    # 2. curved membership per facet, from boundary-length fractions
    fa, fb = facet_of[adj[:, 0]], facet_of[adj[:, 1]]
    between = (fa != fb) & ~wild_e
    smooth_nonzero = between & (ang >= cop_e) & (ang < smo)
    tot = np.zeros(n_facets); smth = np.zeros(n_facets)
    for side in (fa, fb):
        np.add.at(tot, side[between], elen[between])
        np.add.at(smth, side[smooth_nonzero], elen[smooth_nonzero])
    # exact ties are common (a flat quad with two smooth and two sharp sides of
    # equal length) and would be decided by float rounding of the pose: 0.5 in
    # the file's frame, 0.5000000000000001 rotated. A tie is flat, in every pose.
    curved_facet = np.divide(smth, tot, out=np.zeros(n_facets), where=tot > 0) > 0.5 + FRACTION_TIE
    # 2b. planarity: a facet that merged across a fine cylinder under the
    # widened threshold is not flat -> curved
    if sigma > 1e-5:
        tol = max(K_NOISE * sigma, 1e-3)
        for fid in np.flatnonzero(~curved_facet):
            faces = np.flatnonzero(facet_of == fid)
            if len(faces) < 4:
                continue
            d = _plane_residual(mesh.vertices[np.unique(mesh.faces[faces])])
            if 1.4826 * np.median(np.abs(d - np.median(d))) > tol:
                curved_facet[fid] = True
    curved_face = curved_facet[facet_of]

    # crease vertices: on an edge with a non-zero dihedral angle (true CAD
    # vertices) or on a mesh boundary. Interior vertices of a flat facet were
    # added by a remesher and sit on chord planes, inside curved surfaces.
    crease = np.zeros(len(mesh.vertices), bool)
    crease[edges[(ang >= cop_e) & ~wild_e].ravel()] = True
    if not mesh.is_watertight:                # open edges are real CAD edges too
        be = mesh.edges_sorted[trimesh.grouping.group_rows(mesh.edges_sorted, require_count=1)]
        crease[be.ravel()] = True

    # 3. curved patches: smooth adjacency among curved faces only. Wild
    # slivers do not bridge patches (a sliver on the seam between a hole and
    # its chamfer would otherwise fuse them into one unfittable patch).
    keep = curved_face[adj[:, 0]] & curved_face[adj[:, 1]] & (ang < smo) & ~wild_e
    patch_of = _components(F, adj[keep])

    planes, cylinders, cones, freeform = [], [], [], []

    # planes: non-curved facets above area threshold
    for fid in np.flatnonzero(~curved_facet & (facet_area >= min_plane_area)):
        faces = np.flatnonzero(facet_of == fid)
        w = farea[faces]
        n = (fnorm[faces] * w[:, None]).sum(0); n /= np.linalg.norm(n)
        p = (fcent[faces] * w[:, None]).sum(0) / w.sum()
        verts = mesh.vertices[np.unique(mesh.faces[faces])]
        u, v = _basis_perp(n)
        pu, pv = (verts - p) @ u, (verts - p) @ v
        ext = np.sort([pu.max() - pu.min(), pv.max() - pv.min()])[::-1]
        planes.append(Plane(n, p, float(w.sum()), ext, faces))

    # curved patches -> cylinder / cone / freeform
    ctx = _FitContext(mesh, fnorm, farea, fcent, crease, tilt2, sigma,
                      rel_rms=cyl_rel_rms, abs_rms=cyl_abs_rms, min_inliers=min_inliers)
    for pid in np.unique(patch_of[curved_face]):
        faces = np.flatnonzero((patch_of == pid) & curved_face)
        if len(faces) < min_patch_faces:
            continue
        prims = _fit_patch(ctx, faces, adj, ang, smo, min_patch_faces)
        for prim in prims:
            {"cylinder": cylinders, "cone": cones, "freeform": freeform}[prim.kind].append(prim)

    if merge:
        cylinders, cones, freeform = _merge_coaxial(ctx, adj, cylinders, cones, freeform)
        cylinders = _merge_coaxial_arcs(cylinders)

    return Segmentation(planes, cylinders, cones, freeform, float(mesh.area), sigma)


SPLIT_ANGLES_DEG = (20.0, 12.0, 8.0, 5.0)


def _fit_patch(ctx, faces, adj, ang, smo, min_faces, depth: int = 0):
    """Fit a curved patch; when it is not one primitive, split it and fit the
    pieces (recursively):

    1. by n.axis offset - a smooth revolved profile (body -> shoulder -> neck)
       is constant on each cylinder / cone about the shared axis;
    2. by concavity about the common axis - a barrel's outer surface and its
       bore share the axis and the offset (both 0) but face opposite ways,
       and the end fillet that joins them has no offset gap to split on;
    3. by a stricter dihedral threshold - fillets and chamfer rings are
       tessellated in coarser angular steps than the large surfaces they
       join, so lowering the smoothness threshold peels them off.
    Pieces that never fit are returned as one Freeform."""
    farea = ctx.farea
    prims = _fit_curved(ctx, faces)
    if prims[0].kind != "freeform" or depth > 6:
        return prims
    for splitter in (_split_by_normal_offset, _split_by_concavity, _split_by_angle):
        pieces = splitter(ctx, faces, adj, ang, smo, min_faces)
        if len(pieces) <= 1:
            continue
        out, leftover = [], []
        for sub in pieces:
            for sp in _fit_patch(ctx, sub, adj, ang, smo, min_faces, depth + 1):
                (leftover if sp.kind == "freeform" else out).append(sp)
        if out:
            if leftover:
                lf = np.concatenate([f.faces for f in leftover])
                out.append(Freeform(float(farea[lf].sum()), lf))
            return out
    return prims


def _split_by_concavity(ctx, faces, adj, ang, smo, min_faces):
    """Split a patch about its revolution axis into outward-facing and
    inward-facing faces (boss vs hole), then by connectivity."""
    N, w = ctx.fnorm[faces], ctx.farea[faces]
    axis, _, evals = _revolution_axis(N, w)
    if evals[0] > 0.25 * evals[1]:
        return [faces]
    verts = ctx.mesh.vertices[np.unique(ctx.mesh.faces[faces])]
    u, v = _basis_perp(axis)
    c2, _ = _fit_circle_2d(np.column_stack([verts @ u, verts @ v]))
    centre = c2[0] * u + c2[1] * v
    radial = ctx.fcent[faces] - centre
    radial -= (radial @ axis)[:, None] * axis
    sign = np.einsum("ij,ij->i", radial, N) < 0
    if sign.all() or not sign.any():
        return [faces]
    inpatch = ctx.pairs_within(faces, adj)
    inpatch = inpatch[ang[inpatch] < smo]
    a = np.searchsorted(faces, adj[inpatch])
    same = sign[a[:, 0]] == sign[a[:, 1]]
    lab = _components(len(faces), a[same])
    return [faces[lab == L] for L in np.unique(lab) if np.sum(lab == L) >= min_faces]


def _split_by_angle(ctx, faces, adj, ang, smo, min_faces):
    """Connected components of the patch under the next stricter dihedral
    threshold below the one it was built with."""
    inpatch = ctx.pairs_within(faces, adj)
    a = np.searchsorted(faces, adj[inpatch]); e_ang = ang[inpatch]
    for th in SPLIT_ANGLES_DEG:
        if np.deg2rad(th) >= smo:
            continue
        lab = _components(len(faces), a[e_ang < np.deg2rad(th)])
        pieces = [faces[lab == L] for L in np.unique(lab) if np.sum(lab == L) >= min_faces]
        if len(pieces) > 1:
            # peel: the strict threshold applies to the whole patch, but the
            # recursion carries the same `smo`, so mark pieces by returning them
            return pieces
    return [faces]


class _FitContext:
    """Everything the per-patch fitters need, bundled so noise-aware
    thresholds travel with the mesh."""

    def __init__(self, mesh, fnorm, farea, fcent, crease, tilt2, sigma, rel_rms, abs_rms, min_inliers):
        self.mesh, self.fnorm, self.farea, self.fcent = mesh, fnorm, farea, fcent
        self.crease, self.tilt2, self.sigma = crease, tilt2, sigma
        self.rel_rms, self.abs_rms, self.min_inliers = rel_rms, abs_rms, min_inliers
        mz = mesh.metadata.get("measured_z") if isinstance(getattr(mesh, "metadata", None), dict) else None
        self.measured = None
        if mz is not None and len(mz):
            mz = np.sort(np.asarray(mz, dtype=float))
            vz = mesh.vertices[:, 2]
            j = np.clip(np.searchsorted(mz, vz), 1, len(mz) - 1)
            self.measured = np.minimum(np.abs(vz - mz[j - 1]), np.abs(vz - mz[j])) < 1e-6

    def pairs_within(self, faces, adj):
        """Indices (ascending) of the adjacency pairs with both faces in
        `faces` - the same rows as np.isin(adj[:, 0], faces) &
        np.isin(adj[:, 1], faces), in time proportional to the patch rather
        than the mesh (the recursive splitter calls this thousands of times)."""
        key = id(adj)
        if getattr(self, "_adj_key", None) != key:
            n = len(self.farea)
            ends = np.concatenate([adj[:, 0], adj[:, 1]])
            rows = np.concatenate([np.arange(len(adj))] * 2)
            order = np.argsort(ends, kind="stable")
            self._adj_rows = rows[order]
            self._adj_ptr = np.searchsorted(ends[order], np.arange(n + 1))
            self._adj_key, self._adj_ref = key, adj
            self._mark = np.zeros(n, dtype=bool)
        ptr, rows = self._adj_ptr, self._adj_rows
        lo, hi = ptr[faces], ptr[faces + 1]
        cnt = hi - lo
        if cnt.sum() == 0:
            return np.zeros(0, dtype=np.int64)
        start = np.repeat(lo - np.concatenate([[0], np.cumsum(cnt)[:-1]]), cnt)
        cand = rows[start + np.arange(cnt.sum())]
        self._mark[faces] = True
        both = self._mark[adj[cand, 0]] & self._mark[adj[cand, 1]]
        self._mark[faces] = False
        return np.unique(cand[both])

    def fit_vertices(self, faces):
        """Vertex ids used for fitting: crease vertices when the patch has
        enough of them, else all. A mesh reconstructed from G-code layers
        (pipeline.gcode_mesh) is measured only on its slice planes; when it
        says so (metadata "measured_z"), the fit uses the vertices on those
        planes and ignores the ones interpolated between them."""
        vid = np.unique(self.mesh.faces[faces])
        sel = vid[self.crease[vid]]
        sel = sel if len(sel) >= max(8, 0.2 * len(vid)) else vid
        if self.measured is not None:
            meas = sel[self.measured[sel]]
            if len(meas) >= max(8, 0.05 * len(sel)):
                return meas
        return sel

    @property
    def pos_tol(self):
        """Positional acceptance widened for noise."""
        return K_NOISE * self.sigma


def _revolution_axis(N: np.ndarray, w: np.ndarray):
    """Axis of a surface of revolution from its normals (PCA smallest
    component). Returns (axis, offset_mean, eigenvalues)."""
    nbar = (N * w[:, None]).sum(0) / w.sum()
    C = ((N - nbar) * w[:, None]).T @ (N - nbar) / w.sum()
    evals, evecs = np.linalg.eigh(C)
    return evecs[:, 0], float(nbar @ evecs[:, 0]), evals


def _make_cylinder(ctx: _FitContext, faces, vid, inl, axis, u, v, center2, r, rms, frac):
    """Build a Cylinder from robust-circle inliers (`vid` are the fitted
    vertex ids, `inl` their inlier mask). Faces touching an outlier vertex are
    returned separately as Freeform so their area is not credited."""
    mesh, fnorm, farea, fcent = ctx.mesh, ctx.fnorm, ctx.farea, ctx.fcent
    good_v = np.ones(len(mesh.vertices), bool); good_v[vid[~inl]] = False
    face_ok = good_v[mesh.faces[faces]].all(axis=1)
    cyl_faces, rest = faces[face_ok], faces[~face_ok]
    if len(cyl_faces) == 0:
        return [Freeform(float(farea[faces].sum()), faces)]
    verts = mesh.vertices[vid[inl]]
    q = np.column_stack([verts @ u, verts @ v]) - center2
    cov = _angular_coverage(np.arctan2(q[:, 1], q[:, 0]))
    t_all = verts @ axis                      # extent of the inlier vertices along the axis
    center = center2[0] * u + center2[1] * v + 0.5 * (t_all.min() + t_all.max()) * axis
    radial = fcent[cyl_faces] - center
    radial -= (radial @ axis)[:, None] * axis
    w = farea[cyl_faces]
    concave = float(np.sum(w * np.einsum("ij,ij->i", radial, fnorm[cyl_faces]))) < 0
    out = [Cylinder(axis, center, float(r), float(t_all.max() - t_all.min()), cov, bool(concave),
                    rms, frac, float(w.sum()), cyl_faces)]
    if len(rest):
        out.append(Freeform(float(farea[rest].sum()), rest))
    return out


def _smooth_on_patch(vals: np.ndarray, pairs: np.ndarray, iters: int) -> np.ndarray:
    """Average a per-face quantity over its patch neighbours `iters` times."""
    n = len(vals)
    for _ in range(iters):
        acc = vals.copy(); cnt = np.ones(n)
        np.add.at(acc, pairs[:, 0], vals[pairs[:, 1]]); np.add.at(cnt, pairs[:, 0], 1)
        np.add.at(acc, pairs[:, 1], vals[pairs[:, 0]]); np.add.at(cnt, pairs[:, 1], 1)
        vals = acc / cnt
    return vals


def _split_by_normal_offset(ctx: _FitContext, faces, adj, ang, smo, min_faces, gap: float = 0.03):
    """Split a curved patch into sub-patches of constant n.axis (1D gap
    clustering on sorted offsets), then by connectivity within each cluster.
    Under noise the per-face offsets are first smoothed over the patch and the
    gap widened to the residual scatter. Returns face-index arrays (pieces
    >= min_faces)."""
    fnorm, farea = ctx.fnorm, ctx.farea
    N, w = fnorm[faces], farea[faces]
    axis, _, evals = _revolution_axis(N, w)
    # if the normals do not have a single axis direction of low variance
    # (e.g. sphere/torus/generic freeform) there is nothing to split on
    if evals[0] > 0.25 * evals[1]:
        return [faces]
    off = N @ axis
    inpatch = ctx.pairs_within(faces, adj)
    inpatch = inpatch[ang[inpatch] < smo]
    a = np.searchsorted(faces, adj[inpatch])          # faces is sorted
    if ctx.sigma > 1e-5:
        off_s = _smooth_on_patch(off, a, 3)
        resid = off - off_s
        gap = max(gap, 2.0 * 1.4826 * float(np.median(np.abs(resid - np.median(resid)))))
        off = off_s
    order = np.argsort(off)
    breaks = np.flatnonzero(np.diff(off[order]) > gap)
    cluster = np.zeros(len(faces), int)
    cluster[order] = np.searchsorted(breaks, np.arange(len(faces)), side="right")
    if cluster.max() == 0:
        return [faces]
    same = cluster[a[:, 0]] == cluster[a[:, 1]]
    lab = _components(len(faces), a[same])
    return [faces[lab == L] for L in np.unique(lab) if np.sum(lab == L) >= min_faces]


def _fit_curved(ctx: _FitContext, faces):
    """Fit one curved patch. Returns a list of primitives (a cylinder may
    shed its outlier faces as a trailing Freeform)."""
    mesh, fnorm, farea, fcent = ctx.mesh, ctx.fnorm, ctx.farea, ctx.fcent
    rel_rms, abs_rms, min_inliers = ctx.rel_rms, ctx.abs_rms, ctx.min_inliers
    N = fnorm[faces]
    w = farea[faces]
    area = float(w.sum())
    vid = ctx.fit_vertices(faces)
    verts = mesh.vertices[vid]

    # PCA of (area-weighted) normals: smallest component = axis direction
    axis, offset, evals = _revolution_axis(N, w)   # offset = sin(half angle); 0 for cylinder
    # normals not lying on a circle of the unit sphere -> not a surface of
    # revolution about one axis. Under noise the smallest eigenvalue is the
    # normal-tilt variance, not zero.
    noise_ev = float((w * ctx.tilt2[faces]).sum() / w.sum())
    if evals[0] > 0.02 * max(evals[2], 1e-12) + K_NOISE * noise_ev:
        return [Freeform(area, faces)]

    u, v = _basis_perp(axis)
    t = verts @ axis
    p2 = np.column_stack([verts @ u, verts @ v])
    pos_tol = ctx.pos_tol

    if abs(offset) < 0.03 + 2 * np.sqrt(noise_ev):
        center2, r, inl, rms = _robust_circle(p2)
        frac = float(inl.mean())
        if r > 0 and frac >= min_inliers and rms <= max(rel_rms * r, abs_rms, pos_tol):
            return _make_cylinder(ctx, faces, vid, inl, axis, u, v, center2, r, rms, frac)
        # fall through: a very slight taper (e.g. a cartridge body, ~0.5 deg)
        # has offset ~ 0 but is not a cylinder; try the cone fit.

    # cone: radius grows linearly along the axis. Fit axis point first via
    # circle fit on the slice-normalised points, then r(t) = r0 + k t.
    center2, _ = _fit_circle_2d(p2)          # rough centre from all points
    d = np.linalg.norm(p2 - center2, axis=1)
    A = np.column_stack([t, np.ones_like(t)])
    (k, r0), *_ = np.linalg.lstsq(A, d, rcond=None)
    pred = A @ np.array([k, r0])
    rms = float(np.sqrt(np.mean((d - pred) ** 2)))
    if abs(k) < 1e-6 or rms > max(0.02 * d.mean(), 2 * abs_rms, pos_tol):
        return [Freeform(area, faces)]
    half = float(np.degrees(np.arctan(abs(k))))
    if half < 0.15:
        # numerically a cylinder whose vertices carry some off-radius outliers
        # (merged fillet faces); accept the robust-circle inliers as the cylinder.
        center2, r, inl, rms = _robust_circle(p2)
        return _make_cylinder(ctx, faces, vid, inl, axis, u, v, center2, r, rms, float(inl.mean()))
    if k < 0:                                  # orient axis toward growing radius
        axis, k, t = -axis, -k, -t
        r0 = float(d.mean() - k * t.mean())
    t_apex = -r0 / k
    apex = center2[0] * u + center2[1] * v + t_apex * axis
    rmin, rmax = float(pred.min()), float(pred.max())
    theta = np.arctan2(p2[:, 1] - center2[1], p2[:, 0] - center2[0])
    radial = fcent[faces] - apex
    radial -= (radial @ axis)[:, None] * axis
    concave = float(np.sum(w * np.einsum("ij,ij->i", radial, N))) < 0
    return [Cone(axis, apex, half, rmin, rmax, float(t.max() - t.min()),
                 _angular_coverage(theta), bool(concave), rms, area, faces)]


# --------------------------------------------------------------------------- #
# post-fit merging of shattered / seamed primitives
# --------------------------------------------------------------------------- #
def _line_distance(c1, a1, c2, a2) -> float:
    """Distance between two (near-)parallel axis lines."""
    d = c2 - c1
    a = a1 + a2 * (1.0 if a1 @ a2 >= 0 else -1.0); a /= np.linalg.norm(a)
    return float(np.linalg.norm(d - (d @ a) * a))


def _merge_coaxial_arcs(cylinders: list, d_tol: float = 0.12, pos_tol: float = 0.10) -> list:
    """Partial arcs on one axis line with overlapping extent and nearly the
    same diameter are one surface seen in pieces (a rifled bore's grooves, a
    bore interrupted by cross holes, a boss split by a seam). They are
    combined - not refitted, the pieces need not be one exact cylinder -
    into a single element with the area-weighted diameter and summed
    coverage. Full separate holes (two side walls) do not overlap in extent
    and are left alone."""
    out: list = []
    used = [False] * len(cylinders)
    order = sorted(range(len(cylinders)), key=lambda i: -cylinders[i].area)
    for i in order:
        if used[i]:
            continue
        A = cylinders[i]
        group = [A]; used[i] = True
        if A.coverage < 0.95:
            ta = A.center @ A.axis
            for j in order:
                if used[j]:
                    continue
                B = cylinders[j]
                # a short arc locates its axis poorly (centre error grows as the
                # arc shrinks), so the axis tolerance widens with 1 - coverage
                tol = pos_tol + 0.3 * (1.0 - min(A.coverage, B.coverage))
                if (B.concave != A.concave or B.coverage >= 0.95 or abs(B.radius - A.radius) > d_tol / 2
                        or abs(A.axis @ B.axis) < np.cos(np.deg2rad(1.0))
                        or _line_distance(A.center, A.axis, B.center, B.axis) > tol):
                    continue
                tb = B.center @ A.axis * (1.0 if A.axis @ B.axis >= 0 else -1.0)
                if abs(tb - ta) > 0.5 * (A.length + B.length):      # no extent overlap
                    continue
                group.append(B); used[j] = True
        if len(group) == 1:
            out.append(A); continue
        w = np.array([g.area for g in group])
        r = float((w * np.array([g.radius for g in group])).sum() / w.sum())
        ts = [(g.center @ A.axis * (1.0 if A.axis @ g.axis >= 0 else -1.0), g.length) for g in group]
        lo = min(t - L / 2 for t, L in ts); hi = max(t + L / 2 for t, L in ts)
        foot = A.center - (A.center @ A.axis) * A.axis
        center = foot + 0.5 * (lo + hi) * A.axis
        out.append(Cylinder(A.axis, center, r, float(hi - lo), float(min(1.0, sum(g.coverage for g in group))),
                            A.concave, float(max(g.rms for g in group)), float(min(g.inliers for g in group)),
                            float(w.sum()), np.concatenate([g.faces for g in group])))
    return out


def _merge_coaxial(ctx: _FitContext, adj, cylinders, cones, freeform):
    """Merge adjacent primitives that describe one surface: cylinders with the
    same axis line, radius and concavity; cones with the same axis, half-angle
    and apex. Merged groups are refitted on the union of their faces; if the
    refit does not come back as the same kind the originals are kept."""
    prims = [("cyl", i) for i in range(len(cylinders))] + [("cone", i) for i in range(len(cones))]
    if len(prims) < 2:
        return cylinders, cones, freeform
    F = len(ctx.mesh.faces)
    owner = np.full(F, -1)
    for p_i, (kind, i) in enumerate(prims):
        owner[(cylinders if kind == "cyl" else cones)[i].faces] = p_i
    oa, ob = owner[adj[:, 0]], owner[adj[:, 1]]
    touching = np.unique(np.sort(np.column_stack([oa, ob])[(oa >= 0) & (ob >= 0) & (oa != ob)], axis=1), axis=0)
    if len(touching) == 0:
        return cylinders, cones, freeform
    tol_r = max(0.02, ctx.pos_tol)
    tol_pos = max(0.05, ctx.pos_tol)
    parent = list(range(len(prims)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    for p, q in touching:
        kp, ip = prims[p]; kq, iq = prims[q]
        if kp != kq:
            continue
        if kp == "cyl":
            A, B = cylinders[ip], cylinders[iq]
            ok = (A.concave == B.concave and abs(A.radius - B.radius) <= tol_r
                  and abs(A.axis @ B.axis) >= np.cos(np.deg2rad(1.0))
                  and _line_distance(A.center, A.axis, B.center, B.axis) <= tol_pos)
        else:
            A, B = cones[ip], cones[iq]
            ok = (A.concave == B.concave and abs(A.half_angle_deg - B.half_angle_deg) <= 1.0
                  and abs(A.axis @ B.axis) >= np.cos(np.deg2rad(1.0))
                  and np.linalg.norm(A.apex - B.apex) <= max(0.3, 3 * tol_pos))
        if ok:
            parent[find(p)] = find(q)
    groups: dict[int, list[int]] = {}
    for p_i in range(len(prims)):
        groups.setdefault(find(p_i), []).append(p_i)
    if all(len(g) == 1 for g in groups.values()):
        return cylinders, cones, freeform
    new_cyl, new_cone, extra_free = [], [], []
    for g in groups.values():
        kind = prims[g[0]][0]
        src = cylinders if kind == "cyl" else cones
        members = [src[prims[p_i][1]] for p_i in g]
        if len(g) == 1:
            (new_cyl if kind == "cyl" else new_cone).append(members[0]); continue
        faces = np.sort(np.concatenate([m.faces for m in members]))
        refit = _fit_curved(ctx, faces)
        fitted = [p for p in refit if p.kind == ("cylinder" if kind == "cyl" else "cone")]
        if len(fitted) == 1:
            (new_cyl if kind == "cyl" else new_cone).append(fitted[0])
            extra_free += [p for p in refit if p.kind == "freeform"]
        else:
            (new_cyl if kind == "cyl" else new_cone).extend(members)
    return new_cyl, new_cone, freeform + extra_free
