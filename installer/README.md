# AaltoFlow Setup.exe

A Windows installer for the suite. Whoever installs it ticks the modules they
want; each one lands as a folder with its own Python environment — the layout
mission-control expects.

```
installer\
  AaltoFlow.iss           the wizard (Inno Setup 6): folder, shortcuts, uninstall
  gen_components.py    turns the modules' module.toml into the checkbox list
  postinstall.ps1      builds the Python environments (uv sync per module)
  build_installer.ps1  stages the committed code + uv.exe, generates, compiles
  build\, dist\        generated, git-ignored
```

## Building it (developer PC)

Once, for the free compiler:

```powershell
winget install --id JRSoftware.InnoSetup -e --scope user
```

Then, from the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
```

Out comes `installer\dist\AaltoFlow-Setup-<date>-<commit>.exe` (~15 MB).

Two things worth knowing about the build:

- **The checkbox list is generated, not written here.** `gen_components.py`
  reads every `module.toml` in the staged tree — the same files the launcher
  discovers modules from — and emits the `[Components]`, `[Files]` and
  `[UninstallDelete]` sections. Each module's name, description and position
  come from its own manifest. Adding a module to the suite therefore needs no
  change in this folder.
- **It builds from the last commit**, via `git archive HEAD`, not from your
  working tree (which holds `.venv` folders, caches and scan output). A
  Setup.exe should say exactly which revision it carries. Commit first; the
  script warns if you did not.

## What the installed PC gets

- The ticked modules, plus `suite-common` and `mission-control`, which are
  always installed (both the launcher and scan-core import `suite-common`
  through a path dependency — `uv sync` fails without that folder next door).
- A private `uv.exe` in `<root>\uv\`, **not** on PATH. `postinstall.ps1` and the
  launcher look for it there, so the PC needs neither uv nor Python beforehand.
- One `.venv` per module, built from that module's committed `uv.lock`, so the
  versions are the ones that were tested. Which extras a project gets (`gui`,
  `real`) is read from its `pyproject.toml` — `uv sync` drops any extra it is
  not asked for, which is how vna-control loses its VISA driver.
- Start-menu entries: **AaltoFlow Mission Control**, **<setup> Measurement Suite**
  (with scan-core), **Rebuild Python environments**, and an uninstaller — all
  with icons rendered from the suite's own `icon.svg` files, so a shortcut shows
  the same picture the launcher draws on its card. Since 2026-09-23 the two
  application shortcuts also declare an `AppUserModelID` matching the one each
  program claims at startup (`apply_window_icon`), which is what lets the
  taskbar find an icon for a running window and lets it be pinned. The GUI
  *windows* carry their module's icon too, as of the same date.

Installing per user needs no admin rights, and defaults to
`%LOCALAPPDATA%\Programs\AaltoFlow`. Setup refuses a folder inside OneDrive
(OneDrive locks files inside `.venv`, so `uv sync` fails with "Access is
denied") and a path longer than 80 characters (the Python packages nest deeply
and can cross Windows' 260-character limit). A per-user install also cannot
create a folder directly in `C:\`.

## Why it does not need admin rights (and what that cost)

Installing per user should never need elevation, and it does not — but the
first attempt failed at every `uv sync` with

```
failed to query metadata of ...\cpython-3.14-windows-x86_64-none\python.exe:
The path cannot be traversed because it contains an untrusted mount point. (os error 448)
```

uv stores its managed interpreters as a junction — `cpython-3.14-…` pointing at
the real `cpython-3.14.6-…` — and Windows does not let a process that Setup.exe
started follow one. The same script from a normal window is fine, which is why
the Start-menu **Rebuild Python environments** entry always worked, and why
running Setup as administrator appeared to "fix" it.

The first attempt at a fix was to find the real versioned folder by name and
pass it to uv as `--python`, so that no junction would be involved. **It did not
work**, and it is worth knowing why before anyone tries it again: the log shows
uv was handed `cpython-3.14.6-…\python.exe`, every component of that path is a
real directory, and all fifteen modules still failed with 448. uv **enumerates
its managed interpreter directory before it uses anything**, that directory
holds one junction per installed version, and reading any of them is what
Windows refuses. Pinning the interpreter cannot help, because the scan happens
first.

So the fix is not a better path — it is a different process.
`run_envs_task.ps1` registers a one-shot scheduled task and runs the build
inside it: the Task Scheduler starts that process, not Setup, so none of the
restriction applies. It follows the worker's log in its own window, because a
first build downloads a few hundred megabytes and must not look hung, and it
falls back to running `postinstall.ps1` directly where the scheduler is
unavailable — no worse than before. Verified on the lab PC 2026-09-24 with the
same interpreter that had been failing: 0 errors, 15/15 environments built.

## The one thing that needs internet

Copying files works offline. Building the environments does not: `uv sync`
fetches a Python interpreter and the packages (PySide6, numpy, pyzmq, …) from
python.org, PyPI and — for scan-core — GitHub. On a PC where that is blocked,
as on the lab subnet, untick *"Build the Python environments now"* and run
**Start menu ▸ AaltoFlow ▸ Rebuild Python environments** later from a network that
works. Repeating the step is safe: uv fetches only what is missing.

## Installing silently (to script a lab PC)

```powershell
AaltoFlow-Setup-<version>.exe /VERYSILENT /DIR="C:\Users\you\AaltoFlow" /COMPONENTS="core,scan,inst\kim,inst\camera" /TASKS="buildenvs"
```

Component names are `core` (always), `scan`, and `inst\<module key lowercased>`
— the keys from each `module.toml`. Leave `/COMPONENTS` out to install
everything.

## Upgrading and removing

Running a newer Setup on the same folder upgrades in place and keeps lab data:
any `*.ini`, `*calibration*.json` or `Calibrations\` content in a module folder
is written only when missing. Re-running Setup can **add** modules; unticking
one does not delete it (its folder may hold calibrations) — remove the folder by
hand if you mean it.

Uninstalling removes the code, the shortcuts, the `.venv` folders and the build
leftovers (`__pycache__`, `*.egg-info`, `.pytest_cache`), and leaves your data
files behind.
