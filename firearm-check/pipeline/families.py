"""Machine families (firearm platforms): platform-level evidence.

Family patterns are constellations of round elements that recur across several
models of one platform and never occur in another (the AR-15 hammer/trigger
pin pair is the canonical one). They are mined from the reference library by
the development pipeline (Drive project "Gun 3d model detection",
`python -m pipeline.families`) and shipped here as data/family_patterns.json;
this module only loads and applies them.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .fingerprint import Fingerprint, match
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


def classify_family(sig: dict, patterns: list[Fingerprint]) -> dict[str, int]:
    """family -> number of its patterns present (LOOSE)."""
    hits: Counter = Counter()
    for fp in patterns:
        if match(fp, sig, LOOSE, "LOOSE"):
            hits[fp.family] += 1
    return dict(hits.most_common())
