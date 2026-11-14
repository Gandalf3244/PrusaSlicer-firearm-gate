"""Was this part resized? Judging weak evidence at a part's original size.

The weak rules - a single bore diameter, LOOSE (lineage) constellations,
family patterns - were tuned so that no non-gun object fires at its own size
(negative set 0/432). A metric part resized by 2-5 % (shrink compensation, a
slicer's scale field) walks generic dimensions into their bands: a MINI
z-carriage's 8.2 mm rod hole becomes a "7.62x39 bore", Voron joints match an
MP5 stock adapter at LOOSE (scripts/resize_cut.py --negatives).

Such a part gives itself away: divided by the scale it was resized by, its
dimensions are round again (8.2, 3.4, 5.4 mm ...). A search over every scale
finds spurious fits on gun parts, which have few dimensions and are often
inch designs, so only the scales the evidence itself suggests are tried: those
that make the matched features round on the 0.1 mm grid. At such a scale the
rest of the part must be round too, far more than as given (on the mm or a
0.005 in grid - an inch design as given is not resized). Then the evidence is
asked again at the original size (resized_signature): a resized derivative of
a gun part still matches there, a resized printer part does not - it was
tested at its own size.
"""
from __future__ import annotations

import numpy as np

IN = 25.4
RANGE = (0.94, 1.06)       # resizes considered
GRID = 0.1                 # mm: metric design dimensions (8.2 rod clearance, 3.4, 5.4 ...)
INCH_GRID = 0.005 * IN     # a part this round in inches as given is not a resized metric part
SNAP = 0.003               # mm: a value within this of the grid is round
DISTINCT = 0.02            # mm: values closer than this are one dimension (a .22 barrel's 6.137 ..
                           # 6.141 mm features made a wrong scale look 73 % round)
MIN_VALUES = 8             # dimensions needed to judge the whole part ...
FEW_VALUES = 6             # ... or this many when FEW_SCORE of them are round (a Voron
FEW_SCORE = 0.8            #     y-bumper: 6 dimensions, all round at x1/0.95)
MIN_SCORE = 0.4            # share of the part's dimensions round at the scale ...
MIN_GAIN = 0.25            # ... and this much more than as given (chance: ~0.06)


def distinct(vals) -> np.ndarray:
    """Sorted values with near-equal ones (< DISTINCT apart) merged: each
    dimension counts once, however many features carry it."""
    v = np.sort(np.asarray(vals, float))
    if len(v) == 0:
        return v
    keep = np.concatenate([[True], np.diff(v) >= DISTINCT])
    return v[keep]


def part_values(sig: dict, exclude=None) -> np.ndarray:
    """Hole / boss diameters and wall / slot widths: the dimensions a designer typed."""
    from .fingerprint import slab_elements
    vals = [h["d"] for h in sig.get("holes", []) + sig.get("bosses", [])
            if h.get("cov", 0) >= 0.9 and h["d"] >= 1.0 and h is not exclude]
    vals += [e["d"] for e in slab_elements(sig)]
    return distinct(vals)


def round_share(v: np.ndarray, s: float, grid: float = GRID) -> float:
    if len(v) == 0:
        return 0.0
    q = v / s / grid
    return float(np.mean(np.abs(q - np.round(q)) * grid <= SNAP))


def _round(d: float, s: float) -> bool:
    q = d / s / GRID
    return abs(q - round(q)) * GRID <= SNAP


def candidate_scales(d: float) -> list[float]:
    """Scales in RANGE at which `d` is a round size, the original size excluded."""
    lo, hi = RANGE
    out = []
    for k in range(int(np.ceil(d / hi / GRID)), int(np.floor(d / lo / GRID)) + 1):
        s = d / (k * GRID)
        if abs(s - 1) > SNAP / max(d, 1.0):
            out.append(s)
    return out


def resized_scales(sig: dict, matched: list[float], values: np.ndarray | None = None) -> list[float]:
    """Scales the part may have been resized by, suggested by the dimensions
    `matched` (the features the evidence rests on - a scale at which one of
    them is round; fitted countersinks and threads among them need not be)
    and confirmed by the whole part being round at s (MIN_SCORE / FEW_SCORE,
    MIN_GAIN over as given)."""
    v = part_values(sig) if values is None else values
    if not matched or len(v) < FEW_VALUES:
        return []
    need = MIN_SCORE if len(v) >= MIN_VALUES else FEW_SCORE
    base = max(round_share(v, 1.0), round_share(v, 1.0, INCH_GRID))
    out = []
    for s in sorted({round(s, 6) for d in matched for s in candidate_scales(d)}):
        sh = round_share(v, s)
        if sh >= need and sh - base >= MIN_GAIN:
            out.append(s)
    return out


def resized_signature(sig: dict, s: float) -> dict:
    """`sig` with every length divided by s: the part at its original size."""
    f = 1.0 / s

    def cyl(h):
        return {**h, "d": h["d"] * f, "L": h.get("L", 0.0) * f,
                **({"c": [x * f for x in h["c"]]} if "c" in h else {}),
                **({"area": h["area"] * f * f} if "area" in h else {})}
    out = {k: v for k, v in sig.items() if k != "_elements_cache"}
    out["holes"] = [cyl(h) for h in sig.get("holes", [])]
    out["bosses"] = [cyl(h) for h in sig.get("bosses", [])]
    out["cones"] = [{**c, **{k: c[k] * f for k in ("d_max", "d_min", "L") if k in c},
                     **({"c": [x * f for x in c["c"]]} if "c" in c else {}),
                     **({"area": c["area"] * f * f} if "area" in c else {})} for c in sig.get("cones", [])]
    out["planes"] = [{**p, **({"p": [x * f for x in p["p"]]} if "p" in p else {}),
                      "area": p["area"] * f * f, **({"ext": [x * f for x in p["ext"]]} if "ext" in p else {})}
                     for p in sig.get("planes", [])]
    return out
