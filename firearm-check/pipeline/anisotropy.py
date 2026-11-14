"""Per-axis scale read off the mesh itself.

A part stretched along an axis of its file (a per-axis scale in a mesh tool,
or in a slicer before the file reaches the checker) moves its fingerprint
dimensions by different amounts in different directions; the uniform scaled
pass (check.SCALE_RANGE) cannot absorb that. But the stretch is visible on
the part: every round cylinder becomes an elliptic one. Its generators stay
parallel (the fitted axis is still exact), and its cross-section's shape
depends linearly, to first order, on the log scales l = log(sx, sy, sz):

    cross-section quadric  Q ~ I - 2 U^T diag(l) U     (U: 2D basis normal to the axis)

so the ellipse fitted through each cylinder's vertices (CAD-native vertices
sit exactly on the stretched surface) gives two equations in l, and all
cylinders together give l up to a common factor - which the uniform scaled
pass then finds. A few Gauss-Newton rounds (undo the estimate, measure what
is left) remove the second-order error.

    axis_scales(mesh, seg) -> [(sx, sy, sz), ...]  candidate stretches, geometric mean 1
"""
from __future__ import annotations

import numpy as np

MIN_COVERAGE = 0.6        # partial arcs pin the ellipse poorly
MIN_VERTS = 8
MIN_FACES = 8
FIT_RMS = 2e-3            # rms algebraic residual of an ellipse fit (unit-scaled points)
AXIS_SPREAD = 2e-3        # a freeform patch is a generalised cylinder when its normals' least
                          # eigenvalue is below this share (they all lie normal to one direction)
RESID_MAX = 3e-4          # an equation this far off the joint solution is an outlier (a real oval)
MIN_AGREE = 1             # patches that must agree on the stretch for it to be reported
RANK_TOL = 0.2            # singular value of the measured rows below this: that direction is open
GAUGE_W = 1e3             # weight of a reading's exact constraint
MIN_STRETCH = 5e-4        # log-scale spread below this is no stretch (TIGHT is 0.05 mm per 100 mm)
MAX_STRETCH = 0.12        # beyond this the uniform / per-axis passes are not meant to look
MAX_DEV = 0.15            # |deviatoric| of one cross-section beyond this: a real oval / bad fit, not a stretch
ROUNDS = 3


def _basis(a: np.ndarray) -> np.ndarray:
    """2 x 3: an orthonormal basis of the plane normal to unit vector a."""
    t = np.eye(3)[int(np.argmin(np.abs(a)))]
    u = np.cross(a, t); u /= np.linalg.norm(u)
    return np.vstack([u, np.cross(a, u)])


def _conic(w: np.ndarray) -> np.ndarray | None:
    """Normalised 2x2 quadric (trace 2) of the ellipse through 2D points w,
    or None when they do not lie on one (rms algebraic residual > FIT_RMS)."""
    if len(w) < MIN_VERTS or not np.all(np.isfinite(w)):
        return None
    w = w - w.mean(axis=0)
    s = np.abs(w).max()
    if s <= 0:
        return None
    u, v = w[:, 0] / s, w[:, 1] / s
    M = np.column_stack([u * u, u * v, v * v, u, v])
    try:
        x, *_ = np.linalg.lstsq(M, np.ones(len(w)), rcond=None)
    except np.linalg.LinAlgError:
        return None
    if np.sqrt(np.mean((M @ x - 1) ** 2)) > FIT_RMS:
        return None
    A, B, C = x[:3]
    Q = np.array([[A, B / 2], [B / 2, C]])
    tr = A + C
    if tr <= 0 or np.linalg.det(Q) <= 0:
        return None
    return 2 * Q / tr


def _coverage(dirs: np.ndarray) -> float:
    """Fraction of 36 angular bins the 2D directions fall in."""
    ang = np.arctan2(dirs[:, 1], dirs[:, 0])
    return len(np.unique(np.floor((ang + np.pi) / (2 * np.pi) * 36).astype(int) % 36)) / 36


def _patches(mesh, seg) -> list[tuple[np.ndarray, np.ndarray]]:
    """(vertex ids, axis) of every round-ish generalised cylinder: fitted
    cylinders, and freeform patches whose normals all lie normal to one
    direction (a stretched hole no longer fits as a circular cylinder and
    is left freeform) and go most of the way round it."""
    out = []
    N = np.asarray(mesh.face_normals); W = np.asarray(mesh.area_faces)
    for kind, prims in (("cyl", seg.cylinders), ("free", seg.freeform)):
        for c in prims:
            f = np.asarray(c.faces)
            if len(f) < MIN_FACES:
                continue
            if kind == "cyl":
                if c.coverage < MIN_COVERAGE:
                    continue
                axis = np.asarray(c.axis, float)
            else:
                ok = np.all(np.isfinite(N[f]), axis=1)        # degenerate faces have no normal
                f = f[ok]
                if len(f) < MIN_FACES:
                    continue
                C = (N[f] * W[f, None]).T @ N[f]
                if not np.all(np.isfinite(C)) or np.trace(C) <= 0:
                    continue
                ev, evec = np.linalg.eigh(C)
                if ev[0] > AXIS_SPREAD * ev.sum():
                    continue
                axis = evec[:, 0]
            if not np.all(np.isfinite(axis)) or np.linalg.norm(axis) < 0.5:
                continue
            Nf = N[f][np.all(np.isfinite(N[f]), axis=1)]
            if len(Nf) < MIN_FACES or _coverage(Nf @ _basis(axis).T) < MIN_COVERAGE:
                continue
            vid = np.unique(mesh.faces[f])
            if len(vid) >= MIN_VERTS:
                out.append((vid, axis))
    return out


def _rows(V: np.ndarray, patches: list[tuple[np.ndarray, np.ndarray]]):
    """Linear equations in l (log scales) from each patch's cross-section."""
    rows, rhs, owner = [], [], []
    for n, (vid, axis) in enumerate(patches):
        U = _basis(axis)
        Q = _conic(V[vid] @ U.T)
        if Q is None:
            continue
        d1, d2 = (Q[0, 0] - Q[1, 1]) / 2, Q[0, 1]
        if max(abs(d1), abs(d2)) > MAX_DEV:
            continue
        u, v = U
        # Q ~ I - 2 (U L U^T - mean): (Q00 - Q11)/2 = -(u.L.u - v.L.v), Q01 = -2 u.L.v
        rows.append(-(u * u - v * v)); rhs.append(d1)
        rows.append(-2 * u * v); rhs.append(d2)
        owner += [n, n]
    return rows, rhs, owner


def _solve(patches, V0: np.ndarray, extra: list[np.ndarray]) -> tuple[np.ndarray, int, int] | None:
    """Gauss-Newton on the log scales with `extra` exact constraints (rows
    whose dot with l must be 0). Returns (l, rank of the measured rows,
    patches agreeing), or None when fewer than MIN_AGREE patches remain."""
    l = np.zeros(3)
    keep = np.ones(len(patches), bool)
    rank = 0
    for _ in range(ROUNDS + 6):
        inv = np.exp(-l)                       # undo the current estimate
        V = V0 * inv
        cur = []
        for vid, a in patches:
            a2 = a * inv; cur.append((vid, a2 / np.linalg.norm(a2)))
        rows, rhs, owner = _rows(V, cur)
        sel = [k for k, o in enumerate(owner) if keep[o]]
        if len({owner[k] for k in sel}) < MIN_AGREE:
            return None
        R = np.vstack([rows[k] for k in sel]); r = np.array([rhs[k] for k in sel])
        if not (np.all(np.isfinite(R)) and np.all(np.isfinite(r))):
            return None
        rank = int(np.sum(np.linalg.svd(R, compute_uv=False) > RANK_TOL))
        A = np.vstack([R, np.ones(3)] + [GAUGE_W * e for e in extra])
        b = np.concatenate([r, [0.0], -GAUGE_W * np.array([e @ l for e in extra])])
        dl, *_ = np.linalg.lstsq(A, b, rcond=None)
        # a patch whose cross-section disagrees with the joint solution is a
        # real oval (or a bad fit): left out, and the rest solved again. The
        # bar follows the median while the estimate is still first-order.
        res = np.abs(R @ dl - r)
        bar = max(RESID_MAX, 4 * float(np.median(res)))
        bad = {owner[k] for k, x in zip(sel, res) if x > bar}
        if bad:
            keep &= np.array([i not in bad for i in range(len(patches))])
            continue
        l = l + dl
        l -= l.mean()
        if np.abs(dl).max() < 1e-6:
            break
    return l, rank, int(keep.sum())


def axis_scales(mesh, seg) -> list[tuple[float, float, float]]:
    """Per-axis scales the mesh may have been stretched by, from the
    ellipticity of its round patches; [] when there is no measurable stretch.
    The cross-sections fix two of the three log scales' differences when
    their axes point two ways (one answer). When they all point one way, one
    difference is left open and each reading in which a single axis is
    stretched is offered (a per-axis scale stretches one axis, or two by the
    same amount); a reading that disagrees with the measured ellipses is
    dropped."""
    patches = _patches(mesh, seg)
    if len(patches) < MIN_AGREE:
        return []
    V0 = np.asarray(mesh.vertices, float)
    if not np.all(np.isfinite(V0)):
        return []
    first = _solve(patches, V0, [])
    if first is None:
        return []
    l, rank, _ = first
    if rank == 0:
        return []
    cands = [l] if rank >= 2 else []
    if rank == 1:
        for k in range(3):
            i, j = [x for x in range(3) if x != k]
            e = np.zeros(3); e[i], e[j] = 1, -1          # axes i and j stretched alike
            sol = _solve(patches, V0, [e])
            if sol is not None:
                cands.append(sol[0])
    out = []
    for l in cands:
        dev = float(np.ptp(l))
        if MIN_STRETCH <= dev <= MAX_STRETCH and \
                not any(np.abs(np.log(np.array(o)) - l).max() < MIN_STRETCH for o in out):
            out.append(tuple(float(x) for x in np.exp(l)))
    return out
