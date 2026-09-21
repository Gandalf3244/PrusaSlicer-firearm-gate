"""Catalog every STL in the reference repo with labels and basic mesh stats.

Label convention derived from the repo layout:
    <category>/<model_dir>/[STL/[ascii|binary/]]<part>.stl
  category : Ammo | Firearms | Grenades | Misc | Muzzle_Devices | Pistols | Rifles
  model    : model_dir with the trailing "-Author" suffix kept (it disambiguates
             e.g. several AR-15 lower receivers by different designers)
  part     : file stem
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd
import trimesh

REPO = Path(__file__).resolve().parents[1] / "fosscad-repo"
DATA = Path(__file__).resolve().parents[1] / "data"


@dataclass
class Record:
    path: str
    category: str
    model: str
    part: str
    n_faces: int
    n_vertices: int
    n_bodies: int
    watertight: bool
    ext_x: float
    ext_y: float
    ext_z: float
    volume: float
    area: float
    geom_hash: str
    error: str = ""


def labels_for(rel: Path) -> tuple[str, str, str]:
    parts = rel.parts
    category = parts[0]
    model = parts[1] if len(parts) > 2 else parts[0]
    return category, model, rel.stem


def geom_hash(mesh: trimesh.Trimesh) -> str:
    """Hash of sorted, rounded vertex coordinates: identical for ascii/binary
    exports of the same geometry, independent of triangle order."""
    v = np.round(mesh.vertices, 4)
    v = v[np.lexsort(v.T[::-1])]
    return hashlib.sha1(v.tobytes()).hexdigest()[:16]


def load(path: Path) -> trimesh.Trimesh:
    m = trimesh.load(path, force="mesh", process=True)
    if not isinstance(m, trimesh.Trimesh):
        raise ValueError(f"not a mesh: {type(m)}")
    return m


def scan(path: Path) -> Record:
    rel = path.relative_to(REPO)
    cat, model, part = labels_for(rel)
    try:
        m = load(path)
        ext = m.extents if len(m.vertices) else np.zeros(3)
        return Record(
            path=str(rel), category=cat, model=model, part=part,
            n_faces=len(m.faces), n_vertices=len(m.vertices),
            n_bodies=int(m.body_count), watertight=bool(m.is_watertight),
            ext_x=float(ext[0]), ext_y=float(ext[1]), ext_z=float(ext[2]),
            volume=float(m.volume) if m.is_watertight else float("nan"),
            area=float(m.area), geom_hash=geom_hash(m),
        )
    except Exception as e:  # noqa: BLE001 - record and keep going
        return Record(path=str(rel), category=cat, model=model, part=part,
                      n_faces=0, n_vertices=0, n_bodies=0, watertight=False,
                      ext_x=0, ext_y=0, ext_z=0, volume=float("nan"), area=0,
                      geom_hash="", error=f"{type(e).__name__}: {e}"[:200])


def main(out: Path = DATA / "inventory.csv") -> pd.DataFrame:
    files = sorted(p for p in REPO.rglob("*") if p.suffix.lower() == ".stl")
    print(f"scanning {len(files)} STL files", file=sys.stderr)
    recs = []
    for i, p in enumerate(files, 1):
        recs.append(asdict(scan(p)))
        if i % 100 == 0:
            print(f"  {i}/{len(files)}", file=sys.stderr)
    df = pd.DataFrame(recs)
    out.parent.mkdir(exist_ok=True)
    df.to_csv(out, index=False)
    print(f"wrote {out}", file=sys.stderr)
    return df


if __name__ == "__main__":
    main()
