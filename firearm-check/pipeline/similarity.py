"""Step 2 of the build order: pairwise similarity and collision analysis over
the reference library's signatures.

Three questions, each answered empirically from data/signatures.jsonl:
  1. Coverage  - how much of each part is explained by fitted primitives, and
                 how many parts carry at least one measurable hole/boss.
  2. Collision - for every hole diameter in the library, how many *different
                 models* share it (within tolerance). High-collision diameters
                 are generic and must not be used as single-dimension fingerprints.
  3. Uniqueness - for every part, does its compound fingerprint (set of hole
                 diameters + pairwise hole distances) separate it from every
                 other model's parts? Which parts collide, and with what?
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

D_TOL = 0.10      # mm, diameter match tolerance for collision analysis
DIST_TOL = 0.25   # mm, hole-centre distance tolerance


def load(path: Path = DATA / "signatures.jsonl") -> list[dict]:
    rows = [json.loads(l) for l in path.open()]
    inv = pd.read_csv(DATA / "inventory.csv").set_index("path")
    out, seen = [], set()
    for r in rows:
        if "error" in r:
            continue
        r["model"] = inv.loc[r["path"], "model"]
        r["category"] = inv.loc[r["path"], "category"]
        r["part"] = inv.loc[r["path"], "part"]
        # ascii/binary re-exports of one part differ in float noise; collapse
        # them on (model, face count, coarse area) so a model's duplicates do
        # not inflate instance counts
        key = (r["model"], r["n_faces"], round(r["area"], 0))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# --------------------------------------------------------------------------- #
def coverage_report(rows: list[dict]) -> pd.DataFrame:
    rec = []
    for r in rows:
        af = r["area_frac"]
        rec.append({
            "path": r["path"], "model": r["model"], "category": r["category"],
            "n_faces": r["n_faces"], "area": r["area"],
            "prim_frac": 1 - af["freeform"] - af["other"],
            "freeform_frac": af["freeform"],
            "n_holes": r["counts"]["holes"], "n_bosses": r["counts"]["bosses"],
            "n_planes": r["counts"]["planes"], "scale": r["scale"],
        })
    return pd.DataFrame(rec)


# --------------------------------------------------------------------------- #
def diameter_collisions(rows: list[dict], kind: str = "holes") -> pd.DataFrame:
    """Cluster all diameters of `kind` across the library; report how many
    models and parts share each cluster."""
    items = []
    for r in rows:
        for h in r[kind]:
            items.append((h["d"], r["model"], r["path"], h["L"], h["area"]))
    items.sort()
    # greedy windows of width 2*D_TOL from the smallest value up; avoids
    # single-linkage chaining across a continuum of diameters
    clusters, cur, start = [], [], None
    for it in items:
        if cur and it[0] - start > 2 * D_TOL:
            clusters.append(cur); cur = []
        if not cur:
            start = it[0]
        cur.append(it)
    if cur:
        clusters.append(cur)
    rec = []
    for c in clusters:
        ds = np.array([x[0] for x in c])
        rec.append({
            "d_mm": round(float(ds.mean()), 3), "d_in": round(float(ds.mean()) / 25.4, 4),
            "n_instances": len(c), "n_parts": len({x[2] for x in c}),
            "n_models": len({x[1] for x in c}),
            "models": sorted({x[1] for x in c}),
        })
    return pd.DataFrame(rec).sort_values("n_models", ascending=False)


# --------------------------------------------------------------------------- #
def _fingerprint(r: dict):
    """Compound fingerprint: sorted hole diameters and pairwise distances."""
    ds = np.array(sorted(h["d"] for h in r["holes"]))
    dist = np.array(sorted(p["dist"] for p in r["hole_pairs"]))
    return ds, dist


def _match_sets(a: np.ndarray, b: np.ndarray, tol: float) -> float:
    """Fraction of values in `a` that have a match in `b` within tol."""
    if len(a) == 0:
        return 1.0
    if len(b) == 0:
        return 0.0
    idx = np.searchsorted(b, a)
    lo = b[np.clip(idx - 1, 0, len(b) - 1)]
    hi = b[np.clip(idx, 0, len(b) - 1)]
    return float(np.mean(np.minimum(np.abs(a - lo), np.abs(a - hi)) <= tol))


def fingerprint_uniqueness(rows: list[dict]) -> pd.DataFrame:
    """For each part with >=1 hole: is there a part of a *different model*
    whose hole set contains all of this part's holes (diameters and pairwise
    distances)? That is exactly the false-positive scenario for a fused mesh:
    the other part would be flagged as containing this one."""
    fps = [(_fingerprint(r), r) for r in rows]
    rec = []
    for (ds, dist), r in fps:
        if len(ds) == 0:
            rec.append({"path": r["path"], "model": r["model"], "n_holes": 0,
                        "n_pairs": 0, "contained_in": [], "status": "no_holes"})
            continue
        hits = []
        for (ds2, dist2), r2 in fps:
            if r2["model"] == r["model"]:
                continue
            if _match_sets(ds, ds2, D_TOL) == 1.0 and _match_sets(dist, dist2, DIST_TOL) == 1.0:
                hits.append(r2["path"])
        status = "unique" if not hits else ("generic" if len(ds) == 1 else "collides")
        rec.append({"path": r["path"], "model": r["model"], "n_holes": len(ds),
                    "n_pairs": len(dist), "contained_in": hits, "status": status})
    return pd.DataFrame(rec)


# --------------------------------------------------------------------------- #
def global_vectors(rows: list[dict]) -> tuple[np.ndarray, list[str]]:
    """Pose-independent global shape vector for coarse 'shape stage' similarity."""
    X = []
    for r in rows:
        af, c = r["area_frac"], r["counts"]
        X.append([
            *np.log(np.maximum(r["obb"], 1e-3)),
            *r["obb_ratios"],
            np.log(max(r["area"], 1e-3)),
            r.get("fill", np.nan),
            af["plane"], af["cyl_hole"], af["cyl_boss"], af["cone"], af["freeform"],
            np.log1p(c["planes"]), np.log1p(c["holes"]), np.log1p(c["bosses"]),
            np.log1p(c["cones"]), np.log1p(c["plane_dirs"]),
        ])
    X = np.array(X, float)
    X[np.isnan(X)] = np.nanmean(X[:, 6])  # fill missing 'fill' with the mean
    names = ["log_obb0", "log_obb1", "log_obb2", "ratio1", "ratio2", "log_area", "fill",
             "f_plane", "f_hole", "f_boss", "f_cone", "f_free",
             "n_planes", "n_holes", "n_bosses", "n_cones", "n_dirs"]
    return X, names


def nearest_cross_model(rows: list[dict], k: int = 3) -> pd.DataFrame:
    """Nearest neighbours (standardised global vector) restricted to parts of a
    different model: surfaces cross-family lookalikes / shared parts."""
    X, _ = global_vectors(rows)
    Z = (X - X.mean(0)) / (X.std(0) + 1e-9)
    D = np.sqrt(((Z[:, None, :] - Z[None, :, :]) ** 2).sum(-1))
    models = np.array([r["model"] for r in rows])
    rec = []
    for i, r in enumerate(rows):
        d = D[i].copy()
        d[models == r["model"]] = np.inf
        j = np.argsort(d)[:k]
        rec.append({"path": r["path"], "model": r["model"],
                    "nn_dist": round(float(d[j[0]]), 3),
                    "nn": [rows[x]["path"] for x in j]})
    return pd.DataFrame(rec).sort_values("nn_dist")


# --------------------------------------------------------------------------- #
def main():
    rows = load()
    out = DATA / "analysis"; out.mkdir(exist_ok=True)

    cov = coverage_report(rows); cov.to_csv(out / "coverage.csv", index=False)
    print(f"== coverage ({len(cov)} unique parts)")
    print(f"  median primitive-explained area fraction: {cov.prim_frac.median():.2f}"
          f"  (quartiles {cov.prim_frac.quantile(.25):.2f} / {cov.prim_frac.quantile(.75):.2f})")
    print(f"  parts with >=1 measurable hole: {(cov.n_holes>0).mean():.1%}   "
          f">=2 holes: {(cov.n_holes>=2).mean():.1%}   >=1 boss: {(cov.n_bosses>0).mean():.1%}")
    print(f"  parts with <30% primitive area: {(cov.prim_frac<0.3).sum()}")
    print("  by category (median prim_frac, % with holes):")
    g = cov.groupby("category").agg(n=("path", "size"), prim=("prim_frac", "median"),
                                    holes=("n_holes", lambda s: (s > 0).mean()))
    print(g.round(2).to_string())

    col = diameter_collisions(rows, "holes"); col.to_csv(out / "hole_diameter_collisions.csv", index=False)
    print(f"\n== hole-diameter collisions ({len(col)} distinct diameters at +/-{D_TOL} mm)")
    print(f"  diameters shared by >=3 models: {(col.n_models>=3).sum()}  | unique to one model: {(col.n_models==1).sum()}")
    print("  most generic hole diameters:")
    print(col.head(15)[["d_mm", "d_in", "n_instances", "n_parts", "n_models"]].to_string(index=False))

    colb = diameter_collisions(rows, "bosses"); colb.to_csv(out / "boss_diameter_collisions.csv", index=False)
    print(f"\n== boss-diameter collisions: {len(colb)} distinct, shared by >=3 models: {(colb.n_models>=3).sum()}")

    uq = fingerprint_uniqueness(rows); uq.to_csv(out / "fingerprint_uniqueness.csv", index=False)
    print("\n== compound-fingerprint uniqueness (holes + pairwise distances, cross-model)")
    print(uq.status.value_counts().to_string())
    coll = uq[uq.status != "unique"].copy()
    coll = coll[coll.n_holes > 0]
    coll["n_contained"] = coll.contained_in.map(len)
    print("  examples of multi-hole parts contained in another model's part:")
    for _, r in coll[coll.n_holes >= 2].sort_values("n_holes", ascending=False).head(12).iterrows():
        print(f"    [{r.n_holes} holes] {r.path}\n        in: {r.contained_in[:2]}")

    nn = nearest_cross_model(rows); nn.to_csv(out / "nearest_cross_model.csv", index=False)
    print("\n== closest cross-model lookalikes (global shape vector)")
    for _, r in nn.head(12).iterrows():
        print(f"  {r.nn_dist:5.3f}  {r.path}\n         ~ {r.nn[0]}")


if __name__ == "__main__":
    main()
