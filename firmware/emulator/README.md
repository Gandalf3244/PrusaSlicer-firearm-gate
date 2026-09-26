# Emulator test of the printer firmware

Runs the MK4 firmware with the firearm check in [Mini404](https://github.com/vintagepc/MINI404)
(QEMU with Prusa boards; Linux x86-64), tries to print one G-code file from a virtual USB drive
and reads the result off the emulated display.

```bash
cd firmware/emulator
bash setup.sh                  # once: downloads Mini404 + 3 libraries into ~/.cache/firearm-check-emulator
python3 run_test.py --firmware firmware --gcode samples/heatbed_cable_clip.bgcode --expect prints
python3 run_test.py --firmware firmware --gcode /path/to/gun_part.bgcode --expect blocked
```

It prints one JSON line (`"pass": true / false`, which screens were seen) and exits 0 / 1, and
keeps the screenshots in `--out` (default `emulator_screens/`). A run takes about 3 minutes; the
first run of a firmware build takes about 3 more while the firmware installs its fonts and images
into the emulated flash (cached per build). Needs Python 3 with Pillow and numpy. This
repository contains no gun G-code: bring your own file for `--expect blocked`.

| path | what |
|---|---|
| `setup.sh` | downloads Mini404 `dev` (checks the sha256 of the tested build, warns if it changed) and unpacks `libglut3.12`, `libglew2.2`, `libslirp0` from `apt download` (Ubuntu, no root). Uses `downloads/` instead if you put the files there. |
| `run_test.py` | boot, dismiss "complete calibrations", Print, open the file, Print, Ignore, answer a compatibility warning with Print, then a screenshot every 3 s |
| `firmware/` | the firmware build the emulator runs (`mk4_release_noboot.bin` + `.bbf`: same code as `../mk4_firearm_check_6.10.3.bbf`, without the bootloader) |
| `fixtures/Prusa_Mk4_eeprom.bin` | printer settings: English, MK4S, setup wizard done, selftests not run |
| `refs/*.png` | screen crops that identify "Print blocked", "This file contains a firearm part", "PRINTING" |
| `samples/heatbed_cable_clip.bgcode` | a non-gun part (Original Prusa i3 heatbed cable clip) that must print |

To test a firmware you built yourself, point `--firmware` at its `build/products`.

Notes: the emulator needs `-icount shift=4` (faster settings crash the firmware); its fan model
is imperfect, so a started print stops after ~30 s with "Hotend fan not spinning" - after the
printing screen has been seen. Re-plugging the USB drive, power loss and serial printing cannot
be emulated, so those paths are not covered by this test.
