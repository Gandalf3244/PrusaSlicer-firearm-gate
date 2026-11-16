"""Could a fingerprint match be chance? Candidate counts on the target.

A constellation fingerprint is a few round elements (kind + diameter) and
their pairwise relations. Matching it means finding target elements of the
right kind and diameter, then checking the relations. The more candidate
elements the target offers per fingerprint element, the more tuples are
tried, and the likelier one of them satisfies the relations by chance: a
marquee letter with 40 bulb holes of 3 mm offers 14^3 = 2744 triples to an
AR mag well's three 3 mm holes and one of them fits at LOOSE (Thingi10K,
data/analysis_chance.txt).

So a match is *weak* when it is a weak shape at LOOSE (fingerprint.weak_shape:
only M3-size holes, a stepped round feature, a pair of equal holes - one or two
numbers of identity: pins, bolts and brackets have them; mcu/gcheck.c has always
done this), or when its candidate count T (the product, over the
fingerprint's elements, of the target elements same_kind accepts) is above
both a floor and C times the count the fingerprint has on its own reference
part (`cands_ref`, stored in fingerprints.json by `annotate`): a part with 50
identical screw holes legitimately offers many candidates to its own
fingerprints, and so does a decimated copy of it. A weak match only
corroborates - it counts towards a part when another fingerprint of the same
part also fires (detect), never alone.

Fitted on the TUNE halves of Thingi10K / ABC (scripts/chance_eval.py), measured
on the holdout halves: data/analysis_chance.txt.

    python -m pipeline.chance      # (re)annotate data/fingerprints.json with cands_ref
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .fingerprint import elements, same_kind, weak_shape
from .tolerances import LOOSE, TIGHT, Band

FLOOR = {"TIGHT": 16, "LOOSE": 30}   # candidate tuples any fingerprint may have without corroboration
REF_FACTOR = 2.0                     # ... or this many times its count on its own part
LOOSE_WEAK_SHAPES = 15               # fingerprint.weak_shape flags that only corroborate at LOOSE (all)

ROOT = Path(__file__).resolve().parents[1]


def candidates(holes: list[dict], sig: dict, band: Band, min_cov: float = 0.9) -> int:
    """Number of target element tuples that pass the kind + diameter test of
    match() (before relations): the product of per-element candidate counts."""
    el = elements(sig, min_cov)
    return math.prod(sum(same_kind(e, fh, band) for e in el) for fh in holes)


def is_weak(fp, sig: dict, band_name: str) -> bool:
    """A match of `fp` on `sig` at band `band_name` that may be chance."""
    if getattr(fp, "screened", False):
        return True
    if band_name == "LOOSE" and _shape(fp) & LOOSE_WEAK_SHAPES:
        return True
    ref = (getattr(fp, "cands_ref", None) or {}).get(band_name)
    if ref is None:
        return False
    t = candidates(fp.holes, sig, LOOSE if band_name == "LOOSE" else TIGHT)
    return t > max(FLOOR[band_name], REF_FACTOR * ref)


def _shape(fp) -> int:
    if not hasattr(fp, "_weak_shape"):
        fp._weak_shape = weak_shape(fp.holes, fp.relations)
    return fp._weak_shape


def annotate(records: list[dict], sigs: dict[str, dict]) -> int:
    """Set `cands_ref` = {"TIGHT": T, "LOOSE": T} on every fingerprint variant
    (primary and alternates) from its reference part's signature. Returns the
    number of variants annotated."""
    n = 0
    for r in records:
        sig = sigs.get(r["path"])
        if sig is None:
            continue
        for v in [r] + r.get("alternates", []):
            v["cands_ref"] = {"TIGHT": candidates(v["holes"], sig, TIGHT),
                              "LOOSE": candidates(v["holes"], sig, LOOSE)}
            n += 1
    return n


def main():
    from .similarity import load
    path = ROOT / "data" / "fingerprints.json"
    recs = json.load(path.open())
    n = annotate(recs, {r["path"]: r for r in load()})
    json.dump(recs, path.open("w"), indent=0)
    print(f"annotated {n} fingerprint variants of {len(recs)} parts in {path}")


if __name__ == "__main__":
    main()
