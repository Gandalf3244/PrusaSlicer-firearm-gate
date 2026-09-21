"""Step 3: tolerance bands per dimension type.

Derived from data/analysis (steps 1-2), not from drawings, because the
reference database *is* the STL library: a designer's print-clearance choice
(e.g. 7.00 mm for a nominal 0.250" pin) is part of that design's identity,
and different designers make different choices.

Measured error budget (see data/analysis_step2.txt and step-1 runs):
  * fit/tessellation error on CAD-native STL   : radial rms <= 0.001 mm,
    diameters reproducible to < 0.001 mm across mm-vs-inch re-exports.
  * designer clearance offsets from nominal    : +0.00 .. +0.65 mm, in
    discrete steps as small as 0.03 mm (6.35 vs 6.38 vs 6.48 mm).
  * same nominal feature across designers      : p10-p90 spread 0.02 mm (FCG
    pins, everyone uses 5/32") up to 0.65 mm (takedown pins).

Two tiers. TIGHT is for CAD-native input (vertices exactly on surfaces) and
is small enough to keep the 6.35 / 6.38 designer choices apart. LOOSE is for
meshes that have been remeshed / repaired / decimated, where vertices drift
off the true surface; it merges neighbouring clearance choices, so a LOOSE
match should be reported as platform-level rather than design-level.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    diameter: float        # mm, +/- on cylinder / cone diameters
    length: float          # mm, +/- on cylinder length along axis
    distance: float        # mm, +/- on centre-to-centre distances
    angle_deg: float       # deg, +/- on axis-axis and half angles
    coverage: float        # +/- on angular coverage fraction


TIGHT = Band(diameter=0.02, length=0.10, distance=0.05, angle_deg=0.5, coverage=0.05)
LOOSE = Band(diameter=0.12, length=0.50, distance=0.30, angle_deg=2.0, coverage=0.10)

# Diameters shared by many models (step 2 collision table). A single hole at
# one of these sizes is never a fingerprint on its own; it needs a compound
# relation (distance/angle to another primitive) before it carries identity.
# Regenerate with: python -m pipeline.similarity  -> data/analysis/hole_diameter_collisions.csv
GENERIC_HOLE_MIN_MODELS = 5


def is_generic(d_mm: float, collisions) -> bool:
    """`collisions` is the DataFrame from similarity.diameter_collisions."""
    hit = collisions[(collisions.d_mm - d_mm).abs() <= LOOSE.diameter]
    return bool(len(hit) and (hit.n_models >= GENERIC_HOLE_MIN_MODELS).any())
