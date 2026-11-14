# gcheck - G-code firearm-part check for the printer's own board

A lightweight version of `pipeline.check` for sliced G-code that runs on the printer's
controller: Prusa **xBuddy** (MK4 / MK3.9 / Core One / XL), STM32F427ZI, Cortex-M4F at
180 MHz, 1919 KB flash after the bootloader, 192 KB RAM shared with the firmware
(Prusa-Firmware-Buddy `ProjectOptions.cmake`: `XBUDDY -> STM32F427ZI`).

## What it does
One pass over the G-code, one line at a time (`gc_line`), no dynamic memory:

1. **Loops** - consecutive extrusion moves form paths; a path that closes on itself and fits
   a circle (single-precision Kasa fit on its buffered points, residual <= 0.02 + 0.004 r mm,
   enclosed area ~ pi r^2) is a circular loop. G2/G3 arcs are split into <= 10 deg steps.
0. **Feature labels are hints, never a way to hide material** (2026-11-14). A path labelled
   support / skirt / brim / wipe / purge stays out of the occupancy map (so the holes and gaps
   it fills still look empty) and out of the part's extent, but its loops and edges are still
   examined - among themselves, with the rules for unlabelled G-code and a second map that
   holds them (`smap`, not in the tiny preset). A labelled outer wall counts as one; any other
   perimeter is told apart by geometry (one with a partner perimeter on its "empty" side is an
   inner one), so relabelling outer walls as inner hides nothing either.
1. **Layer height** - from the layer under each path, not the layer printed just before:
   footprints (2 mm cells) of the last 3 layers vote, and the highest one under at least half as
   many points as the best wins. PrusaSlicer prints support at its own heights between the
   part's layers (0.6, 0.7 support, 0.9, ...); without labels the bead width came out 1.5x too
   wide there. The map is centred on the middle of the last layer's part material.
2. **Hole or boss** - per layer, concentric loops form a wall family. The innermost loop is a
   hole wall if a 1 mm occupancy bitmap of the layer is empty inside it (hole radius = loop
   radius - w/2, w = the layer's median bead width from E and layer height); the outermost is a
   boss if the ring outside it is empty. Without a label, a loop also needs a concentric partner
   loop one bead width away (support shells and tree branches are single loops) or, for a hole,
   the part's material all around outside it (>= 50 % of the ring; Cura merges a hole's inner
   perimeters with the next feature's, leaving one round loop), and a hole may hold sparse
   material (<= 50 %: support printed inside it).
   A track (below) stays open over layers that printed nothing near it (support layers between
   the part's layers), for up to 1.2 mm.
3. **Vertical cylinders** - the same hole / boss in consecutive layers (centre within 0.15 mm,
   radius within 0.1 mm) is one vertical cylinder with a height range.
4. **Tilted cylinders** - a closed loop that is not a circle is fitted with an ellipse (conic
   A x^2 + B xy + C y^2 + D x + E y + F = 0 with A + C = 1, same in every pose; Sampson residual
   <= 0.02 + 0.004 b mm; 1.01 <= a / b <= 4, i.e. tilt 8 - 75 deg). A hole in a tilted part
   shows up as such an ellipse: semi-minor axis = the radius (+- half a bead; the offset curve
   stays within 0.01 mm of that up to a / b = 3), major axis along the tilt. Concentric
   ellipses make a wall family and pass the same hole / boss tests as circles. The same hole in
   the next layers is an ellipse of the same size whose centre has moved along the major axis
   by tan(tilt) per mm of height; the track's centres are fitted with a straight line, and a
   tilted cylinder needs >= 4 layers over >= 0.8 mm, centres on the line within 0.08 mm, and
   the tilt from the drift equal to the tilt from a / b within 4 deg (tree-support branches
   drift but stay round; a drifting ellipse that is not a cylinder fails the agreement).
   Length = height / cos(tilt) + 2 r tan(tilt) (the layers only show complete sections).
5. **Straight edges** - straight runs (>= 1 mm, 3 deg) of closed wall loops (with comments: the
   outer perimeter only). Per layer, edges are sorted by direction (2 deg bins) and offset; the
   bitmap says which side of each edge is empty. Two parallel overlapping edges with nothing
   between them and empty outside are a slot gap; back to back with material between, a wall.
6. **Slabs and horizontal cylinders** - the same edge pair across layers: a constant separation
   (sd <= 0.05 mm, range <= 0.2 mm, area >= 20 mm^2) is a vertical wall / slot; a separation that
   follows a circle's chord (fit y = s^2/4 + z^2 = 2 zc z + R^2 - zc^2) is a hole / boss lying
   horizontally. A tilted wall / slot cuts every layer in parallel lines too, but their midline
   moves by k per mm of height: a pair that continues no track exactly may join a one-layer
   track within 3.75 mm/mm of drift (tilt <= 75 deg) at the same separation, and a track whose
   midline has visibly drifted follows it. With the midline on a straight line (rms <= 0.05 mm)
   and a constant separation s, the faces' normal is (n, -k) / sqrt(1 + k^2) and their distance
   s / sqrt(1 + k^2). Matching runs in two passes per layer (exact continuations first) so the
   drift window cannot take another face's track. Slabs no fingerprint could use (width outside
   every LOOSE band) are not stored.
7. **Merging** - coaxial pieces of one diameter (a pin hole through two walls, a bore between
   chambers) become one cylinder over their union, with `fill` = the length that has wall.
8. **Matching** - the 925 fingerprints of `fp_table.h` made of holes, bosses, walls and slots
   (498 of the 552 shipped parts), with the full checker's rules: TIGHT = design, LOOSE =
   lineage, k-of-n and the corroboration tier, grips exact-only, `min_L` (against `fill`), `amb`,
   angles and separations of every element pair. Weak shapes: M3-size pairs and stepped tubes
   match at design level only (`GC_LOOSE_SKIP`); pairs of equal holes never (`GC_SKIP_ALWAYS`: a
   Charon lower got a Revenant frame's label); a horizontal cylinder (chord-fitted diameter) does
   not complete a two-element lineage match (a buffer tower got an STI frame's label); the
   same holds for tilted ones.
9. **Bore** - a hole at a bullet diameter, >= 25 mm and 4 d long, >= 30 % of it with wall
   (`BORE_MIN_FILL`), whose coaxial holes cover >= 84 % of the part's extent along its axis
   (any axis: the print's bounding box projected on it; `pipeline.calibers` rules and tables).

10. **Scaled pass** (`GC_SCALED`, 2026-11-13) - only when nothing above fired: the fingerprints
   again at a free uniform scale in `GC_SCALE_LO .. GC_SCALE_HI` (0.95 .. 1.05; a part resized
   before slicing), carrying the interval of scales every diameter and separation allows; TIGHT
   residuals after the scale; a part needs a fingerprint of >= `GC_SCALED_ALONE` (3) elements or
   two at one common scale; grips never. Evidence `GC_EV_SCALED`. On G-code scaled x1.02:
   with comments 18 / 19 / 1 / 13 -> 65 / 122 / 26 / 106 of 551 (Cura / Prusa 0.6 / Orca / Prusa
   0.4), stripped 7 / 5 / 0 / 3 -> 40 / 42 / 17 / 60; negatives unchanged (0 new). Details in
   `PIPELINE_STATUS.md`, "Scaled pass".

It cannot see cones, holes too short or too steep (over ~65 deg) for any layer to hold their
complete elliptical section (a hole of radius r and length L needs L > 2 r tan(tilt)), steep
holes other than horizontal ones, or faces within 15 deg of horizontal.

## Results (2026-11-14: labels as hints, layer footprints, every layer)

Files blocked of 551 per config (Cura / Orca / PrusaSlicer 0.4 / PrusaSlicer 0.6), negatives 0/1721
in every row. "Relabelled": the evasions `mcu_eval.py --relabel` - every label rewritten to support,
or every outer wall to an inner one.

| G-code as handed over | before (full, 55.7 KB) | full (89 KB) | compact (51 KB) | tiny (28 KB) |
|---|---|---|---|---|
| labelled | 157 / 73 / 179 / 191 = 600 | 162 / 69 / 177 / 200 = **608** | 575 | 528 (was 520) |
| comments stripped | 88 / 36 / 113 / 78 = 315 | 121 / 39 / 143 / 160 = **463** | 446 (was 277) | 416 (was 275) |
| everything relabelled support | **0** | 117 / 39 / 142 / 151 = **449** | 438 | 333 |
| outer walls relabelled inner | 439 | 565 | | |
| every 2nd layer, labelled / stripped | | 581 / 449 | | |

Emulator (Renode, 64 files incl. the largest of each config): 0 faults, verdict and cylinder count
identical to the host build on 64/64, stack 896 B, flash 159 KB. Cost: **87 M instructions per MB**
of G-code (was 52 M) = 0.48-0.73 s/MB at 180 MHz; worst file (265 MB, Orca tree supports) 150-225 s.
Most of the increase is labelled support now being examined (a PrusaSlicer file with support has
twice the extrusion moves of its part alone); with comments stripped the host build is ~25 % slower.

What the stripped files lacked, in order of size: support layers between the part's layers broke
every track and the layer height (PrusaSlicer 0.6: 78 -> 132); holes with one round loop or support
inside (Cura: +23); inner perimeters taken for boundaries; the edge buffer (256 -> 512).

## Results (2026-09-25, corpus of `scripts/gcode_corpus.py`, every layer)

| config | full checker (PC) | gcheck, comments | gcheck, comments stripped |
|---|---|---|---|
| Cura Ender-3, flat | 267/551 | 142/551 | 84/551 |
| PrusaSlicer 0.4, flat | 235/551 | 158/551 | 104/551 |
| PrusaSlicer 0.6, as modelled | 197/551 | 181/551 | 69/551 |
| Orca X1C, random rotation | 219/550 | **62/551** | **34/551** |
| negatives (4 configs) | 0 | **0/1721** | **0/1721** |

Wrong-family labels on gun parts: 0 (both modes, every layer and every 2nd layer). History, with
comments / stripped: vertical holes only 358 / 245 files; + horizontal holes, walls, slots 467 /
250; + tilted holes and tilted walls / slots (this version) 543 / 291 - the random-rotation
config went from 0 / 1 to 62 / 34. Every 2nd layer: 538 / 285.

Random rotation, per hole of the library parts (150 files; axis tilt in the print): found 57 % at
8-20 deg, 78 % at 20-40, 45 % at 40-60 (88 % of the 20-60 deg holes >= 8 mm long), 11 % at 60-75,
4 % beyond. Steep and short holes never show a complete ellipse in a layer (L > 2 r tan(tilt) is
needed); that and faces near horizontal are what is left of the gap to the full checker. Walls /
slots are found at about the same rate tilted (~20 %) as vertical in a flat print (27 %).

Emulator (Renode 1.17, STM32F4 platform; 80 files x 2 strides incl. the largest of each config):
0 faults, verdict and cylinder count identical to the host build on 160/160 runs.

| | every layer | every 2nd layer |
|---|---|---|
| instructions per MB of G-code text | 52 M | 46 M |
| time at 180 MHz, CPI 1.0-1.5 | 0.29-0.44 s/MB | 0.25-0.38 s/MB |
| median file (3.8 MB), CPI 1.25 | 1.6 s | 1.3 s |
| files <= 20 MB, worst | 18 s | 12 s |
| worst file (330 MB Orca tree supports) | 86-128 s | 77-116 s |

RAM: one block from the caller, laid out by `gc_init(mem, bytes, caps, stride)`; sizes are chosen
at run time (`gc_caps_t`). Since 2026-11-14 (host sizes; the MCU's are a little smaller): full
89.1 KB, compact 51.3 KB, tiny 28.5 KB - see the table above. Before: full 55.5 KB, compact 35.2 KB,
tiny 27.3 KB on the MCU; with comments / stripped, all 4 configs: full 543 / 288 files, compact
529 / 277, tiny 520 / 275 (that library); 0 negatives and 0 wrong-family labels for all three.
The firmware takes the largest that fits: on the Mini404 emulator's heap (~54 KB free at the
preview, no network) that is compact. Stack high-water 1.0 KB (large scratch arrays
live in the state block: the printer runs the check on a 4 KB thread stack), no heap. Flash 127 KB
(code + fingerprint table + libm). `-DGC_TILT=0`: no tilted cylinders. Not included in the time:
reading the file from USB and decompressing `.bgcode` (the firmware's reader does both already
while printing; the check adds one pass).

## Files
- `gcheck.h`, `gcheck.c` - the checker (C99). Capacity presets in `gcheck.h` / `gcheck.c`;
  overflowing one drops data (recall) and sets `overflow`, never writes out of bounds.
- `fp_table.h` - generated by `scripts/mcu_table.py` from `data/fingerprints.json`.
- `host_main.c` - host driver: G-code text on stdin, JSON verdict on stdout
  (`gcheck_host [stride] [full|compact|tiny]`; `mcu_eval.py --caps`).
- `target/` - bare-metal emulator image: startup, vector table, stack painting, linker script for
  the STM32F427 memory; G-code read from 0x90000000 (stand-in for the USB drive).

## Build and test
The project folder is on Google Drive, which cannot execute binaries: builds go to `~/src/mcu`.
```bash
~/src/mcu/build.sh                                   # emulator images (strides 1-3) + host build
python scripts/mcu_eval.py --bin ~/src/mcu/build/gcheck_host [--stride 1] [--strip]   # corpus, ~70 s
python scripts/mcu_renode.py --per-cfg 10 --strides 1,2                             # emulator, ~20 min
```
Toolchain: Arm GNU Toolchain 14.2.rel1 (`~/src/mcu/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi`),
Renode 1.17.0 portable (`~/src/mcu/renode_1.17.0-portable`).

## Firmware integration
Done for the MK4 / MK4S / MK3.9 firmware 6.10.3: see `firmware/README.md` (patch, build, flashing,
emulator tests). The checker runs in the pre-print scan job with its state borrowed from the heap
(full, else compact, else tiny, else the print is blocked); the preview waits for the verdict; the
print start refuses files without one; resumes after a USB re-plug or power loss check again;
extruding moves from the serial line are refused. Timing on real hardware and the free heap with
networking running still need a real printer.

Evasion notes: feature labels no longer hide anything (item 0): stripping or rewriting them drops
the check to the unlabelled recall (463 of 2204 files at full size, 446 compact, vs 608 labelled),
not to zero. A part printed at a steep angle hides most of its holes from this checker (see Results).
