# PrusaSlicer with a firearm-part gate

A fork of [PrusaSlicer](https://github.com/prusa3d/PrusaSlicer) 2.9.6 that
refuses to slice anything it recognises as a printed-gun part, and tells the
user what was recognised. Everything else about PrusaSlicer is unchanged.

```
Slicing refused: the plate contains a firearm part.
The following object matches the reference library of printed-gun parts
(design = identical part, lineage = derivative of a known design,
platform = firearm-family interface, bore = barrel at a bullet diameter):
  "AR-15_Reinforced_Lower_Receiver.stl"
    design   frame/receiver: arlower-full-strengthened1.stl (AR-15_Lower_Receiver_v1-haveblue), 1/1 fingerprints
    platform AR platform pattern x10
Remove it from the plate to continue.
```

The GUI disables Slice / Export with that message; `prusa-slicer --export-gcode`
prints it to stderr, writes no G-code and exits 1. The gate fails closed: if the
checker is missing or broken, slicing is disabled with a message saying so.

## Demo

https://github.com/user-attachments/assets/7816469c-1ee4-42ed-b2d4-52e3a621982c

Also on YouTube: [PrusaSlicer firearm-part gate demonstration](https://youtu.be/ri56BqrvPNU). 






<img width="1907" height="1060" alt="image" src="https://github.com/user-attachments/assets/fd426084-3fa7-45ae-a506-8ba675d99948" />

## How it works

`Print::validate()` (FFF) and `SLAPrint::validate()` (SLA) call
`firearm_gate_validate()` ([src/libslic3r/FirearmGate.cpp](src/libslic3r/FirearmGate.cpp))
before anything else. Each object on the bed is written to a temporary STL at its
printed scale and handed to the checker, `firearm-check --json --units mm <stl>`.
Verdicts are cached by geometry and scale, so moving or rotating an object is free.

The checker ([firearm-check/](firearm-check/)) is a small Python pipeline: it fits
planes, cylinders and cones to the mesh, and matches the resulting constellation
of holes and bosses against 1065 fingerprints of 552 reference parts plus per-platform
patterns. Any of four kinds of evidence blocks a print:

| level | meaning |
|---|---|
| design | a reference part is identified at tight tolerance |
| lineage | the same at loose tolerance: a derivative or re-export of a known design |
| platform | a firearm-family interface is present (e.g. the AR-15 fire-control pocket) |
| bore | a long hole at a printed-gun bullet diameter that runs the whole length of the part (a barrel) |
<img width="2496" height="1598" alt="sheet" src="https://github.com/user-attachments/assets/70223c91-b1d7-4482-b9f5-48a88a378748" />

Grips block only on an exact match of a reference grip (many harmless handles
share a grip's screw holes). Measured on the reference library: 552/552 parts
identified, 0 false positives on 511 non-gun files. Details, the JSON contract and the environment variables are in
[doc/Firearm gate.md](doc/Firearm%20gate.md).

## Getting it

### Windows installer

**Download: [PrusaSlicer-FirearmGate-2.9.6-win64.exe](https://github.com/Gandalf3244/PrusaSlicer-firearm-gate/releases/latest/download/PrusaSlicer-FirearmGate-2.9.6-win64.exe)**
(130 MB, from the [latest release](https://github.com/Gandalf3244/PrusaSlicer-firearm-gate/releases/latest);
all versions are under [releases](https://github.com/Gandalf3244/PrusaSlicer-firearm-gate/releases)).

Run it. Windows SmartScreen will warn that the installer is unsigned: choose
"More info" → "Run anyway". It installs, for 64-bit Windows 10 / 11:

- PrusaSlicer 2.9.6 with the gate (`prusa-slicer.exe`, `prusa-slicer-console.exe`,
  `prusa-gcodeviewer.exe`) under `C:\Program Files\PrusaSlicer-FirearmGate\`, with
  Start-menu and desktop shortcuts and an uninstaller;
- the checker with its own private Python runtime in `resources\firearm-check\`
  (nothing to install separately: no Python, no packages);
- the Visual C++ runtime, quietly, if it is missing.

It replaces a regular PrusaSlicer installation: a stock PrusaSlicer would slice
anything, so the installer uninstalls it first (and stops if it cannot) and opens
.3mf / .stl files in the gated build. The configuration (printers, filaments,
profiles) is kept and shared, so a plate that sliced before still slices, unless a
firearm part is on it. The gated build has no "Check for Application Updates",
which would download the stock installer; updates come from this page. The installer contains no firearm models:
the checker ships as code plus two JSON files of dimensional fingerprints (hole
diameters, spacings and angles), not part geometry.

Each installer is tested before it is published: the workflow runs the staged
`prusa-slicer-console.exe` on a part carrying a reference part's hole pattern
(refused, exit 1, no G-code) and on a plain part (sliced), then installs it over
fake stock PrusaSlicer installs and checks they are removed. Both test parts are
generated during the run (the refused one is a plain block drilled with one
fingerprint's holes), so this repository contains no firearm models.

The installer is built by the
[Windows installer workflow](.github/workflows/build_windows_installer.yml): every
push to `firearm-gate` produces it as a workflow artifact (kept 90 days, GitHub
sign-in required to download), and a tag `gate-v<version>` attaches it to a GitHub
release. Building it yourself is described in
[packaging/windows/README.md](packaging/windows/README.md).

**Linux / macOS from source** – build PrusaSlicer as usual
([Linux](doc/How%20to%20build%20-%20Linux%20et%20al.md),
[macOS](doc/How%20to%20build%20-%20Mac%20OS.md)), then make the checker findable:

```bash
python3 -m venv ~/.venvs/gun3d
~/.venvs/gun3d/bin/pip install numpy scipy trimesh
ln -s "$PWD/firearm-check/scripts/firearm-check" ~/.local/bin/firearm-check
```

PrusaSlicer looks for the checker at `$PRUSA_FIREARM_CHECK`, then
`<resources>/firearm-check/` (the installer layout), then `firearm-check` on `PATH`
or in `~/.local/bin`. Ubuntu-specific build notes are at the end of
[doc/Firearm gate.md](doc/Firearm%20gate.md).

## Layout of the fork's additions

| path | what |
|---|---|
| `src/libslic3r/FirearmGate.{hpp,cpp}` | the gate: export object → run checker → cache verdict → validation error |
| `src/libslic3r/Print.cpp`, `SLAPrint.cpp` | call the gate first in `validate()` |
| `src/CLI/ProcessActions.cpp` | a refused print now exits 1 (upstream exited 0 on any validation error) |
| `firearm-check/pipeline/` | the checker (runtime only: load, fit, match, decide) |
| `firearm-check/data/` | `fingerprints.json` (part fingerprints), `family_patterns.json` (platform patterns) |
| `firearm-check/scripts/` | `firearm-check` launcher, `sldprt2stl.py` (SolidWorks reader) |
| `firearm-check/tests/` | one part that must block, one that must not |
| `packaging/windows/` | NSIS installer, checker-runtime assembler |
| `doc/Firearm gate.md` | behaviour, configuration, latency, build notes |

The reference library, the fingerprint-mining and curation tools that produce the
two JSON files, and the accuracy reports are not in this repository; they live in
the development project ("Gun 3d model detection" on Google Drive, `PIPELINE_STATUS.md`
there). Update the JSON files from that project when the library changes.

## License

PrusaSlicer is licensed under the GNU Affero General Public License, version 3
([LICENSE](LICENSE)); this fork and the checker are distributed under the same terms.
PrusaSlicer is developed by Prusa Research and is based on Slic3r by Alessandro
Ranellucci and the RepRap community. For everything that is not the gate, see the
[upstream project](https://github.com/prusa3d/PrusaSlicer).
