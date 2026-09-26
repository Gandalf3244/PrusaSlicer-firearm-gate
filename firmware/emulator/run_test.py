#!/usr/bin/env python3
"""Emulator test of the firearm check in the MK4 firmware.

Boots a no-bootloader MK4 build in Mini404 (see setup.sh), opens one G-code file from a
virtual USB drive, presses Print through the preview's questions and reads the screen:

    python run_test.py --firmware build/products --gcode FILE --expect blocked|prints

blocked = the "Print blocked / This file contains a firearm part" screen appeared and the
print never started; prints = the printing screen appeared and nothing was blocked.
Screenshots go to --out. Needs Pillow and numpy.

The first run for a firmware build boots once with the .bbf on the drive so the firmware
installs its fonts and images into the emulated external flash; that flash is cached per build.
The EEPROM comes from fixtures/ (English, MK4S, setup wizard done, selftests not run - so the
preview asks "complete calibrations?", which the script answers with Ignore).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
EMU = Path(
    os.environ.get("FIREARM_EMU_DIR",
                   Path.home() / ".cache" / "firearm-check-emulator"))
QEMU = EMU / "Mini404-dev-linux" / "qemu-system-buddy"
LIBS = EMU / "libs" / "usr" / "lib" / "x86_64-linux-gnu"
EEPROM = "Prusa_Mk4_eeprom.bin"
XFLASH = "Prusa_Mk4_xflash.bin"
# screen areas that identify a result (crops of screenshots of this firmware)
REFS = {
    "blocked_firearm":
    ((10, 84, 400, 106),
     "refs/blocked_firearm.png"),  # "This file contains a firearm part."
    "blocked": ((10, 38, 200, 66),
                "refs/blocked_title.png"),  # "Print blocked" (any reason)
    "printing":
    ((10, 6, 150, 32), "refs/printing_header.png"),  # "PRINTING ..."
}


def qemu(run_dir: Path, usb_dir: Path, kernel: Path, script: list[str],
         timeout: int) -> None:
    (run_dir / "script.txt").write_text("\n".join(script) +
                                        "\nScriptHost::Log(done)\n")
    env = dict(os.environ, LD_LIBRARY_PATH=str(LIBS))
    cmd = [
        str(QEMU),
        "-machine",
        "prusa-mk4-034",
        "-icount",
        "shift=4",  # shift=3 red-screens the firmware
        "-display",
        "none",
        "-kernel",
        str(kernel),
        "-append",
        "script=script.txt",
        "-drive",
        f"id=usbstick,file=fat:rw:{usb_dir},if=none,format=raw",
        "-device",
        "usb-storage,drive=usbstick"
    ]
    log_path = run_dir / "qemu.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd,
                                cwd=run_dir,
                                env=env,
                                stdout=log,
                                stderr=subprocess.STDOUT)
        # QEMU keeps running after the script: stop it when the script logs "done"
        deadline = time.monotonic() + timeout
        try:
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(2)
                if "ScriptLog: done" in log_path.read_text(errors="replace"):
                    break
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


def prepared_xflash(bbf: Path, kernel: Path) -> Path:
    """External flash with the build's resources installed (cached per .bbf)."""
    key = hashlib.sha256(bbf.read_bytes()).hexdigest()[:16]
    cache = EMU / "state" / key
    if (cache / XFLASH).exists():
        return cache / XFLASH
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as usb:
        shutil.copy(bbf, Path(usb) / "firmware.bbf")
        shutil.copy(HERE / "fixtures" / EEPROM, cache / EEPROM)
        print(
            "first boot of this build: installing resources (a few minutes)...",
            flush=True)
        qemu(cache, Path(usb), kernel, [
            "ScriptHost::WaitMs(300000)",
            "generic-spi-display::Screenshot(prepared.png)"
        ], 1800)
    (cache / EEPROM).unlink(missing_ok=True)
    return cache / XFLASH


def classify(png: Path) -> set[str]:
    im = Image.open(png).convert("RGB")
    return {
        name
        for name, (box, ref) in REFS.items()
        if np.array_equal(np.asarray(im.crop(box)),
                          np.asarray(Image.open(HERE / ref).convert("RGB")))
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--firmware",
        required=True,
        help="directory with mk4_release_noboot.bin and .bbf (build/products)")
    ap.add_argument("--gcode",
                    required=True,
                    help=".gcode or .bgcode file to try to print")
    ap.add_argument("--expect", required=True, choices=["blocked", "prints"])
    ap.add_argument("--load-wait",
                    type=float,
                    default=None,
                    help="seconds for the preview to load (default from size)")
    ap.add_argument("--out",
                    default="emulator_screens",
                    help="screenshot directory")
    a = ap.parse_args()

    if not QEMU.exists():
        sys.exit(f"run setup.sh first (no {QEMU})")
    fw = Path(a.firmware).resolve()  # QEMU runs in another directory
    kernel, bbf = fw / "mk4_release_noboot.bin", fw / "mk4_release_noboot.bbf"
    gcode = Path(a.gcode).resolve()
    xflash = prepared_xflash(bbf, kernel)
    # the preview loads a file slowly on the emulated USB (about 20 s per MB of binary G-code)
    load_ms = int(1000 * (a.load_wait if a.load_wait is not None else 15 +
                          20 * gcode.stat().st_size / 1e6))

    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        run, usb = Path(tmp) / "run", Path(tmp) / "usb"
        run.mkdir()
        usb.mkdir()
        shutil.copy(HERE / "fixtures" / EEPROM, run / EEPROM)
        shutil.copy(xflash, run / XFLASH)
        shutil.copy(bbf, usb / "firmware.bbf")
        shutil.copy(gcode, usb / ("test" + gcode.suffix))
        shots = []

        def shot(name: str) -> str:
            shots.append(name)
            return f"generic-spi-display::Screenshot({name}.png)"

        script = [
            "ScriptHost::WaitMs(45000)",
            shot("00_home"),
            "encoder-input::Push()",
            "ScriptHost::WaitMs(5000)",  # dismiss "complete calibrations"
            "encoder-input::Push()",
            "ScriptHost::WaitMs(8000)",
            shot("01_files"),  # Print -> file list
            "encoder-input::Push()",
            f"ScriptHost::WaitMs({load_ms})",
            shot("02_preview"),
            "encoder-input::Push()",
            "ScriptHost::WaitMs(5000)",
            shot("03_after_print"),
            "encoder-input::Push()",
            "ScriptHost::WaitMs(5000)",
            shot("04_after_ignore"),  # Ignore calibrations
            # a file not sliced for this printer gets a compatibility warning: go to PRINT
            "encoder-input::Twist(-1)",
            "ScriptHost::WaitMs(2000)",
            "encoder-input::Push()",
        ]
        for i in range(20):
            script += [
                "ScriptHost::WaitMs(3000)",
                shot(f"{10 + i:02d}_result")
            ]
        qemu(run, usb, kernel, script, 3600)
        for f in run.glob("*.png"):
            shutil.copy(f, out / f.name)
        shutil.copy(run / "qemu.log", out / "qemu.log")

    seen: dict[str, list[str]] = {}
    for name in shots:
        png = out / f"{name}.png"
        if png.exists():
            for k in classify(png):
                seen.setdefault(k, []).append(name)
    if "blocked_firearm" in seen:
        result = "blocked"
    elif "blocked" in seen:
        result = "blocked_other"  # no memory / unreadable - fail closed, but not a detection
    elif "printing" in seen:
        result = "prints"
    else:
        result = "unknown"
    ok = result == a.expect and not ("printing" in seen
                                     and a.expect == "blocked")
    print(
        json.dumps({
            "gcode": str(gcode),
            "expect": a.expect,
            "result": result,
            "pass": ok,
            "screens": seen,
            "screenshots": str(out)
        }))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
