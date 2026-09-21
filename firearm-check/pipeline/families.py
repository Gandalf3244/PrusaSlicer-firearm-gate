"""Step 6: machine families (firearm platforms) for search-space pruning and
platform-level labels.

Two things live here:

1. A label taxonomy: every reference model is mapped to one or more
   *families* (AR-15, Glock, 1911, ...) from its model / path name. A
   composite design belongs to every platform it borrows from (the Gluty is
   an AR-15 fire-control pocket on a Glock magazine well).

2. *Family patterns*: constellations that recur across several models of one
   family and never occur in another family. They are the platform-level
   evidence the design report asked for ("coarsely classify the machine
   family first"): the AR-15 hammer/trigger pin pair is the canonical one.
   Mined automatically (pairs and triples of round elements, LOOSE tolerance,
   >= MIN_MODELS other models of the family, zero hits outside it), so a new
   platform added to the library gets its patterns without hand-picking.

    python -m pipeline.families        # data/families.csv, data/family_patterns.json

detect.py uses both: it reports which families' patterns are present, and
`--family X` / `--prune` restricts part-level fingerprints to the detected
or given families (parts of unknown family are always kept).
"""
from __future__ import annotations

import itertools
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from .calibers import classify_diameter
from .fingerprint import Fingerprint, dedupe_coaxial, elements, match
from .mine import MIN_COV, _cands, _diam_index
from .similarity import load
from .tolerances import LOOSE, TIGHT

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# (family, regex over "model/path" lower-cased). A model can match several.
FAMILY_PATTERNS: list[tuple[str, str]] = [
    # "AR" = the AR-15/AR-10 pattern (they share the fire-control group, grip
    # screw and buffer tube); "AR-10" additionally marks the large-frame variant
    ("AR", r"ar-?15|ar-?10|\bar_|m-16|m4a1|shuty|gluty|ap-?9|protector|fcg|trigger_module|vanguard|hermes|charon|phobos|hanuman|atlas|aliamanu|thor_iv|ghostfire|ar15000|sopmod|treillage|orion_pdw|p-15_stock|telescopic-stock|bumpfire|cma_stock|tailhook_arm|sisyphus|minimalist_stock|lr-308|abraxas|caleb|nephilim|lts-zeus|\bwx_"),
    ("AR-10", r"ar-?10|lr-308|abraxas|caleb|nephilim|lts-zeus|\bwx_"),
    ("AK", r"ak-?47|akm|\bak_"),
    ("VZ-58", r"vz-58"),
    ("Glock", r"glock|\bg17\b|\bg19\b|\bg26|\bg43|p80|w26|gluty|ss80"),
    ("1911", r"1911|sti_frame|sti_full"),
    ("10/22", r"10[_-]22"),
    ("Liberator", r"liberator|pepperbox_liberator"),
    ("Skorpion vz61", r"skorpion|vz61"),
    ("CZ Scorpion Evo", r"scorpion_evo"),
    ("XD-40", r"xd-40"),
    ("Ruger MK", r"ruger_mk"),
    ("Uzi", r"\buzi"),
    ("MAC-10", r"mac-10|m-10_"),
    ("Madsen M-50", r"madsen"),
    ("Mauser", r"mauser"),
    ("M14", r"\bm14"),
    ("MP5", r"mp5"),
    ("P90", r"\bp90"),
    ("Sten", r"\bsten"),
    ("Makarov", r"makarov"),
    ("SIG", r"sig_p"),
    ("Beretta 92", r"beretta|92fs"),
    ("HK USP", r"hk_usp"),
    ("Steyr", r"steyr"),
    ("S&W", r"m&p|sd9"),
    ("Ruger SR9", r"sr9"),
    ("Tec-9", r"gab-10|tec-9"),
    ("DB380", r"db380"),
    ("SKS", r"\bsks"),
    ("Reprringer", r"reprringer|pentagun|hexen"),
    ("Songbird/Washbear", r"songbird|washbear|pm422|pm522"),
    ("Proteus", r"proteus"),
    ("Caboose", r"caboose|parlor|sacramento"),
    ("Flare pistol", r"flare_pistol"),
    ("Grizzly", r"grizzly"),
    ("Picatinny", r"picatinny|keymod|quad_rail|rail_mount|dual_picatinny"),
]
_COMPILED = [(f, re.compile(p, re.I)) for f, p in FAMILY_PATTERNS]

MIN_MODELS = 2          # a family pattern must recur in >= this many *other* models of the family
MAX_PATTERNS = 12       # kept per family
MAX_ELEMS = 10          # elements considered per part when mining


def families_of(model: str, path: str = "") -> list[str]:
    text = f"{model}/{path}".replace(" ", "_")
    out = [f for f, rx in _COMPILED if rx.search(text)]
    return out or ["other"]


def load_families(path: Path = DATA / "families.csv") -> dict[str, list[str]]:
    df = pd.read_csv(path)
    return {r.path: r.families.split("|") for r in df.itertuples()}


# --------------------------------------------------------------------------- #
def _round_elems(sig: dict) -> list[dict]:
    els = [e for e in dedupe_coaxial(elements(sig, MIN_COV), TIGHT) if e["kind"] in ("hole", "boss", "cone")]
    els.sort(key=lambda e: -e["area"])
    return els[:MAX_ELEMS]


def mine_family_patterns(rows: list[dict], fam_of: dict[str, list[str]], band=LOOSE, verbose=True) -> dict[str, list[dict]]:
    idx = _diam_index(rows, band)
    step = band.diameter
    by_fam: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        for f in fam_of[r["path"]]:
            by_fam[f].append(i)
    out: dict[str, list[dict]] = {}
    for fam, members in sorted(by_fam.items()):
        models = {rows[i]["model"] for i in members}
        if fam == "other" or len(models) < MIN_MODELS + 1:
            continue
        member_set = set(members)
        outside = [i for i, r in enumerate(rows) if fam not in fam_of[r["path"]]]
        outside_set = set(outside)
        seen_keys: set = set()
        found: list[dict] = []
        for i in members:
            r = rows[i]
            els = _round_elems(r)
            if len(els) < 2:
                continue
            sig_elems = {"path": r["path"], "holes": els}
            cand = [_cands(idx, e, step) for e in els]
            generic = [classify_diameter(e["d"], e["kind"], e.get("L", 0.0))[0] == "generic" for e in els]
            for k in (2, 3):
                for subset in itertools.combinations(range(len(els)), k):
                    # two standard-size holes at some spacing is not platform
                    # evidence (a Glock 3.96 + 3.00 @ 64 mm pair fired on two
                    # printer parts); pairs need a non-generic diameter
                    if k == 2 and all(generic[j] for j in subset):
                        continue
                    fp = Fingerprint.from_reference(fam, sig_elems, list(subset))
                    key = tuple(sorted((e["kind"], round(e["d"], 1)) for e in fp.holes)) + tuple(
                        sorted((round(a), round(s, 0)) for a, s in fp.relations.values()))
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    pool = set.intersection(*(cand[j] for j in subset))
                    # in-family support: other models of the family
                    in_models = set()
                    for o in pool & member_set:
                        if rows[o]["model"] != r["model"] and match(fp, rows[o], band, "LOOSE", MIN_COV):
                            in_models.add(rows[o]["model"])
                    if len(in_models) < MIN_MODELS:
                        continue
                    # exclusivity: no part outside the family
                    leak = None
                    for o in pool & outside_set:
                        if match(fp, rows[o], band, "LOOSE", MIN_COV):
                            leak = rows[o]["path"]; break
                    if leak:
                        continue
                    found.append({"family": fam, "source": r["path"], "size": k,
                                  "holes": fp.holes, "relations": {f"{a}-{b}": [round(v, 3) for v in rel] for (a, b), rel in fp.relations.items()},
                                  "n_models": len(in_models) + 1, "models": sorted(in_models | {r["model"]})})
        # keep the most widely shared, most specific patterns
        found.sort(key=lambda p: (-p["n_models"], -p["size"]))
        kept, cover = [], Counter()
        for p in found:
            # prefer patterns that add models not yet covered by kept patterns
            new = [m for m in p["models"] if cover[m] < 2]
            if not new and len(kept) >= 4:
                continue
            kept.append(p); cover.update(p["models"])
            if len(kept) >= MAX_PATTERNS:
                break
        if kept:
            out[fam] = kept
        if verbose:
            print(f"  {fam:<18} {len(models):>3} models, {len(members):>3} parts -> {len(found):>4} exclusive patterns, kept {len(kept)}"
                  f"{'  (top covers ' + str(kept[0]['n_models']) + ' models)' if kept else ''}", file=sys.stderr)
    return out


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


def main():
    rows = load()
    fam_of = {r["path"]: families_of(r["model"], r["path"]) for r in rows}
    df = pd.DataFrame([{"path": p, "model": next(r["model"] for r in rows if r["path"] == p), "families": "|".join(f)}
                       for p, f in fam_of.items()])
    df.to_csv(DATA / "families.csv", index=False)
    counts = Counter(f for fs in fam_of.values() for f in fs)
    print("== families (parts):", dict(counts.most_common()))
    print("\n== mining family patterns (LOOSE, exclusive to the family, shared by >= %d other models)" % MIN_MODELS)
    pats = mine_family_patterns(rows, fam_of)
    json.dump(pats, (DATA / "family_patterns.json").open("w"), indent=0)
    for fam, ps in pats.items():
        print(f"\n  {fam}: {len(ps)} patterns")
        for p in ps[:4]:
            els = ", ".join(f"{h['kind']} {h['d']:.2f}" for h in p["holes"])
            rel = ", ".join(f"{a:.0f}deg/{s:.1f}mm" for a, s in p["relations"].values())
            print(f"     {p['n_models']:>2} models  [{els}]  {rel}   (from {p['source'].split('/')[-1]})")
    # self-test: how many library parts get their own family back?
    fps = load_family_patterns()
    ok = amb = none = 0
    for r in rows:
        c = classify_family(r, fps)
        if not c:
            none += 1
        elif set(c) & set(fam_of[r["path"]]):
            ok += 1
        else:
            amb += 1
    print(f"\n== self-test on {len(rows)} parts: correct family {ok}, wrong family {amb}, no platform evidence {none}")
    print(f"wrote data/families.csv, data/family_patterns.json")


if __name__ == "__main__":
    main()
