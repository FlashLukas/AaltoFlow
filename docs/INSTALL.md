# Installing AaltoFlow

AaltoFlow can be installed three ways. Pick one:

| You want to ... | Use |
|---|---|
| run experiments on a PC, no programming | **A. Setup.exe** (a wizard, no admin rights) |
| run experiments AND get updates with one command, or change code | **B. git** (this page, the main part) |
| install on a PC with **no internet at all** | **C. prepare on another PC and copy** |

Everything starts in **simulation**: no instrument is touched until you tick
*real* for a module in Mission Control (or start its service with `--real`).
All commands below are for **PowerShell** on Windows 10/11, 64-bit.

---

## A. Setup.exe

Someone with the git checkout builds it (needs [Inno Setup 6](https://jrsoftware.org/isinfo.php)):

```powershell
powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
```

Run `installer\dist\AaltoFlow-Setup-<date>-<commit>.exe` on the target PC, tick the
modules that PC needs, and keep *"Build the Python environments now"* ticked
(this step downloads Python and the packages, so it needs internet). Details:
[installer/README.md](../installer/README.md).

---

## B. Installing with git

### What you need

- **Internet** during the install and every update: GitHub (the code), PyPI and
  python.org (the packages). If your instrument network blocks it, connect the
  PC to another network (e.g. Wi-Fi) for these steps only.
- **A folder on the local disk.** Not inside OneDrive (it locks the Python
  environments: `Access is denied`), and a short path -- the deepest PySide6
  files hit Windows' 260-character limit. `C:\Users\<you>\Documents\AaltoFlow`
  is fine.
- No Python and no admin rights: `uv` downloads its own Python.

### First install

**1. git.** Skip if `git --version` already answers.

```powershell
winget install --id Git.Git -e
```

**2. uv** (the tool that builds each module's Python environment).

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then **open a new PowerShell window**, so it finds `uv`. Check with `uv --version`.

**3. Get the code.**

```powershell
git clone https://github.com/FlashLukas/AaltoFlow.git C:\Users\$env:USERNAME\Documents\AaltoFlow
cd C:\Users\$env:USERNAME\Documents\AaltoFlow
```

**4. Build and check everything** with one script. It builds one environment
per module (including the `real` hardware extra where a module has one), runs
every test, runs a demo scan that writes data files, and runs one live scan
against a simulated magnet. It ends with a table and writes the same to
`deploy_report.txt`. The first run downloads a few hundred MB and takes a while.

```powershell
powershell -ExecutionPolicy Bypass -File tools\deploy_lab.ps1 -Target .
```

**5. Start the dashboard.**

```powershell
mission-control\.venv\Scripts\python.exe mission-control\mission_control.py
```

Every module appears as a card: **Service** starts it, **GUI** opens its window.
The **Measurement suite** button opens the scan builder and the data viewer.

### Using real instruments

1. Install the **vendor driver** of each instrument that is connected -- git
   does not bring these. Which one a module needs is in its README
   (`modules\<category>\<name>-control\README.md`): e.g. NI-VISA, NI-DAQmx,
   Thorlabs Kinesis, IDS peak, Zurich Instruments LabOne.
2. In Mission Control tick **real** on that module's card, then **Service**.
3. Which instruments have actually been tested on hardware, and which are
   simulation-only so far: [VERIFIED_INSTRUMENTS.md](VERIFIED_INSTRUMENTS.md).

Each module **adopts** the instrument's current state when it starts and
changes nothing until you command it.

### Updating

```powershell
cd C:\Users\$env:USERNAME\Documents\AaltoFlow
```

**1. Stop every service** in Mission Control (*Stop* on each card), then close
it. A running service keeps files of its environment open.

**2. Save your settings** (once in a while, and always before a big update):
Mission Control > **Export settings...** writes one zip with every module's
`.ini`, the calibrations and the launcher's settings. **Import settings...**
restores it -- also on a new PC.

**3. See whether you changed tracked files.** Your `.ini` files, calibrations and
`suite_local.json` are *not* tracked by git and are never touched by an update.

```powershell
git status
```

If it lists **modified** files (not just *untracked*), put them aside:

```powershell
git stash
```

**4. Get the new code.**

```powershell
git pull
```

**5. Only if your copy is from before 2026-09-27** (modules directly in the top
folder instead of under `modules\`): move them. Without `--apply` it only shows
what it would do.

```powershell
uv run --no-project python tools\migrate_layout.py
uv run --no-project python tools\migrate_layout.py --apply
```

**6. Rebuild the environments and re-test.** Same script as the install; `-NoLive`
skips the live check, in case a service still holds its port.

```powershell
powershell -ExecutionPolicy Bypass -File tools\deploy_lab.ps1 -Target . -NoLive
```

**7. Optional: the contract check** -- starts every service on spare ports and
checks it answers as it should.

```powershell
uv run --no-project python tools\check_modules.py --live
```

**8. If you stashed in step 3,** put your changes back:

```powershell
git stash pop
```

Start Mission Control again (step 5 of the first install).

### Don'ts

- **Don't run `uv sync` by hand without all the extras.** `uv sync` *removes*
  every extra you do not name, so a plain `uv sync --extra gui` uninstalls the
  hardware packages and `--real` then fails. Use `deploy_lab.ps1`, or
  `uv sync --extra gui --extra real` in modules that have a `real` extra.
- **Don't set `UV_PROJECT_ENVIRONMENT` globally.** Every module then shares (and
  breaks) one environment.
- **Don't edit code on the measurement PC** if someone else develops it: every
  local change has to be stashed and merged at the next update.
- **Don't print non-ASCII** in scripts you add (`→`, `✓`, ...): Mission Control
  reads services through a pipe that cannot encode them, and the service crashes.

### If something goes wrong

| Symptom | Cause and fix |
|---|---|
| `Access is denied (os error 5)` during sync | the folder is in OneDrive, or a service is still running. Stop services; move the folder out of OneDrive. |
| a card says *up (external)* and Stop does nothing | an old service process still holds the port: `netstat -ano \| findstr :5555` (the card's port), then `taskkill /F /PID <pid>`. |
| a service exits at once with code 4 | another service (or another program) already has that instrument open -- one instrument, one service. |
| `--real` says a package is not installed | the `real` extra was removed by a plain `uv sync` -- run step 6 of *Updating*. |
| a GUI shows values that never change from another window | it was started without `--connect` and runs its own private simulation. Open GUIs from Mission Control. |
| `git pull` refuses: *local changes would be overwritten* | step 3 of *Updating* (`git stash`). |

More: the numbered gotchas in [DEVELOPER_NOTES.md](DEVELOPER_NOTES.md).

---

## C. A PC with no internet at all

There is no fully offline installer yet. The environments contain absolute
paths, so prepare a complete install on a PC **with** internet in a folder that
will have **the same path** on both PCs, and copy it.

1. Build Setup.exe with that folder as its default (a path without a user name):

   ```powershell
   powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1 -DefaultDir 'C:\Users\Public\AaltoFlow'
   ```

2. On the PC with internet run it into `C:\Users\Public\AaltoFlow`, **unticking**
   *"Build the Python environments now"*. Then build them with Python placed
   inside the folder (otherwise it lands in your user profile and is not copied):

   ```powershell
   $env:UV_PYTHON_INSTALL_DIR = "C:\Users\Public\AaltoFlow\python"
   powershell -ExecutionPolicy Bypass -File C:\Users\Public\AaltoFlow\installer\postinstall.ps1 -Root C:\Users\Public\AaltoFlow
   ```

3. Start Mission Control there once to check it works, then copy the whole
   folder to a USB disk (several GB).
4. On the offline PC run the same Setup.exe into the same folder (environments
   unticked -- this makes the Start-menu entries), then copy the folder from the
   USB disk over it.
5. Bring the vendor drivers on the USB disk too.

This route is new; if Mission Control or a service does not start, the error
names the path it expected -- usually the folder is not exactly the same.
