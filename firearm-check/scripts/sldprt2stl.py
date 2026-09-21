"""Extract the display tessellation from SolidWorks part files (.SLDPRT,
2015+ format) and write STL - no SolidWorks needed.

How the format was worked out (Barrel1.SLDPRT of the owner's 1911 set):

* A 2015+ SLDPRT is a ZIP-like container whose local-header signatures are
  replaced and whose entry *names* are nibble-swapped (0x43 'C' stored as
  0x34). The entry payloads are ordinary raw-deflate streams; CRC32 in the
  header matches the inflated data, which is how entries are verified here.
* The entry `Contents/DisplayLists` holds the display tessellation SolidWorks
  draws on screen: per face a record

      n_triangles u32, n_strips u32,
      [4, 8, 2, n_strips]     strip lengths (u32 each, sum = n_vertices)
      [12, 100, 2, n_verts]   vertices  (float32 x, y, z, metres)
      [12, 100, 2, n_verts]   per-vertex normals (float32)

  where [elem_size, type, flag, count] is the container's array header.
  Strips are OpenGL-style triangle strips (verified against the stored
  normals: strip winding agrees with the normals on >99 % of triangles,
  fan interpretation does not).

Limits: display tessellation is coarser than a fine STL export (SolidWorks
"image quality" setting at save time) but its vertices lie exactly on the
B-rep surfaces, so hole diameters fitted on crease vertices are exact.
Only the active configuration's body is present. Assemblies (.SLDASM) carry
no geometry of their own - convert the parts.

    python scripts/sldprt2stl.py <file_or_dir> [-o OUTDIR]
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
import zlib
from pathlib import Path

import numpy as np
import trimesh

ARRAY = re.compile(rb"\x04\x00\x00\x00\x08\x00\x00\x00\x02\x00\x00\x00", re.S)
F3_HDR = b"\x0c\x00\x00\x00\x64\x00\x00\x00\x02\x00\x00\x00"


def _swap(b: bytes) -> bytes:
    return bytes(((x & 0xF) << 4) | (x >> 4) for x in b)


def read_container(raw: bytes) -> dict[str, bytes]:
    """name -> inflated payload for every CRC-verified entry."""
    out: dict[str, bytes] = {}
    for m in re.finditer(rb"\x14\x00[\x00\x02\x04\x06\x08]\x00[\x00\x08]\x00", raw):
        p = m.start()
        try:
            ver, flags, method, t, d, crc, cs, us, nl, xl = struct.unpack_from("<HHHHHIIIHH", raw, p)
        except struct.error:
            continue
        if not (0 < nl <= 120) or cs > len(raw):
            continue
        try:
            name = _swap(raw[p + 26:p + 26 + nl]).decode("ascii")
        except UnicodeDecodeError:
            continue
        if not re.fullmatch(r"[\w\[\]\./ _-]+", name):
            continue
        data = raw[p + 26 + nl + xl:p + 26 + nl + xl + cs]
        try:
            dec = zlib.decompress(data, -15) if method == 8 else data
        except zlib.error:
            continue
        if len(dec) == us and (zlib.crc32(dec) & 0xFFFFFFFF) == crc:
            # later duplicates (e.g. several DisplayLists) - keep the largest
            if name not in out or len(dec) > len(out[name]):
                out[name] = dec
    return out


def _f3(b: bytes, p: int, n: int) -> np.ndarray:
    return np.frombuffer(b[p:p + 12 * n], dtype="<f4").reshape(n, 3).astype(np.float64)


def parse_faces(dl: bytes):
    """Yield (vertices[m], normals, strips) per tessellated face."""
    for m in ARRAY.finditer(dl):
        p = m.start()
        if p < 8:
            continue
        n_tri, n_strips = struct.unpack_from("<II", dl, p - 8)
        k = struct.unpack_from("<I", dl, p + 12)[0]
        if k != n_strips or not (0 < k < 100000):
            continue
        q = p + 16
        lens = np.frombuffer(dl[q:q + 4 * k], dtype="<u4").astype(int)
        q += 4 * k
        n_v = int(lens.sum())
        if n_tri != n_v - 2 * k or dl[q:q + 12] != F3_HDR:
            continue
        if struct.unpack_from("<I", dl, q + 12)[0] != n_v:
            continue
        verts = _f3(dl, q + 16, n_v)
        q += 16 + 12 * n_v
        normals = None
        if dl[q:q + 12] == F3_HDR and struct.unpack_from("<I", dl, q + 12)[0] == n_v:
            normals = _f3(dl, q + 16, n_v)
        yield verts, normals, lens


def strip_triangles(lens: np.ndarray, verts: np.ndarray, normals: np.ndarray | None) -> np.ndarray:
    """Triangle index array from strip lengths; winding fixed per triangle to
    agree with the vertex normals when available."""
    tris = []
    base = 0
    for L in lens:
        for i in range(L - 2):
            a, b_, c = base + i, base + i + 1, base + i + 2
            tris.append((a, c, b_) if i % 2 else (a, b_, c))
        base += L
    tris = np.array(tris, dtype=np.int64).reshape(-1, 3)
    if normals is not None and len(tris):
        fn = np.cross(verts[tris[:, 1]] - verts[tris[:, 0]], verts[tris[:, 2]] - verts[tris[:, 0]])
        vn = normals[tris].mean(axis=1)
        flip = np.einsum("ij,ij->i", fn, vn) < 0
        tris[flip] = tris[flip][:, [0, 2, 1]]
    return tris


def sldprt_to_mesh(path: Path) -> tuple[trimesh.Trimesh, dict]:
    raw = path.read_bytes()
    entries = read_container(raw)
    dl = entries.get("Contents/DisplayLists")
    if dl is None:
        raise ValueError(f"no DisplayLists stream in {path.name} ({len(entries)} entries: {sorted(entries)[:6]}...)")
    V, F = [], []
    off = 0
    n_faces = 0
    for verts, normals, lens in parse_faces(dl):
        tris = strip_triangles(lens, verts, normals)
        if len(tris) == 0:
            continue
        V.append(verts * 1000.0)            # metres -> mm
        F.append(tris + off)
        off += len(verts)
        n_faces += 1
    if not V:
        raise ValueError(f"no tessellated faces found in {path.name}")
    mesh = trimesh.Trimesh(np.vstack(V), np.vstack(F), process=True)
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    info = {"brep_faces": n_faces, "triangles": len(mesh.faces), "watertight": bool(mesh.is_watertight),
            "extents_mm": [round(float(x), 3) for x in mesh.extents], "entries": len(entries)}
    return mesh, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help=".SLDPRT file or a directory of them")
    ap.add_argument("-o", "--out", default=None, help="output directory (default: <src>/stl)")
    a = ap.parse_args()
    src = Path(a.src)
    files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.suffix.lower() == ".sldprt")
    out = Path(a.out) if a.out else (src.parent if src.is_file() else src) / "stl"
    out.mkdir(parents=True, exist_ok=True)
    ok = 0
    for f in files:
        try:
            mesh, info = sldprt_to_mesh(f)
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {f.name}: {e}")
            continue
        dst = out / (f.stem.replace(" ", "_") + ".stl")
        mesh.export(dst)
        ok += 1
        print(f"  {f.name:<45} {info['brep_faces']:>4} faces {info['triangles']:>7} tris "
              f"{'watertight' if info['watertight'] else 'open      '} extents {info['extents_mm']} mm -> {dst.name}")
    print(f"{ok}/{len(files)} converted into {out}")


if __name__ == "__main__":
    main()
