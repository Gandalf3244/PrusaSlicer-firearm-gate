"""Step 5 (first half): mine a discriminating compound fingerprint per
reference part.

For each reference part, find the smallest constellation of its holes that
matches (at TIGHT) no part belonging to any *other model* in the library.
Search order is pairs -> triples -> quads, always restricted to elements whose
diameters co-occur in the fewest other parts, so the common case is a handful
of constellation checks per part rather than an exhaustive subset search.

Outputs data/fingerprints.json: one entry per part with either a fingerprint
(hole indices, size, the reference relations) or a reason it could not be
made discriminating ("shared": an identical constellation exists in another
model - typically a derivative design or a reused part; "too_few_holes").
"""
from __future__ import annotations

import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from .fingerprint import Fingerprint, dedupe_coaxial, elements, match
from .similarity import load
from .tolerances import TIGHT, Band

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
MAX_SIZE = 4
MIN_COV = 0.9


def _diam_index(rows: list[dict], band: Band):
    """diameter bucket -> set of row indices having a hole of that diameter."""
    step = band.diameter
    idx: dict[tuple, set[int]] = defaultdict(set)
    for r_i, r in enumerate(rows):
        for h in elements(r, MIN_COV):
            b = int(round(h["d"] / step))
            kinds = ("hole", "boss") if h.get("amb") and h["kind"] in ("hole", "boss") else (h["kind"],)
            for k in kinds:
                for bb in (b - 1, b, b + 1):
                    idx[(k, bb)].add(r_i)
    return idx


def _cands(idx, h: dict, step: float) -> set:
    """Rows having an element that could match `h` by kind + diameter."""
    b = int(round(h["d"] / step))
    kinds = ("hole", "boss") if h.get("amb") and h["kind"] in ("hole", "boss") else (h["kind"],)
    return set().union(*(idx[(k, b)] for k in kinds))


def mine_part(r_i: int, rows: list[dict], idx, band: Band = TIGHT) -> dict:
    r = rows[r_i]
    elems = dedupe_coaxial(elements(r, MIN_COV), band)
    others_same_model = {i for i, o in enumerate(rows) if o["model"] == r["model"]}
    if len(elems) < 2:
        return {"path": r["path"], "model": r["model"], "status": "too_few_elements",
                "n_elems": len(elems)}
    # candidate rows per element (other models only), by diameter
    step = band.diameter
    cand = [_cands(idx, h, step) - others_same_model for h in elems]
    # elements with the fewest diameter collisions are the best seeds
    order = sorted(range(len(elems)), key=lambda i: len(cand[i]))
    # limit combinatorics for hole-heavy parts
    order = order[:12]
    sig_elems = {"path": r["path"], "holes": elems}
    # "core" constellation of this part: its least-generic elements. Another
    # model's part that contains the whole core is a composite / derivative
    # that genuinely includes this part, so a fingerprint firing on it is a
    # correct multi-label hit, not a collision.
    core = Fingerprint.from_reference(r["part"], sig_elems, order)
    contains_core: dict[int, bool] = {}

    def is_container(o_i: int) -> bool:
        if o_i not in contains_core:
            contains_core[o_i] = bool(match(core, rows[o_i], band, "TIGHT", MIN_COV))
        return contains_core[o_i]

    tested = 0
    last_hits: list[str] = []
    for k in range(2, min(MAX_SIZE, len(order)) + 1):
        best = None
        for subset in itertools.combinations(order, k):
            pool = set.intersection(*(cand[i] for i in subset))
            fp = Fingerprint.from_reference(r["part"], sig_elems, list(subset))
            hits, containers = [], []
            for o_i in pool:
                tested += 1
                if match(fp, rows[o_i], band, "TIGHT", MIN_COV):
                    (containers if is_container(o_i) else hits).append(rows[o_i]["path"])
                    if hits and best is not None:
                        break
            if not hits:
                area = sum(elems[i]["area"] for i in subset)
                if best is None or area > best[0]:
                    best = (area, subset, fp, containers)
            elif best is None:
                last_hits = hits
        if best is not None:
            _, subset, fp, containers = best
            return {"path": r["path"], "model": r["model"], "status": "ok", "size": k,
                    "n_elems": len(elems), "tested": tested,
                    "holes": [elems[i] for i in subset],
                    "relations": {f"{a}-{b}": [round(v, 3) for v in rel] for (a, b), rel in fp.relations.items()},
                    "contained_in": sorted(containers)}
    return {"path": r["path"], "model": r["model"], "status": "shared", "n_elems": len(elems),
            "tested": tested, "shared_with": sorted(set(last_hits))[:5]}


def mine_all(rows: list[dict], exclude_models: set[str] = frozenset(), verbose: bool = True) -> list[dict]:
    """Mine every part whose model is not excluded, against a library that
    also excludes those models (used for leave-one-out evaluation)."""
    rows = [r for r in rows if r["model"] not in exclude_models]
    idx = _diam_index(rows, TIGHT)
    results = []
    for r_i in range(len(rows)):
        results.append(mine_part(r_i, rows, idx))
        if verbose and (r_i + 1) % 100 == 0:
            print(f"  {r_i+1}/{len(rows)}", file=sys.stderr)
    return results


def main(out: Path = DATA / "fingerprints.json"):
    rows = load()
    results = mine_all(rows)
    json.dump(results, out.open("w"), indent=0)
    df = pd.DataFrame(results)
    print("== fingerprint mining (TIGHT, holes+bosses+cones, max size", MAX_SIZE, ")")
    print("  element kinds used:", pd.Series([h["kind"] for r in results if r["status"]=="ok" for h in r["holes"]]).value_counts().to_dict())
    print(df.status.value_counts().to_string())
    ok = df[df.status == "ok"]
    print("\n  discriminating constellation size:", ok["size"].value_counts().sort_index().to_dict())
    comp = ok[ok.contained_in.map(lambda x: isinstance(x, list) and len(x) > 0)]
    print(f"\n  parts whose fingerprint also fires on a composite/derivative that contains them: {len(comp)}")
    for _, r in comp.head(8).iterrows():
        print(f"    {r.path.split('/')[-1]}  <=  {[c.split('/')[-1] for c in r.contained_in[:3]]}")
    sh = df[df.status == "shared"]
    print(f"\n  'shared' parts by model (top):")
    print(sh.groupby("model").size().sort_values(ascending=False).head(12).to_string())
    print("\n  examples of shared parts and who they share with:")
    for _, r in sh.head(8).iterrows():
        print(f"    {r.path}\n        ~ {r.shared_with[:2]}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
