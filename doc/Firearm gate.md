# Firearm-part gate

This fork refuses to slice anything the part-identification checker recognises
as a printed-gun part, and tells the user what was recognised.

## What the user sees

Opening a model or changing the plate runs the gate. When an object is
identified, the Slice / Export G-code buttons are disabled and the red
validation notification reads, for example:

```
Slicing refused: the plate contains a firearm part.
The following object matches the reference library of printed-gun parts
(design = identical part, lineage = derivative of a known design,
platform = firearm-family interface, bore = barrel at a bullet diameter):
  "AR-15_Reinforced_Lower_Receiver.stl"
    design   frame/receiver: arlower-full-strengthened1.stl (AR-15_Lower_Receiver_v1-haveblue), 1/1 fingerprints
    lineage  frame/receiver: M-Lower-V3.stl (AR-15_Lower_Receiver_v3-DSL), 2/2 fingerprints
    platform AR platform pattern x10
Remove it from the plate to continue.
```

The command line slicer prints the same text to stderr and exits with status 1.

## How it works

`Print::validate()` (FFF) and `SLAPrint::validate()` (SLA) are the single
place where PrusaSlicer decides whether a print may be processed, for the GUI,
the CLI and G-code export alike. Both now call
`firearm_gate_validate()` (`src/libslic3r/FirearmGate.cpp`) first:

1. For every object on the bed, the model parts are merged with their volume
   transforms and the instance's scaling / mirroring is applied (the printed
   shape; rotation and position are irrelevant to the identification).
2. The mesh is written to a temporary binary STL and the checker is run:
   `firearm-check --json --units mm <stl>`. Geometry inside PrusaSlicer is
   always millimetres, so the checker's unit inference is bypassed.
3. The verdict line is parsed. `BLOCK` becomes the refusal above, with the
   checker's evidence lines; `ALLOW` lets validation continue.
4. Verdicts are cached by object geometry and scale, so moving, rotating or
   re-validating an object costs nothing. Scaling an object re-runs the check.

The gate fails closed: if the checker is missing, crashes, times out or
returns something unparseable, slicing is disabled with a message saying so.
Failures are not cached.

## Configuration

| environment variable | meaning | default |
|---|---|---|
| `PRUSA_FIREARM_CHECK` | checker executable | a bundle at `<resources>/firearm-check/` (private Python runtime in `python/`, the pipeline in `app/`; what the Windows installer ships, see `packaging/windows/`), then `firearm-check` on `PATH`, then `~/.local/bin/firearm-check` |
| `PRUSA_FIREARM_CHECK_TIMEOUT` | seconds allowed per object | 300 |

`firearm-check` is a launcher for the detection pipeline
(`python -m pipeline.check`); the pipeline's own `PIPELINE_STATUS.md`
documents the checker and its measured accuracy (491 reference parts
identified, 0 false positives on 511 non-gun files). Its JSON contract:
one object per input file with `verdict` (`ALLOW` / `BLOCK` / `ERROR`),
`level`, `evidence[]` (`level`, `what`, `body`), exit code 0 / 2 / 1.

## Latency

The checker takes 0.14 s (median) to a few seconds per new object; an 800k
facet mesh took 36 s. The check runs on the UI thread inside validation, so
importing a very large model pauses the interface for that long, once.

## Building this fork (Ubuntu 26.04 notes)

Follow `How to build - Linux et al.md`, plus:

- `sudo apt-get install libhidapi-dev libtool` (2.9.6 needs system hidapi; MPFR's
  `autoreconf` needs `libtoolize`).
- CMake 4.x refuses the old `cmake_minimum_required` of Eigen / OCCT / Boost in the
  deps bundle: export `CMAKE_POLICY_VERSION_MINIMUM=3.5` for the deps and main builds.
- Do not build with more than ~6 parallel jobs on a 32 GB machine; several GUI
  translation units need 3-4 GB each and `-j 16` took the whole machine down.
- The CLI used to exit 0 on a validation error (`process_actions()` returned `1` as
  `true`); fixed in `src/CLI/ProcessActions.cpp` so a refused print exits 1.

Verified with the built binary: `prusa-slicer --export-gcode ar15_lower_mm.stl`
prints the refusal above, writes no G-code and exits 1; a Prusa MINI knob slices
normally; `PRUSA_FIREARM_CHECK=/nonexistent` disables slicing with the reason.
