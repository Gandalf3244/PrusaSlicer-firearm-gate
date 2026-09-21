# Windows installer for PrusaSlicer with the firearm gate

The result is one file, `PrusaSlicer-FirearmGate-<version>-win64.exe`. Running it
installs PrusaSlicer, the checker with its own private Python runtime and the
Visual C++ runtime, and adds Start-menu / desktop shortcuts. The user needs
nothing else; PrusaSlicer finds the checker at `resources\firearm-check\` next
to itself.

## Option A - GitHub Actions builds it (no Windows machine needed)

Push this repository to GitHub (the `firearm-gate` branch) and run the
"Windows installer (firearm gate)" workflow (Actions tab → Run workflow), or push a
tag `gate-v2.9.6` to get it attached to a GitHub release. The first run takes
~3 h (the dependency bundle), later runs ~1 h thanks to the cache. The workflow
also runs the checker and the gated `prusa-slicer-console.exe` on Windows and
fails if a known gun part is not refused or a plain part does not slice.

Download the artifact `PrusaSlicer-FirearmGate-<version>-win64` from the run.

## Option B - build on a Windows PC

Prerequisites: Visual Studio 2022 with "Desktop development with C++", CMake,
git, Python 3.10+, NSIS 3 (https://nsis.sourceforge.io, adds `makensis` to PATH).
Use a short path without spaces, e.g. `C:\src`.

Open "x64 Native Tools Command Prompt for VS 2022":

```bat
set CMAKE_POLICY_VERSION_MINIMUM=3.5
cd C:\src
git clone --branch firearm-gate <this repository> PrusaSlicer
cd PrusaSlicer

rem dependencies (~2 h, once)
mkdir deps\build && cd deps\build
cmake .. -G "Visual Studio 17 2022" -A x64 -DDEP_DEBUG=OFF
cmake --build . --config Release -- /m
cd ..\..

rem PrusaSlicer
mkdir build && cd build
rem Release only: the deps have no Debug libs, and FindOpenVDB on MSVC wants them otherwise
cmake .. -G "Visual Studio 17 2022" -A x64 -DCMAKE_CONFIGURATION_TYPES=Release -DCMAKE_PREFIX_PATH="C:\src\PrusaSlicer\deps\build\destdir\usr\local"
cmake --build . --config Release --target PrusaSlicer_app_gui PrusaSlicer_app_console PrusaSlicer_app_gcodeviewer PrusaSlicerDllsCopy -- /m
cd ..

rem checker runtime + installer
python packaging\windows\make_checker_runtime.py firearm-check checker-runtime
python packaging\windows\build_installer.py --build build\src\Release --source . --checker checker-runtime --out installer --version 2.9.6
```

`installer\PrusaSlicer-FirearmGate-2.9.6-win64.exe` is the installer.

Quick check before shipping:

```bat
installer\stage\prusa-slicer-console.exe --export-gcode --output x.gcode firearm-check\tests\blocked_ejector_arm.stl
```

must print "Slicing refused: the plate contains a firearm part" and exit 1;
the same with `allowed_mini_knob.stl` must write `x.gcode`.

## What the pieces are

| path | role |
|---|---|
| `firearm-check/` | the checker: `pipeline/`, `data/fingerprints.json`, `data/family_patterns.json`, `scripts/sldprt2stl.py`, two test parts |
| `make_checker_runtime.py` | official embeddable CPython + pinned Windows wheels (numpy, scipy, trimesh) + the checker → `python\` and `app\` |
| `build_installer.py` | stages exe/dll + `resources\` + the runtime + `vc_redist.x64.exe`, runs `makensis` |
| `installer.nsi` | NSIS script: install dir, shortcuts, uninstaller, quiet VC++ runtime install |
| `src/libslic3r/FirearmGate.cpp` | the gate; looks for `<resources>\firearm-check\python\python.exe` first |
