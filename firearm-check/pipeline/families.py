"""Machine families (firearm platforms): platform-level evidence.

Family patterns are constellations of round elements (and at most one wall / slot)
that recur across several models of one platform and never occur in another (the AR-15 hammer/trigger
pin pair is the canonical one). They are mined from the reference library by
the development pipeline (Drive project "Gun 3d model detection",
`python -m pipeline.families`) and shipped here as data/family_patterns.json;
this module only loads and applies them.
"""
from __future__ import annotations

import itertools
import json
from collections import Counter
from pathlib import Path

from .calibers import platform_pair_match
from .fingerprint import Fingerprint, elements, hole_relation, match
from .tolerances import LOOSE

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def load_family_patterns(path: Path = DATA / "family_patterns.json") -> list[Fingerprint]:
    fps = []
    if not path.exists():
        return fps
    for fam, pats in json.load(path.open()).items():
        for i, p in enumerate(pats):
            fp = Fingerprint(f"{fam}#{i}", p["source"], p["holes"])
            fp.relations = {tuple(int(x) for x in k.split("-")): tuple(v) for k, v in p["relations"].items()}
            fp.family, fp.n_models = fam, p["n_models"]
            fps.append(fp)
    return fps


def platform_pairs(elems: list[dict]) -> list[str]:
    """Known platform pin pairs (calibers.PLATFORM_PAIRS) among these holes: generic
    diameters whose relation is firearm-specific - the AR hammer + trigger pins are
    0.154" holes 0.899" apart in every lower that takes mil-spec parts, whoever drew it."""
    out = []
    for a, b in itertools.combinations(elems, 2):
        if a["kind"] == b["kind"] == "hole":
            ang, sep = hole_relation(a, b)
            name = platform_pair_match(a, b, ang, sep)
            if name:
                out.append(name)
    return out


def classify_family(sig: dict, patterns: list[Fingerprint]) -> dict[str, int]:
    """family -> number of its patterns present (LOOSE)."""
    return {fam: len(ms) for fam, ms in family_matches(sig, patterns).items()}


def family_matches(sig: dict, patterns: list[Fingerprint]) -> dict[str, list[list[float]]]:
    """family -> per pattern present, the diameters of the target elements it
    matched (most patterns first)."""
    hits: dict[str, list[list[float]]] = {}
    els = None
    for fp in patterns:
        ms = match(fp, sig, LOOSE, "LOOSE")
        if ms:
            els = els if els is not None else elements(sig, 0.9)
            hits.setdefault(fp.family, []).append([els[k]["d"] for k in ms[0].assignment])
    return dict(sorted(hits.items(), key=lambda kv: -len(kv[1])))
