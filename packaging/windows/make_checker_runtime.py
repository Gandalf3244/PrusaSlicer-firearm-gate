"""Assemble the self-contained Windows checker that the installer ships at
    <PrusaSlicer>\\resources\\firearm-check\\
        python\\   official embeddable CPython + numpy/scipy/trimesh (Windows wheels)
        app\\      pipeline/, data/fingerprints.json, data/family_patterns.json, scripts/sldprt2stl.py

    python make_checker_runtime.py <project root> <output dir> [--python 3.13]

Runs on Linux, macOS or Windows with any Python 3.9+: wheels are zips, nothing
in the runtime has to execute here. Package versions are pinned to what the
checker is validated with (PIPELINE_STATUS.md).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PINNED = ["numpy==2.5.3", "scipy==1.18.1", "trimesh==5.1.0"]
PYTHON_FTP = "https://www.python.org/ftp/python/"


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def newest_embeddable(minor: str) -> str:
    """Newest {minor}.x that ships python-X-embed-amd64.zip (security-only releases are source only)."""
    index = fetch(PYTHON_FTP).decode()
    versions = sorted({v for v in re.findall(rf"{re.escape(minor)}\.\d+", index)},
                      key=lambda v: [int(x) for x in v.split(".")], reverse=True)
    for v in versions:
        if f"python-{v}-embed-amd64.zip" in fetch(f"{PYTHON_FTP}{v}/").decode():
            return v
    sys.exit(f"no embeddable zip for Python {minor}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, help="folder with pipeline/, data/, scripts/")
    ap.add_argument("out", type=Path)
    ap.add_argument("--python", default="3.13", help="CPython minor version")
    a = ap.parse_args()

    if a.out.exists():
        shutil.rmtree(a.out)
    py_dir, app = a.out / "python", a.out / "app"
    py_dir.mkdir(parents=True)

    full = newest_embeddable(a.python)
    print(f"Python {full}")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "python.zip").write_bytes(fetch(f"{PYTHON_FTP}{full}/python-{full}-embed-amd64.zip"))
        zipfile.ZipFile(tmp / "python.zip").extractall(py_dir)

        subprocess.run([sys.executable, "-m", "pip", "download", "-q", "--platform", "win_amd64",
                        "--python-version", a.python, "--implementation", "cp", "--only-binary=:all:",
                        "-d", str(tmp / "wheels"), *PINNED], check=True)
        site = py_dir / "Lib" / "site-packages"
        site.mkdir(parents=True)
        wheels = sorted((tmp / "wheels").glob("*.whl"))
        for w in wheels:
            zipfile.ZipFile(w).extractall(site)
        for stray in site.glob("*.whl"):      # scipy's wheel carries an empty file of its own name
            stray.unlink()

    # the embeddable runtime's ._pth is the whole sys.path: add site-packages and the app
    pth = next(py_dir.glob("python3*._pth"))
    pth.write_text(pth.read_text() + "Lib/site-packages\n../app\n")

    shutil.copytree(a.root / "pipeline", app / "pipeline", ignore=shutil.ignore_patterns("__pycache__"))
    (app / "data").mkdir()
    for f in ("fingerprints.json", "family_patterns.json"):
        shutil.copy(a.root / "data" / f, app / "data" / f)
    (app / "scripts").mkdir()
    shutil.copy(a.root / "scripts" / "sldprt2stl.py", app / "scripts" / "sldprt2stl.py")
    (a.out / "VERSION.txt").write_text(f"python {full}\n" + "\n".join(w.name for w in wheels) + "\n")
    total = sum(p.stat().st_size for p in a.out.rglob("*") if p.is_file())
    print(f"{a.out}: {total / 1e6:.0f} MB, {len(wheels)} wheels")


if __name__ == "__main__":
    main()
