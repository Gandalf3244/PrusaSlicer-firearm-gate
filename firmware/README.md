# Printer firmware with the firearm check (Prusa MK4)

The slicer gate stops a gun part from being sliced in this PrusaSlicer. This folder stops it on
the printer: Prusa-Firmware-Buddy **6.10.3** for the **MK4 / MK4S / MK3.9 / MK3.9S** (xBuddy
board) with a G-code checker built in. The printer reads the whole file before it prints and
refuses it if it contains a known printed-gun part - whichever slicer made the G-code.

| path | what |
|---|---|
| `mk4_firearm_check_6.10.3.bbf` | the firmware to flash (release build, **unsigned** - see Flashing) |
| `prusa-firmware-buddy-v6.10.3-firearm-check.patch` | all changes, `git apply` on Prusa-Firmware-Buddy tag `v6.10.3` |
| `checker/` | the G-code checker (C99, no heap, runs on the STM32F427) and its test drivers |
| `emulator/` | an emulator test: the MK4 firmware in [Mini404](https://github.com/vintagepc/MINI404), one file printed or blocked |

## What the printer does

1. **Before every print from a file** (USB drive, PrusaLink, Prusa Connect, reprint, `AUTO.GCO`)
   the whole G-code is checked while the preview is on the screen. Pressing *Print* waits for the
   result ("Checking the file", progress bar, *Quit*).
2. **A gun part:** "Print blocked - This file contains a firearm part. Printing is blocked."
   Nothing moves.
3. **Fails closed:** if the file cannot be read or there is not enough free memory for the check,
   the print is blocked too, and the screen says why.
4. **No way around the preview:** the print start itself refuses any file that has no passing
   check of that exact file.
5. **Swapping the file mid-print does not work:** plugging a USB drive in while printing pauses the
   print; resuming after a re-plug, the automatic USB-error recovery and resuming after a power
   loss all check the file again first. A failed re-check stops the print (warning XX855).
6. **Serial printing** (OctoPrint etc.) has no file to check first: moves from the serial line
   that extrude while moving X / Y are refused. Homing, jogging, temperatures and extruder-only
   moves still work.

The checker's working memory (55.5 KB) is borrowed from the heap only while it runs; when the
heap is short it uses a 35 KB or 27 KB configuration (2-4 % fewer parts found). Flash use goes
from 1.21 to 1.33 MB of 1.9 MB.

## How good it is

The checker sees holes, bosses, walls and slots in the layers of the G-code - vertical, lying
flat and tilted - and matches them against fingerprints of the same reference library as the
slicer gate (498 of its parts have usable fingerprints). On G-code sliced from the library and from
1721 non-gun files with four slicer setups:

| slicer setup | gun files blocked | with comments stripped |
|---|---|---|
| Cura, Ender-3, flat | 142 / 551 | 84 / 551 |
| PrusaSlicer 0.4 mm, flat | 158 / 551 | 104 / 551 |
| PrusaSlicer 0.6 mm, as modelled | 181 / 551 | 69 / 551 |
| OrcaSlicer X1C, random rotation | 62 / 551 | 34 / 551 |
| non-gun files blocked | **0 / 1721** | **0 / 1721** |

No gun part got another design's label. It catches about a quarter to a third of the gun files
(the full slicer gate on meshes catches all 552): what it misses are parts whose holes are too
short or too steep to show up in the layers, and features the fingerprints need but the layers
lose. It is a second line behind the slicer gate, not a replacement.

## Tested

- **Emulator of the whole firmware** (Mini404, MK4S): a gun part and a 10 MB AR-15 lower are
  blocked with the message; a non-gun part (cable clip) prints. See `emulator/`.
- **Emulator of the checker alone** (Renode, STM32F4): 160 runs, 0 faults, verdicts identical to
  the PC build; 0.29-0.44 s per MB of G-code at 180 MHz (a typical 4 MB file: 1.5 s). See `checker/`.
- **Not tested on a real printer.** In particular the free heap with Wi-Fi / Prusa Connect
  running is unknown: if not even 27 KB are free there, every print is blocked with
  "Not enough free memory".

## Flashing

- The MK4 accepts unsigned firmware only after the **"appendix"** on the xBuddy board has been
  broken off - physical, permanent, and it voids the warranty. Without that the printer refuses
  this file.
- The firmware lives in the printer's own flash; the USB drive only carries the `.bbf`. Put it on
  a FAT32 drive, insert, reset the printer, confirm. Going back = flash an official `.bbf` from
  prusa3d.com the same way.
- Anyone who owns the printer can flash official firmware back: this is a check, not a lock.

## Building

```bash
git clone --branch v6.10.3 --recurse-submodules https://github.com/prusa3d/Prusa-Firmware-Buddy.git
cd Prusa-Firmware-Buddy
git apply /path/to/PrusaSlicer-firearm-gate/firmware/prusa-firmware-buddy-v6.10.3-firearm-check.patch
python utils/build.py --preset mk4 --build-type release --bootloader yes   # build/products/mk4_release_boot.bbf
```

Python 3.8-3.12 (the pinned requirements do not install on 3.14) and gettext's `msgfmt` are
needed. The patch adds `src/common/firearm_check/` (the checker from `checker/` plus the glue),
the preview gate, the print-start backstop, the re-checks, the serial filter, the warning XX855,
`doc/firearm_check.md` and the emulator test (`utils/firearm_check_emulator/`, same as `emulator/`).
`-D FIREARM_CHECK_HEAP_DIAG:BOOL=ON` shows heap numbers instead of the out-of-memory message.
