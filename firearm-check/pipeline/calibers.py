"""Firearm-specific dimension priors.

Used by the curation step to decide whether a fingerprint element is a
*gun-specific* dimension (a bore matching a bullet diameter, a receiver
interface such as the AR buffer-tube thread, a known platform pin pair) or a
*generic* one (a standard drill / metric / fractional size that any bracket
or enclosure could carry).

Bullet diameters and cartridge lengths: sportsmans.com ammo caliber chart
(2026-09). Magazine widths: table supplied by the project owner. Bore
diameters in printed parts run slightly over bullet diameter (groove vs bore,
print clearance), so BORE_TOL is positive-only but stays below the gap to the
next standard drill size wherever possible.
"""
from __future__ import annotations

import numpy as np

IN = 25.4

# name -> bullet diameter (mm). Restricted to calibers that occur in printed
# firearms; hunting-rifle calibers from the chart (.243, .257, .270, 7mm,
# .338, .375 ...) were only ever matching 1/4" and 7 mm pin bores, so they
# are left out to avoid coincidental "bore" anchors.
BULLETS: dict[str, float] = {
    ".22 LR": 0.2255 * IN, ".223 / 5.56 / 5.7x28": 0.224 * IN,
    "6mm (Reprringer/Pentagun)": 6.0,
    ".308 / 300 BLK": 0.308 * IN, "7.62x39": 0.310 * IN,
    "9mm / .380": 0.355 * IN, ".38 / .357": 0.357 * IN,
    ".40 S&W / 10mm": 0.400 * IN, ".44 Mag": 0.429 * IN, ".45 ACP": 0.451 * IN,
    "12 gauge": 0.729 * IN, "26.5mm flare": 26.5,
}
BORE_TOL_UNDER = 0.20    # mm: printed bores run from bullet dia - 0.2 (tight .22 smoothbores at 0.220")
BORE_TOL_OVER = 0.50     #     to bullet dia + 0.5 (clearance smoothbores, e.g. 6.2 mm for .22)
BORE_METRIC_TOL = 0.06   # a bore diameter that *is* a metric rod size (8.0 for .308) is refused ...
BORE_METRIC_CLEARANCE = 0.30   # ... as is the clearance fit of a linear-rod size (8.0 .. 8.3)
ROD_SIZES = (6.0, 8.0, 10.0, 12.0, 16.0, 20.0)   # smooth-rod diameters in printers / machines
BORE_MIN_LD = 4.0        # a bore is a long hole: length >= 4 x diameter ...
BORE_MIN_L = 25.0        # ... and at least 25 mm (shortest barrel in the library)

# magazine-well / magazine-body widths (mm), from the owner's table. Kept as a
# *diagnostic* only: magazines vary by manufacturer and are excluded from
# identification, and well cavities in this library rarely have flat walls.
MAGAZINE_WIDTHS: dict[str, tuple[float, float]] = {
    "single-stack pistol": (0.50 * IN, 0.55 * IN),      # 12.7-14.0
    "double-stack pistol": (0.80 * IN, 0.90 * IN),      # 20.3-22.9
    "rifle (STANAG/AK/AR-10)": (0.90 * IN, 1.12 * IN),  # 22.9-28.4
}
MAG_TOL = 1.0            # mm slack on either side (printed well clearance)

# magazine front-to-back depth ~ cartridge overall length + 1..8 mm feed-lip /
# wall clearance. OALs from the caliber chart (in).
CARTRIDGE_OAL_IN = {".22 LR": 1.000, ".380": 0.984, "9mm": 1.169, ".40": 1.135,
                    ".45 ACP": 1.275, "10mm": 1.260, "5.7x28": 1.594, "7.62x39": 2.205,
                    ".223/5.56/300BLK": 2.260, ".308/AR-10": 2.800}
MAG_DEPTH_RANGES = [(oal * IN + 1.0, oal * IN + 8.0) for oal in CARTRIDGE_OAL_IN.values()]

# receiver interfaces with published, distinctive dimensions (mm):
# (min dia, max dia, min length). Length matters: a 1/4" through-hole in a
# 3 mm bracket is not a grip-screw boss, and a thread is a long cylinder.
INTERFACES: dict[str, tuple[float, float, float]] = {
    "AR buffer tube thread 1-3/16-16 (minor..major)": (28.4, 30.3, 8.0),
    "AR pistol grip screw 1/4-28 (tap..clearance)": (5.1, 6.6, 14.0),
    "1/2-28 muzzle thread": (11.5, 12.8, 8.0),
    "5/8-24 muzzle thread": (14.7, 15.95, 8.0),
    "AR receiver extension OD": (29.0, 30.0, 20.0),
}

# Known platform constellations: pairs of generic-diameter holes whose
# *relation* is firearm-specific. (d_lo, d_hi) for both holes, axis angle,
# axis separation range in mm. Measured on the library (AR pins: 22.838-22.849).
PLATFORM_PAIRS = [
    {"name": "AR-15/AR-10 hammer+trigger pins (0.154\")", "d": (3.85, 4.05), "angle": 0.0,
     "sep": (22.75, 22.95)},
    {"name": "AR-15 takedown+pivot pins (0.250\")", "d": (6.3, 7.05), "angle": 0.0,
     "sep": (161.0, 163.5)},          # 6.375" nominal between pivot and takedown pin centres
]

# standard sizes any mechanical object may carry: drill fractions, metric
# clearance holes and common CAD fillet radii. A hole at one of these is
# generic unless a relation to a gun-specific element rescues it.
GENERIC_DIAMETERS = sorted(set(
    [f * IN for f in np.arange(1 / 16, 1.0001, 1 / 32)] +
    [float(x) for x in (1.5, 2, 2.5, 3, 3.2, 3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 20, 22, 25)] +
    [3.3, 4.2, 6.8, 8.5, 10.2] +                        # M4..M12 tap drills (4.2 is also the usual printed M4 clearance)
    [2 * r for r in (0.5, 1, 1.5, 2, 2.5, 3, 4, 5)]      # fillet radii -> partial cylinders
))
# judged at LOOSE resolution (+ a printed-clearance allowance): 5.08 mm (0.2")
# or 3.43 mm are "specific" only on paper - remeshed input at LOOSE tolerance
# cannot tell them from 5.0 / 3.5, and neither can a designer's clearance
GENERIC_TOL = 0.15
METRIC_SIZES = [float(x) for x in (1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 20, 22, 25)]


def bore_match(d_mm: float) -> str | None:
    """Bullet-diameter match for a *long* hole. Metric rod sizes are refused
    (an 8 mm linear-rod bore in a printer carriage is >= 25 mm long too)."""
    # refuse metric rod sizes and their clearance fits (an 8.2 mm x 54 mm hole
    # is a printer's 8 mm smooth-rod bore, found on the negative set)
    if any(abs(d_mm - m) <= BORE_METRIC_TOL for m in METRIC_SIZES):
        return None
    if any(m <= d_mm <= m + BORE_METRIC_CLEARANCE for m in ROD_SIZES):
        return None
    names = [name for name, b in BULLETS.items() if b - BORE_TOL_UNDER <= d_mm <= b + BORE_TOL_OVER]
    return " or ".join(names) if names else None      # neighbouring calibers overlap (.44 / .45)


def magazine_match(width_mm: float) -> str | None:
    for name, (lo, hi) in MAGAZINE_WIDTHS.items():
        if lo - MAG_TOL <= width_mm <= hi + MAG_TOL:
            return name
    return None


def magazine_depth_match(depth_mm: float) -> bool:
    return any(lo <= depth_mm <= hi for lo, hi in MAG_DEPTH_RANGES)


def interface_match(d_mm: float, length_mm: float = 0.0) -> str | None:
    """A receiver interface by diameter band and minimum length. A diameter
    that is also a round metric size (5.5, 6.0 ...) is refused: two M6-deep
    holes 10 mm apart are a printer bracket, not an AR grip-screw boss
    (negative-set finding, data/analysis_negatives.txt)."""
    if any(abs(d_mm - m) <= GENERIC_TOL for m in METRIC_SIZES):
        return None
    for name, (lo, hi, min_L) in INTERFACES.items():
        if lo <= d_mm <= hi and length_mm >= min_L:
            return name
    return None


def platform_pair_match(e1: dict, e2: dict, angle_deg: float, sep_mm: float) -> str | None:
    for pp in PLATFORM_PAIRS:
        if (pp["d"][0] <= e1["d"] <= pp["d"][1] and pp["d"][0] <= e2["d"] <= pp["d"][1]
                and abs(angle_deg - pp["angle"]) <= 1.0 and pp["sep"][0] <= sep_mm <= pp["sep"][1]):
            return pp["name"]
    return None


def is_generic_diameter(d_mm: float) -> bool:
    return any(abs(d_mm - g) <= GENERIC_TOL for g in GENERIC_DIAMETERS)


BORE_MIN_FILL = 0.3      # patch area / (pi d L): a coil spring's inner envelope is ~0.1


def classify_diameter(d_mm: float, kind: str, length_mm: float = 0.0, area_mm2: float | None = None) -> tuple[str, str]:
    """-> (class, note) with class in {bore, interface, generic, specific}.
    Only a long hole (barrel-like) can be a bore; a pin hole that happens to be
    a bullet diameter is generic. With `area_mm2` the bore must also be a real
    surface, not the sparse inner envelope of a coil spring."""
    if kind == "hole" and length_mm >= max(BORE_MIN_L, BORE_MIN_LD * d_mm):
        b = bore_match(d_mm)
        if b and (area_mm2 is None or area_mm2 >= BORE_MIN_FILL * np.pi * d_mm * length_mm):
            return "bore", b
    i = interface_match(d_mm, length_mm)
    if i:
        return "interface", i
    if is_generic_diameter(d_mm):
        return "generic", ""
    return "specific", ""


BORE_MIN_SPAN = 0.84     # bore + coaxial chamber / part length along the bore axis


def _coaxial(h: dict, e: dict, ang_max: float = 2.0, dist_max: float = 0.5) -> bool:
    a, b = np.asarray(h["a"], float), np.asarray(e["a"], float)
    if abs(float(a @ b)) < np.cos(np.radians(ang_max)):
        return False
    d = np.asarray(e["c"], float) - np.asarray(h["c"], float)
    return float(np.linalg.norm(d - (d @ a) * a)) <= dist_max


def axial_span(h: dict, holes: list[dict], mesh) -> float:
    """Length covered by hole `h` and the holes coaxial with it (chamber,
    throat), as a fraction of the part's length along the hole axis."""
    a, c = np.asarray(h["a"], float), np.asarray(h["c"], float)
    t = (np.asarray(mesh.vertices) - c) @ a
    extent = float(t.max() - t.min())
    if extent <= 0:
        return 0.0
    ivs = sorted((float((np.asarray(o["c"], float) - c) @ a) - o["L"] / 2,
                  float((np.asarray(o["c"], float) - c) @ a) + o["L"] / 2)
                 for o in [h] + [o for o in holes if o is not h and _coaxial(h, o)])
    span, lo, hi = 0.0, *ivs[0]
    for a0, b0 in ivs[1:]:
        if a0 > hi:
            span += hi - lo; lo, hi = a0, b0
        else:
            hi = max(hi, b0)
    return (span + hi - lo) / extent


def bore_candidates(sig: dict) -> list[dict]:
    """Long holes (>= 25 mm and 4 diameters, not a metric rod size) at a
    printed-gun bullet diameter - the diameter test of bore_evidence alone."""
    out = []
    for h in sig.get("holes", []):
        if h.get("cov", 0) < 0.9:
            continue
        cls, note = classify_diameter(h["d"], "hole", h.get("L", 0.0), h.get("area"))
        if cls == "bore":
            out.append({"caliber": note, "d": h["d"], "L": h["L"], "hole": h})
    return out


def bore_evidence(sig: dict, mesh=None) -> list[dict]:
    """Part-type-level evidence independent of any reference design: a barrel
    bore, whatever the design around it. Two features, both always present on
    a barrel: a long hole at a printed-gun bullet diameter (bore_candidates),
    and that hole - with its coaxial chamber - running the whole length of the
    part along its axis (>= BORE_MIN_SPAN). Every library barrel the diameter
    test finds spans >= 0.86 of its length; a pistol grip's screw hole, an FCG
    pin bore or a stock's buffer channel is a long hole of bullet diameter
    that ends inside the part (0.2 - 0.8), and is no longer reported.
    `mesh` is the part the signature was fitted on (same coordinates); the
    span cannot be measured without it, so no bore is reported then."""
    if mesh is None:
        return []
    holes = sig.get("holes", [])
    out = []
    for b in bore_candidates(sig):
        span = axial_span(b.pop("hole"), holes, mesh)
        if span >= BORE_MIN_SPAN:
            out.append({**b, "span": round(span, 3)})
    return out
