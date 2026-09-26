"""Compound fingerprints and constellation matching (report: Choice 2 + step 4/5).

A fingerprint is a small set of reference primitives (currently holes) plus
the pairwise geometric relations between them. Matching finds an injective
assignment of fingerprint holes to target holes such that every diameter and
every pairwise relation agrees within a tolerance Band. All relations are
invariant to rigid motion *and* to how far a hole extends along its own axis
(a fused part may thicken or cut a wall):

  * axis angle          angle between the two hole axes, folded to [0, 90] deg
  * axis separation     parallel axes  -> perpendicular distance between the lines
                        skew axes      -> closest-approach distance between the lines

Matching is exhaustive backtracking with pruning on partial assignments; a
fingerprint has <= ~10 holes and a target has <= ~60 measurable holes, so this
is instant. Every match carries per-constraint residuals so a pass/fail can be
explained.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np

from .tolerances import Band, TIGHT


# --------------------------------------------------------------------------- #
LINE_KINDS = {"hole", "boss", "cone"}
MIN_ELEMENT_D = 1.0      # mm
PLUG_OVERSIZE = 0.4      # mm: a hole stand-in (check's plug pass) may be this much wider than the hole
SLAB_KINDS = {"slot", "wall"}


def hole_relation(h1: dict, h2: dict) -> tuple[float, float]:
    """(angle deg, separation mm) between two elements, invariant to pose and
    to where along its own axis an element's reference point sits.

    line-line  axis angle; parallel -> perpendicular distance between axis
               lines, skew -> closest-approach distance
    slab-slab  normal angle; parallel -> distance between mid-planes, else 0
    line-slab  angle between axis and normal; axis lying in the slab plane
               (angle ~90) -> distance of the axis line from the mid-plane,
               otherwise 0 (a line crossing a slab has no invariant offset)
    """
    c1, a1 = np.asarray(h1["c"]), np.asarray(h1["a"])
    c2, a2 = np.asarray(h2["c"]), np.asarray(h2["a"])
    cosang = abs(float(a1 @ a2))
    ang = float(np.degrees(np.arccos(np.clip(cosang, 0, 1))))
    d = c2 - c1
    k1, k2 = h1.get("kind", "hole"), h2.get("kind", "hole")
    s1, s2 = k1 in SLAB_KINDS, k2 in SLAB_KINDS
    # symmetric "shared" axis for near-parallel pairs: projecting onto a1 vs
    # a2 differs by |d| sin(angle), which exceeds TIGHT tolerance for axes a
    # fraction of a degree apart and tens of mm long
    a_mean = a1 + a2 * (1.0 if a1 @ a2 >= 0 else -1.0)
    a_mean /= np.linalg.norm(a_mean)
    if not s1 and not s2:
        if ang < 1.0:
            perp = d - (d @ a_mean) * a_mean
            return ang, float(np.linalg.norm(perp))
        n = np.cross(a1, a2); n /= np.linalg.norm(n)
        return ang, abs(float(d @ n))
    if s1 and s2:
        return ang, (abs(float(d @ a_mean)) if ang < 1.0 else 0.0)
    n = a1 if s1 else a2                       # the slab normal
    return ang, (abs(float(d @ n)) if ang > 89.0 else 0.0)


def slab_elements(sig: dict, min_area: float = 20.0, d_range=(1.0, 60.0), max_out: int = 30) -> list[dict]:
    """Parallel plane pairs as elements. Facing normals -> 'slot' (channel /
    rail-gap width); outward normals -> 'wall' (tab / wall thickness). 'd' is
    the separation, 'a' the normal, 'c' a point on the mid-plane, 'area' the
    smaller face. Kept: the `max_out` largest pairs."""
    planes = [p for p in sig.get("planes", []) if "n" in p and p["area"] >= min_area]
    if len(planes) < 2:
        return []
    N = np.array([p["n"] for p in planes]); P = np.array([p["p"] for p in planes])
    # canonical normal (sign-folded) for grouping parallel planes
    flip = np.array([1 if n[np.argmax(np.abs(n))] > 0 else -1 for n in N])
    key = np.round(N * flip[:, None], 2)
    groups: dict[tuple, list[int]] = {}
    for i, k in enumerate(map(tuple, key)):
        groups.setdefault(k, []).append(i)
    out = []
    for idx in groups.values():
        for ii in range(len(idx)):
            for jj in range(ii + 1, len(idx)):
                i, j = idx[ii], idx[jj]
                if N[i] @ N[j] > -0.999:                 # need anti-parallel normals
                    continue
                sep = float((P[j] - P[i]) @ N[i])        # >0: i's normal points at j (facing)
                d = abs(sep)
                if not d_range[0] <= d <= d_range[1]:
                    continue
                kind = "slot" if sep > 0 else "wall"
                a = N[i] * flip[i]
                c = 0.5 * (P[i] + P[j])
                c = c - (c @ a) * a + ((P[i] @ a) + 0.5 * sep * (N[i] @ a)) * a   # exact mid-plane
                out.append({"kind": kind, "d": round(d, 3), "L": round(max(min(planes[i]["ext"]), min(planes[j]["ext"])), 2),
                            "cov": 1.0, "area": round(min(planes[i]["area"], planes[j]["area"]), 1),
                            "c": [round(float(x), 3) for x in c], "a": [round(float(x), 5) for x in a]})
    out.sort(key=lambda e: -e["area"])
    return out[:max_out]


def elements(sig: dict, min_cov: float = 0.9) -> list[dict]:
    """Axis-bearing primitives of a signature as uniform matching elements:
    kind in {hole, boss, cone, slot, wall}; 'd' is the diameter (cones:
    large-end diameter, plus 'half_deg'; slabs: separation); 'c','a' a point
    on the axis / mid-plane and its direction. Cached on the signature dict:
    slab extraction is O(planes^2) and match() is called hundreds of times
    per target."""
    cache = sig.setdefault("_elements_cache", {})
    if min_cov in cache:
        return cache[min_cov]
    out = []
    # sub-millimetre "holes" are engraving, fillets or fit artefacts, never
    # a feature that survives any remesh - not elements
    for h in sig["holes"]:
        if h["cov"] >= min_cov and h["d"] >= MIN_ELEMENT_D:
            out.append({**h, "kind": "hole"})
    for h in sig.get("bosses", []):
        if h["cov"] >= min_cov and h["d"] >= MIN_ELEMENT_D:
            out.append({**h, "kind": "boss"})
    for c in sig.get("cones", []):
        if c.get("cov", 1.0) >= min_cov and "c" in c and c["d_max"] >= MIN_ELEMENT_D:
            out.append({"kind": "cone", "d": c["d_max"], "half_deg": c["half_deg"], "L": c["L"],
                        "cov": c["cov"], "area": c["area"], "c": c["c"], "a": c["a"],
                        "concave": c["concave"]})
    out.extend(slab_elements(sig))
    cache[min_cov] = out
    return out


def same_kind(e1: dict, e2: dict, band: Band) -> bool:
    """e1: target element, e2: fingerprint element. Lengths are not compared
    (a fused part may thicken or cut a wall) except when the fingerprint
    element carries `min_L`: an anchor whose meaning depends on length (a
    bore, a grip-screw depth, a thread) must be at least that long in the
    target too."""
    k1, k2 = e1.get("kind", "hole"), e2.get("kind", "hole")
    if k1 != k2:
        # a cylinder from an open, multi-shell *reference* mesh has no
        # decidable concavity (27 library files). Only the reference side is
        # allowed to be lenient: an arbitrary broken input mesh must not get
        # extra ways to match, that would be a false-positive path.
        if not ({k1, k2} == {"hole", "boss"} and e2.get("amb")):
            return False
    if e1.get("plug"):
        # a pin fused into a hole leaves its protruding stub: a boss at least
        # as wide as the hole it fills (check.plug_standins)
        if not -band.diameter <= e1["d"] - e2["d"] <= PLUG_OVERSIZE + band.diameter or k2 != "hole":
            return False
    elif abs(e1["d"] - e2["d"]) > band.diameter:
        return False
    if e2.get("min_L") and e1.get("L", 0.0) < e2["min_L"] - band.length:
        return False
    if e1.get("kind") == "cone":
        return (abs(e1["half_deg"] - e2["half_deg"]) <= band.angle_deg
                and e1["concave"] == e2["concave"])
    return True


def dedupe_coaxial(holes: list[dict], band: Band = TIGHT) -> list[dict]:
    """Collapse coaxial elements of equal kind/diameter (the same pin bore
    appearing in two side walls) into one element, keeping the longest."""
    out: list[dict] = []
    for h in sorted(holes, key=lambda h: -h["L"]):
        dup = False
        for o in out:
            if same_kind(o, h, band):
                ang, sep = hole_relation(o, h)
                if ang <= band.angle_deg and sep <= band.distance:
                    dup = True; break
        if not dup:
            out.append(h)
    return out


# --------------------------------------------------------------------------- #
@dataclass
class Fingerprint:
    name: str
    source: str                         # reference path it was taken from
    holes: list[dict]                   # reference holes (d, L, cov, c, a)
    relations: dict = field(default_factory=dict)   # (i,j) -> (angle, sep)

    @classmethod
    def from_reference(cls, name: str, sig: dict, hole_idx: list[int]) -> "Fingerprint":
        hs = [sig["holes"][i] for i in hole_idx]
        fp = cls(name, sig["path"], hs)
        for i, j in itertools.combinations(range(len(hs)), 2):
            fp.relations[(i, j)] = hole_relation(hs[i], hs[j])
        return fp

    def describe(self) -> str:
        lines = [f"fingerprint '{self.name}' from {self.source}"]
        for i, h in enumerate(self.holes):
            lines.append(f"  H{i}: {h.get('kind','hole')} d={h['d']:.3f} L={h['L']:.2f}")
        for (i, j), (ang, sep) in self.relations.items():
            lines.append(f"  H{i}-H{j}: angle={ang:5.1f} deg  sep={sep:7.3f} mm")
        return "\n".join(lines)


@dataclass
class Match:
    fingerprint: str
    target: str
    assignment: list[int]               # target hole index per fingerprint hole
    residuals: dict                     # constraint -> (value, reference, tol)
    band: str

    @property
    def worst(self) -> float:
        """Largest residual as a fraction of its tolerance (<=1 means pass)."""
        return max(abs(v - r) / t for v, r, t in self.residuals.values())


def match(fp: Fingerprint, target: dict, band: Band = TIGHT, band_name: str = "TIGHT",
          min_cov: float = 0.9) -> list[Match]:
    """All injective assignments of fp.holes onto target['holes'] satisfying
    the diameter and pairwise-relation constraints within `band`."""
    cand_holes = elements(target, min_cov)
    # per fingerprint element: candidate target elements by kind + diameter
    cands = [[k for k, h in enumerate(cand_holes) if same_kind(h, fh, band)]
             for fh in fp.holes]
    if any(len(c) == 0 for c in cands):
        return []
    n = len(fp.holes)
    order = sorted(range(n), key=lambda i: len(cands[i]))   # most constrained first
    matches: list[Match] = []
    assign = [-1] * n

    def consistent(i: int, k: int) -> bool:
        for j in range(n):
            if assign[j] < 0 or j == i:
                continue
            key = (min(i, j), max(i, j))
            ref_ang, ref_sep = fp.relations[key]
            ang, sep = hole_relation(cand_holes[k], cand_holes[assign[j]])
            if abs(ang - ref_ang) > band.angle_deg or abs(sep - ref_sep) > band.distance:
                return False
        return True

    def rec(depth: int):
        if depth == n:
            res = {}
            for i in range(n):
                res[f"H{i}.d"] = (cand_holes[assign[i]]["d"], fp.holes[i]["d"], band.diameter)
            for (i, j), (ref_ang, ref_sep) in fp.relations.items():
                ang, sep = hole_relation(cand_holes[assign[i]], cand_holes[assign[j]])
                res[f"H{i}-H{j}.angle"] = (ang, ref_ang, band.angle_deg)
                res[f"H{i}-H{j}.sep"] = (sep, ref_sep, band.distance)
            matches.append(Match(fp.name, target["path"], assign.copy(), res, band_name))
            return
        i = order[depth]
        for k in cands[i]:
            if k in assign or not consistent(i, k):
                continue
            assign[i] = k
            rec(depth + 1)
            assign[i] = -1

    rec(0)
    # coaxial duplicates in the target (both side walls) produce equivalent
    # assignments; keep one per distinct set of axis lines
    uniq, seen = [], set()
    for m in matches:
        key = tuple(sorted(
            (cand_holes[k].get("kind", "hole"), round(cand_holes[k]["d"], 2)) + tuple(np.round(np.abs(cand_holes[k]["a"]), 2))
            + tuple(np.round(_axis_point(cand_holes[k]), 1)) for k in m.assignment))
        if key not in seen:
            seen.add(key); uniq.append(m)
    return uniq


def _axis_point(h: dict) -> np.ndarray:
    """Foot of the perpendicular from the origin onto the hole axis: a point
    that identifies the axis line independent of where the centre sits."""
    c, a = np.asarray(h["c"]), np.asarray(h["a"])
    return c - (c @ a) * a


def best_partial(fp: Fingerprint, target: dict, band: Band = TIGHT, band_name: str = "TIGHT",
                 min_size: int = 2) -> tuple[list[int], list[Match]]:
    """Largest subset of fingerprint holes that matches the target as a
    rigid constellation. Returns (subset indices, matches). Used to grade a
    fused part's partial containment of a reference and to diagnose which
    features of a reference are platform-shared vs design-specific."""
    n = len(fp.holes)
    for k in range(n, min_size - 1, -1):
        for subset in itertools.combinations(range(n), k):
            sub = Fingerprint(fp.name, fp.source, [fp.holes[i] for i in subset])
            for a, b in itertools.combinations(range(k), 2):
                sub.relations[(a, b)] = fp.relations[(subset[a], subset[b])]
            ms = match(sub, target, band, band_name)
            if ms:
                return list(subset), ms
    return [], []
