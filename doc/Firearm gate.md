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

1. For every object on the bed the printed shape is built: the model parts
   merged with their volume transforms, minus negative volumes, minus
   modifiers that print nothing (0 perimeters and 0 % infill) and, for SLA,
   minus drain holes (CGAL boolean; when the meshes are open or
   self-intersecting, the removed volumes are added inside out, which gives
   the checker the same hole walls). Then the instance's scaling, skew and
   mirroring are applied. Mirrored volumes and instances are re-wound, so a
   mirrored part is not checked inside out (every hole would read as a pin).
   Rotation and position are irrelevant to the identification.
   A shape with more than 1M facets (`PRUSA_FIREARM_CHECK_MAX_FACETS`) is
   first reduced to that by quadric edge collapse - the Simplify tool's - and
   so are the parts and cut volumes before the boolean. The checker needs ~2 KB
   per facet: a 17.7M-facet object took 30+ GB, was killed by the OS twice
   (server, then the one-shot retry) and disabled slicing after minutes of
   swapping. No reference part has more than 820k facets. Of 28 library parts
   densified to 1.2-4.7M facets (edges split, shape unchanged), 27 are still
   BLOCKed after the reduction; the 28th (VZ58 barrel) is already missed at
   the dense resolution without it. 10 densified printer parts stay ALLOW.
2. Each shape is written to a temporary binary STL and checked, all objects
   not yet checked in one request. The checker runs as a server
   (`firearm-check --serve --units mm`), started in the background when the
   PrusaSlicer window opens: Python, numpy/scipy and the reference library load
   once per session instead of once per check (seconds on a slow laptop). Each
   request is one JSON line on its stdin (`{"files": [...], "out": ..., "done":
   ...}`); the verdicts are written to `out`, then `done` is created. If the
   server cannot be started, exits or does not support `--serve` (a custom
   `PRUSA_FIREARM_CHECK` script), the gate runs the checker once per plate
   instead: `firearm-check --json --units mm <stl> <stl> ...`. Geometry inside
   PrusaSlicer is always millimetres, so the checker's unit inference is
   bypassed.
3. The verdict lines are matched to the objects by file name. `BLOCK` becomes
   the refusal above, with the checker's evidence lines; `ALLOW` lets
   validation continue. An object without a verdict line is a failure.
4. Verdicts are cached by the shape-defining volumes (parts, negative volumes,
   modifiers and the options that make them void, SLA drain holes) and the
   instance's shape (M^T M of its linear part and the mirror flag), so moving,
   rotating or re-validating an object costs nothing. Scaling, skewing,
   mirroring or editing a volume re-runs the check.

In the GUI the checks run on a worker thread, not in `validate()` itself:
reducing and checking a dense mesh takes from seconds to a few minutes, and
on the UI thread that froze the window. An object without a cached verdict is
copied (its meshes are shared and immutable, so the user can go on editing the
plate), queued, and refused meanwhile, so Slice / Export stay disabled:

```
Slicing is disabled until the firearm-part check is done. Checking:
  "dense_part.stl" (3495496 facets; reducing a mesh this dense for the check takes a while)
```

When the verdicts are in, the worker asks the Plater to validate again
(`Plater::on_firearm_check_done()`), which now finds them in the cache: the
notification turns into the refusal or goes away, and background processing,
when enabled, starts slicing. Quitting stops a check still running. The
command line slicer checks in `validate()` as before and waits for the verdict.

The gate fails closed: if the checker is missing, crashes, times out or
returns something unparseable, slicing is disabled with a message saying so. A
server killed by the OS mid-check (out of memory) is not retried as a one-shot
run, which would only be killed again.
Failures are not cached: the next validation checks again (in the GUI, once
the failure has been shown for 30 s).

## Configuration

| environment variable | meaning | default |
|---|---|---|
| `PRUSA_FIREARM_CHECK` | checker executable | a bundle at `<resources>/firearm-check/` (private Python runtime in `python/`, the pipeline in `app/`; what the Windows installer ships, see `packaging/windows/`), then `firearm-check` on `PATH`, then `~/.local/bin/firearm-check` |
| `PRUSA_FIREARM_CHECK_TIMEOUT` | seconds allowed per object (a run over n objects gets n times this) | 300 |
| `PRUSA_FIREARM_CHECK_MAX_FACETS` | largest mesh handed to the checker; denser objects are reduced to it first (minimum 10000) | 1000000 |

`firearm-check` is a launcher for the detection pipeline
(`firearm-check/`, `python -m pipeline.check`). Its measured accuracy (560
reference parts identified in any orientation, 0 false positives on 511
non-gun files; parts printed with pins fused into their holes are caught
by stubs the pins leave) and the
tools that build `data/fingerprints.json` from the reference library are
documented in the development project (`PIPELINE_STATUS.md` in "Gun 3d model
detection" on Google Drive), not in this repository. Its JSON contract:
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
