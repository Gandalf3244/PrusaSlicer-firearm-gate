"""Stage a Windows PrusaSlicer build with the bundled checker and compile the installer.

    python build_installer.py --build <PrusaSlicer build output dir> --source <PrusaSlicer source tree>
                              --checker <dir from make_checker_runtime.py> --out <dir> [--version 2.9.6]

<build output dir> is where the Windows build put prusa-slicer.exe, prusa-slicer-console.exe,
prusa-gcodeviewer.exe and PrusaSlicer.dll (build\\src\\Release with Visual Studio).
Needs makensis (NSIS 3) on PATH; works on Linux (conda-forge nsis) and Windows.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
VC_REDIST = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
BINARIES = ["prusa-slicer.exe", "prusa-slicer-console.exe", "prusa-gcodeviewer.exe", "PrusaSlicer.dll"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", type=Path, required=True)
    ap.add_argument("--source", type=Path, required=True, help="PrusaSlicer source tree (for resources/)")
    ap.add_argument("--checker", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--version", default="2.9.6")
    ap.add_argument("--no-vcredist", action="store_true")
    a = ap.parse_args()

    # absolute: makensis compiles relative to the script's directory, not the caller's cwd
    a.out = a.out.resolve()
    stage = a.out / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    missing = [b for b in BINARIES if not (a.build / b).exists()]
    if missing:
        sys.exit(f"missing in {a.build}: {', '.join(missing)}")
    for f in a.build.iterdir():
        if f.suffix.lower() in {".exe", ".dll"}:
            shutil.copy(f, stage / f.name)

    shutil.copytree(a.source / "resources", stage / "resources", ignore=shutil.ignore_patterns("firearm-check"))
    shutil.copytree(a.checker, stage / "resources" / "firearm-check")

    if not a.no_vcredist:
        print("downloading vc_redist.x64.exe")
        with urllib.request.urlopen(VC_REDIST, timeout=120) as r:
            (stage / "vc_redist.x64.exe").write_bytes(r.read())

    outfile = a.out / f"PrusaSlicer-FirearmGate-{a.version}-win64.exe"
    cmd = ["makensis", "-V2", f"-DSTAGE={stage}", f"-DVERSION={a.version}", f"-DOUTFILE={outfile}",
           str(HERE / "installer.nsi")]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"{outfile}: {outfile.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
