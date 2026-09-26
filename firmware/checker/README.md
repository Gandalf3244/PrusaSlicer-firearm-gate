# G-code checker for the printer (gcheck)

A streaming firearm-part check of sliced G-code, small enough for the printer's own board: C99,
one pass, one line at a time, no heap, all state in one caller-given block. It runs in the MK4
firmware of `../` and on a PC for testing.

| file | what |
|---|---|
| `gcheck.h`, `gcheck.c` | the checker: `gc_init(mem, bytes, caps, stride)`, `gc_line(s, line, len)` per line, `gc_finish(s)` -> `s->res.block`, evidence |
| `fp_table.h` | the fingerprints (generated from the reference library's `fingerprints.json` by the development project's `scripts/mcu_table.py`) |
| `host_main.c` | PC driver: G-code on stdin, one JSON verdict on stdout |
| `target/` | bare-metal image for an STM32F427 emulator (Renode): startup, vector table, stack painting, linker script; reads the G-code from 0x90000000 |

## How it works
One pass over the G-code, one line at a time (`gc_line`), no dynamic memory:

1. **Loops** - consecutive extrusion moves form paths; a path that closes on itself and fits
   a circle (single-precision Kasa fit on its buffered points, residual <= 0.02 + 0.004 r mm,
   enclosed area ~ pi r^2) is a circular loop. G2/G3 arcs are split into <= 10 deg steps.
2. **Hole or boss** - per layer, concentric loops form a wall family. The innermost loop is a
   hole wall if a 1 mm occupancy bitmap of the layer is empty inside it (hole radius = loop
   radius - w/2, w = the layer's median bead width from E and layer height); the outermost is a
   boss if the ring outside it is empty. Without feature comments, a loop also needs a
   concentric partner loop one bead width away (support shells and tree branches are single
   loops); with comments, support / skirt / brim / wipe tower are skipped by their label.
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

It cannot see cones, holes too short or too steep (over ~65 deg) for any layer to hold their
complete elliptical section (a hole of radius r and length L needs L > 2 r tan(tilt)), steep
holes other than horizontal ones, or faces within 15 deg of horizontal.


## Sizes and speed

State block, sized at run time (`gc_caps_t`): full 55.5 KB, compact 35.2 KB, tiny 27.3 KB
(`gc_state_bytes()`); stack 1.0 KB; flash 127 KB with the table. In Renode (STM32F4, 160 runs of
80 corpus files): 0 faults, verdicts identical to the PC build, 52 M instructions per MB of
G-code = 0.29-0.44 s/MB at 180 MHz; a typical 4 MB file takes 1.5 s, the largest (330 MB, tree
supports) about 1.5-2 minutes. Checking every 2nd layer (`stride` 2) is ~12 % faster and finds
about the same.

## Building

```bash
cc -std=c99 -O2 -o gcheck_host gcheck.c host_main.c -lm
./gcheck_host 1 full < part.gcode            # stride, full | compact | tiny
arm-none-eabi-gcc -std=c99 -O2 -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard \
    -nostartfiles --specs=nano.specs --specs=nosys.specs -T target/stm32f427.ld \
    -o gcheck.elf gcheck.c target/main_target.c -lm            # the Renode image
```
`.bgcode` (binary G-code) has to be decoded to text first on a PC; the firmware reads both.
The corpus, the accuracy scripts and the Renode runner live in the development project.
