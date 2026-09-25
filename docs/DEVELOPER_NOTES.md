# AaltoFlow developer notes

The technical ground rules of the suite: architecture, the wire contract every
module speaks, shared conventions, and the hard-won gotchas. Code comments refer
to these notes by section and gotcha number ("gotcha #25", "section 4"), so the
numbering is kept stable; sections 0, 1, 7, 10, 12 and 13 of the original notes
are not part of the public documentation (each module's own README covers it).

## 2. Environment (Windows, uv, OneDrive)

### Why OneDrive is a problem: `uv` + `.venv` file locking
OneDrive syncs and locks files inside a project's `.venv`. `uv sync` then fails
with `Access is denied (os error 5)` (seen when deleting `cv2.pyd`) or leaves a
corrupted numpy (`InvalidVersion: None`). `clMag-control\.venv` and
`smb-control\.venv` currently sit **inside OneDrive**. T: is not synced by
OneDrive, but `dev.ps1` warns that mapped network drives can cause the same
locking, so venvs should live on local disk either way.

### The `UV_PROJECT_ENVIRONMENT` gotcha (it has already bitten us)
At one point a **User-level** `UV_PROJECT_ENVIRONMENT` was pinned to *one*
project's venv (`%LOCALAPPDATA%\uv-venvs\kim-control`), so every other project
(for example scan-core) silently reused and corrupted it.
**Rule: never pin it globally to one path. Each project gets its own env.**
- Check first:
  `[Environment]::GetEnvironmentVariable("UV_PROJECT_ENVIRONMENT","User")`
- Clear a bad global value:
  `[Environment]::SetEnvironmentVariable("UV_PROJECT_ENVIRONMENT",$null,"User")`
- Recommended on OneDrive: **`dev.ps1`** (it exists in `clMag-control\`; copy it
  to any other project). It sets the variable *for that shell only* to
  `%LOCALAPPDATA%\uv-venvs\<project-folder-name>` and forwards everything to
  `uv`: `.\dev.ps1 sync --extra gui`, `.\dev.ps1 run pytest`,
  `.\dev.ps1 run scripts/run_gui.py`. If PowerShell blocks it:
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
- On a non-OneDrive local disk, a normal in-project `.venv` is fine.

### Interaction with mission-control
Mission Control launches **services** with the project's own
`.venv\Scripts\python.exe`, so that Stop kills the real process. If the venv
lives in `%LOCALAPPDATA%` (the dev.ps1 approach), there is no `.venv` folder, so
it falls back to `uv run scripts/run_service.py`. That fallback has a wrapper
process and can orphan the service on Stop (section 8). A `taskkill /T` backstop
exists, but if you standardise on external venvs, **teach mission-control to
look in `%LOCALAPPDATA%\uv-venvs\<project>\Scripts\python.exe` as well**.

### Which projects use `--extra gui`
- `uv sync --extra gui`: clMag, smb, stage, piezo, camera, kim, scan-core.
- plain `uv sync`: zpiezo-control (headless service, no GUI) and mission-control
  (PySide6 is a base dependency).
- Python ≥ 3.11 (scan-core pins `>=3.11`; everything was verified on 3.11).

---

## 3. Architecture overview

```
suite-common     ── module discovery: <root>/*/module.toml + suite_local.json (this PC)
mission-control  ── discovers modules, spawns run_service.py / run_gui.py (never imports them)
scan-core suite  ── follows the launcher: connects to the discovered modules that are running

CLIENTS                              WIRE                      SERVICES (one process per instrument)
  GUIs (--connect HOST)     ── REQ/REP JSON commands ──►   clMag    Kepco magnet + Hall + AUX DAQ   5555/5556
  consoles (raw pyzmq)      ◄── PUB/SUB status+event ──    smb     R&S SMB100A RF generator        5557/5558
  scan-core (coordinator:                                  stage   Thorlabs BSC203 3-axis stepper  5559/5560
    registry of Settable/                                  piezo   Jena d-Drive + PXY-200 XY       5561/5562
    Gettable wrapping clients)                             camera  IDS uEye+ vision brain          5563/5564
                                                           zpiezo  Thorlabs KCube Z focus          5565/5566
                                                           kim     KIM101 + 3x PIA25 inertia       5567/5568
                                                           hf2     Zurich HF2LI lock-in (detector) 5569/5570
                                                           pm16    Thorlabs PM16 power meter       5571/5572
                                                           vna     PNA-X N5222A or simulated VNA   5573/5574
                                                           mag2d   2-axis vector magnet on NI DAQ  5575/5576
                                                           mag2dcal the same magnet, calibrated seek 5577/5578

ONE of mag2d / mag2dcal runs at a time (same coils): they speak the SAME verbs
and status keys, so everything else is unchanged apart from the id prefix.

vna is a SUBSCRIBER of a magnet's status stream (mag2d by default, mag2dcal or clMag): it
files the field + angle with every trace, and its simulated film sits in that
field. It never commands the magnet.

camera is itself a CLIENT of piezo (XY) and zpiezo (Z) -- or, on the LAB RIG
(hardware.motion="kim", the default since 2026-09-13), of kim for XY AND Z
(ch1/ch2/ch3). See the camera-control README. mission-control's start order
still assumes piezo/zpiezo.
```

- **One service = one instrument = the single source of truth.** GUIs, consoles,
  camera-control and scan-core are all *clients*. A GUI started **without**
  `--connect` runs its **own private local simulation**. This has confused users
  once ("my remote change doesn't show up"): to see the shared state, start the
  service and launch the GUI with `--connect localhost`.
- The code is the same whether the client runs in the same PC or across the lab
  Ethernet. Only the host changes.
- **camera-control owns no motion hardware.** It drives XY through
  piezo-control and Z through zpiezo-control over ZeroMQ.
- **scan-core is THE coordinator** (homegrown, chosen over QCoDeS; see 7.8/7.10).

---

## 4. The suite-wide wire contract (fixed; don't break it)

- **ZeroMQ**, services bind `tcp://0.0.0.0`.
- **Commands:** REQ/REP, JSON. Every reply is `{"ok": true, ...}` or
  `{"ok": false, "error": "..."}`.
- **Fire-and-forget (by design):** a command reply `{"ok": true}` means
  *queued/accepted*, **not done**. Callers poll `status` for the effect (for
  example clMag `field_stable`, or a stage's `moving` going false).
- **Telemetry:** PUB/SUB, multipart `[topic, json]`. Topics `b"status"`
  (5–10 Hz) and `b"event"` (`{"level", "msg"}`).
- **Universal verbs** that every module has: `status`, `info`, `get_config`,
  `set_config`, **`describe`**, **`shutdown`** (2026-09-15: stop cleanly and exit,
  see gotcha #25), plus one verb per setter.
- **`describe` (added 2026-09-10) is how a client builds a UI for a module it has
  never heard of.** It returns a manifest of controls / indicators / actions with
  LIVE limits, units, types, the verb that sets each one, the status path that
  reads it back, and its settle policy. Full spec: `INSTRUMENT_MODULE_GUIDE.md`
  section 6b. Two consumers, two projections of one source: the reconfigurable
  control screen reads the manifest DIRECTLY (it needs buttons, their args, their
  danger flags, group/order hints), while scan-core projects it into
  Settables/Gettables via `scan_core/manifest.py`. Limits are DYNAMIC (clMag's
  field range is the calibration; piezo travel depends on CL/OL; kim's leash
  replaces the clamp), so every status frame carries `describe_rev` and a client
  re-fetches only when that integer moves. **Implemented in ALL SEVEN
  modules (2026-09-10).** 109 parameters across the suite: clMag 20 · smb 6 ·
  stage 18 · piezo 14 · camera 25 · zpiezo 3 · kim 23. hf2 (2026-09-14) adds 38
  and is the first manifest whose SHAPE changes with a mode (freq is a control on
  internal reference, an indicator on external) and whose detectors use an
  `acquire` block keyed on the trigger reply (section 7.11).
- **Port scheme:** instrument *n* (0-based) → `cmd = 5555 + 2n`, `pub = cmd + 1`.
  Since 2026-09-15 the ports are DECLARED in each module's `module.toml` (the
  table below mirrors them) and can be overridden per PC in the launcher; every
  `run_service.py` accepts `--cmd-port/--pub-port/--real`, every `run_gui.py`
  `--connect/--cmd-port/--pub-port`.

  | n | project | package | cmd | pub | family |
  |---|---------|---------|-----|-----|--------|
  | 0 | clMag-control   | `clMag`   | 5555 | 5556 | closed-loop (Controller) |
  | 1 | smb-control    | `smb`    | 5557 | 5558 | set-and-forget (Generator) |
  | 2 | stage-control  | `stage`  | 5559 | 5560 | set-and-forget (Stage) |
  | 3 | piezo-control  | `piezo`  | 5561 | 5562 | set-and-forget (Piezo) |
  | 4 | camera-control | `camera` | 5563 | 5564 | closed-loop vision brain |
  | 5 | zpiezo-control | `zpiezo` | 5565 | 5566 | set-and-forget (ZPiezo), headless |
  | 6 | kim-control    | `kim`    | 5567 | 5568 | set-and-forget (Kim) |
  | 7 | hf2-control    | `hf2`    | 5569 | 5570 | set-and-forget + settle-aware acquire (LockIn) |
  | 8 | pm16-control   | `pm16`   | 5571 | 5572 | set-and-forget + fresh-reading acquire (PowerMeter) |
  | 9 | vna-control    | `vna`    | 5573 | 5574 | array detector, complex trace, real or sim (Analyzer) |
  | 10 | mag2d-control | `mag2d`  | 5575 | 5576 | closed-loop, continuous PI (VectorMagnet) |
  | 11 | mag2dcal-control | `mag2dcal` | 5577 | 5578 | closed-loop, calibrated seek + freeze + stabilizer |
  | 12 | *next module* |          | 5579 | 5580 | |

- `service.py` runs 2 daemon threads: a publisher (owns PUB) and a commander
  (owns REP, `poll(200)`). The loop must never be allowed to die: catch the
  exception, reply with an error, and continue.
- `client.py` is a **brain-compatible facade** (same method names as the local
  brain, so the GUI can't tell whether it has a local brain or a remote client)
  plus a `RemoteStatus`, a SUB cache thread, and REQ under a lock with
  `RCVTIMEO` + rebuild-socket-on-timeout.
- Standalone consoles (`scripts/<inst>_console.py`) speak the **raw protocol
  with pyzmq only, no package import**. Cross-package clients (camera →
  piezo/zpiezo) are also raw pyzmq, never importing the other package, so the
  projects stay decoupled.
- CLI flag inconsistency to know about: GUIs take `--connect HOST`. camera uses
  `--cmd/--pub` for ports, all others `--cmd-port/--pub-port`. On default ports
  no port flag is needed.

---

## 5. Shared code conventions

The full blueprint is `INSTRUMENT_MODULE_GUIDE.md` in the root. **It dates from
2026-08-05 (clMag + smb only)**, so its theme section predates the light/dark
mechanism in section 6. Where they disagree, these notes wins.

- **Layout:** src layout, `uv`, `pyzmq` main dep, `gui` extra = PySide6
  (+ pyqtgraph where plotted), `pytest` dev group. Package name is short and
  lowercase.
  ```
  <inst>-control/  pyproject.toml  README.md  .gitignore
    src/<inst>/  config.py  backends/{base,sim,<real>}.py  <brain>.py  sim_system.py
                 net/{protocol,service,client}.py  apps/{theme,gui,settings_dialog}.py
    scripts/  run_service.py  run_gui.py  <inst>_console.py  smoke_test.py
    tests/    conftest.py  test_config.py  test_<brain>.py  test_net.py  test_gui_smoke.py
  ```
- **Backends:** `backends/base.py` = `typing.Protocol` (`@runtime_checkable`);
  `sim.py` = default simulator; the real driver **lazy-imports** its hardware
  library *inside `open()`*, so the package imports on a PC without VISA/Kinesis.
  That real-driver file is the **only** file that touches the vendor library.
- **Config:** grouped dataclasses (always including a `Limits` safety envelope and
  a `UI` group) + INI save/load. `_cast` handles bool/int/float/str. **The bool
  cast is the classic gotcha**: `bool("False") is True`, so it must parse the
  string. A new config group must be added in **every** place that lists groups:
  `config._sections`/`_GROUPS`, `Config.__post_init__`,
  `protocol.config_to_dict` + `apply_config_dict` (which edits **in place**),
  and for camera also the brain's `set_config` group map.
- **Brain surface:** `start/shutdown/status/get_config/apply_config`, an
  `_on_event` hook, and setters that **clamp to Limits and emit an info/warn
  event** when they clamp.
- **Status snapshots + threads:** see gotcha #1 in section 8 (lost-update race).
- **GUI:** `MainWindow(ctrl, cfg, remote=bool)` + `run_app(...)`; 30–60 ms status
  poll timer; a `Bridge(QObject)` signal to bring events across the thread
  boundary; **one signature `paintEvent` indicator widget per instrument**
  (animated ones get their own ~33 ms QTimer): MagnetIndicator (glowing dipole),
  AntennaIndicator (radiating tower), StageIndicator, PiezoIndicator (XY travel
  map), InertiaIndicator (kim), camera_view (live frame + overlays). Buttons use
  objectName `"primary"` (amber) / `"danger"` (red), `QFrame#card`,
  `QLabel#bigValue` 34 px.
- **Tests (all offline):** config round-trip including bools; brain clamp/lifecycle;
  net round-trip on **non-default ports** (so tests don't collide with a running
  service); GUI smoke guarded by `pytest.importorskip("PySide6")`, offscreen.

---

## 6. Theme mechanism (light/dark, identical in every GUI module)

Now in clMag, smb, stage, piezo, camera, kim, scan-core and mission-control.
zpiezo has no GUI.

- `apps/theme.py` holds `DARK`, `LIGHT`, `THEMES` and **one module-level
  `COLORS` dict = the active palette** (`COLORS = dict(DARK)` initially;
  scan-core and mission-control also export alias `C`, the same object).
- `set_theme(name)` **mutates COLORS in place** (`COLORS.clear();
  COLORS.update(...)`) and **never rebinds** it, because other modules did `from
  .theme import COLORS` and must keep seeing live values. Unknown name → dark.
- `build_stylesheet()` is an f-string over COLORS (it replaced the old
  `STYLESHEET` constant, which some modules keep for back-compat).
  `apply_palette(app)` builds a Fusion QPalette from COLORS (`apply_dark_palette`
  kept as alias in some).
- `run_app` calls `set_theme(cfg.ui.theme)` **before building any widget**, then
  `app.setStyle("Fusion")`, `apply_palette(app)`,
  `app.setStyleSheet(build_stylesheet())`. Painters read `COLORS["key"]` at paint
  time and hold **no baked-in colour constants**. camera keeps legacy
  `theme.ACCENT` etc. working through a module `__getattr__` that maps to COLORS.
- **Startup-only, no live toggle.** Chosen in Settings ▸ Appearance ("applies next
  launch", saved in `cfg.ui.theme`, which also round-trips over
  get_config/set_config) or per launch with `run_gui.py --theme {dark,light}`.
  scan-core and mission-control have no .ini, so they use a
  `DEFAULT_THEME = "dark"` constant + `--theme`. scan-core also re-sets pyqtgraph
  background/foreground after set_theme. A live toggle would need a retheme pass
  for the pyqtgraph plots.
- Keys: `bg, panel, panel_hi, border, text, muted, ok, danger, grid, pressed,
  code_bg, accent, accent_hi, accent_dim`. Dark neutrals: bg `#0e1013`, panel
  `#171a1f`, panel_hi `#1e222a`, border `#2a2f37`, text `#e8eaed`, muted
  `#8b929c`, ok `#3ddc84`, danger `#ff5c5c`, grid `#20242b`. Accent = amber:
  dark `#ff9e2c`/`#ffb454`, light a deeper `#d9821a`. Log/plot/image areas use
  `code_bg`. Keep the shared neutrals identical across modules.

---

## 8. Hard-won gotchas (check these before debugging anything similar)

1. **Threaded status lost-update race.** If a worker thread rebuilds the status
   object each cycle (`st = Status(); ...; self._status = st`) and a setter does
   `self._status.flag = True`, the write can land on the object about to be
   discarded. In camera, `set_stabilize(True)` was silently ignored ~15% of the
   time, and only under load (pytest). **Rule:** live control state lives in
   dedicated brain attributes (`self._stabilize_on`); the worker *copies* them
   into each snapshot; setters never touch the snapshot. This applies to every
   module that publishes status from a thread. A bimodal, load-dependent bug
   ("works instantly or never") points to a race, not slow convergence.
2. **Fire-and-forget means stale status is possible.** Right after a command,
   status can still describe the *previous* target. Always guard with "the service
   has adopted my setpoint" before trusting `stable` or `moving`.
3. **Bool casting from INI:** `bool("False")` is `True`. Use the module's `_cast`.
4. **New config group = several places** (section 5). Forgetting
   `protocol.config_to_dict`/`apply_config_dict` makes the group silently not
   travel over the wire.
5. **`set_config` from a coordinator overwrites the relative "zero here"**
   (stage/piezo/kim keep it in config). Push only the groups you mean to change.
6. **Theme:** never rebind `COLORS`; call `set_theme` before building widgets;
   never cache a colour in a module-level constant.
7. **mission-control / `uv run` orphan:** `uv run` inserts a wrapper process.
   Killing it orphans the real service, which keeps its port (shows as "up
   (external)"). Services therefore launch with the venv python directly;
   QProcess `errorOccurred(Crashed)` also fires when *we* kill on Stop, so only
   `FailedToStart` counts as an error (`_stopping` flag). To clear orphans:
   `netstat -ano | findstr :5555` then `taskkill /F /PID <pid>`.
8. **OneDrive + `.venv`** → `Access is denied`. **A global
   `UV_PROJECT_ENVIRONMENT`** → projects share and corrupt one venv (section 2).
9. **piezo software ramp:** the hardware slew must be 0, or the limiters stack.
10. **kim:** changing the step rate mid-move must re-anchor the sim; changing the
    drive voltage invalidates `um_per_step`.
11. **clMag PI near target:** hard-freeze within tol/2, otherwise the hysteresis
    flip makes it limit-cycle.
12. **Remote vs local GUI:** without `--connect` the GUI runs its own private sim.
13. **Qt checkbox feedback:** use `.clicked` (user-only) and `blockSignals` around
    programmatic `setChecked`.
14. **Non-ASCII in `print()` crashes when stdout is a pipe** (found 2026-09-10).
    A Windows console handles Unicode, but a PIPE gets `sys.stdout.encoding =
    cp1252` — and mission-control captures every service's stdout through a pipe.
    Characters in cp1252 (`·` `±` `°` `…` `—`) survive; anything above U+00FF
    (`→`, `✓`, `≈`, box-drawing, emoji) raises `UnicodeEncodeError` and takes the
    thread down. This killed `scan-core/run_demo.py`. **Keep printed text ASCII**;
    Unicode in GUI labels and comments is fine, since those never hit stdout.
    To reproduce a suspected case, pipe it: `python script.py | cat`.
15. **`h5netcdf` does not install `h5py`** (found 2026-09-10). xarray's
    `to_netcdf` then dies with `ImportError: No module named 'h5py'` — at SAVE
    time, so a long scan runs to completion and only then throws the data away.
    `h5py` is named explicitly in `scan-core/pyproject.toml`. The general lesson:
    a library that dispatches to a backend usually leaves the backend to you.
16. **A per-axis list is truthy even when every entry is False** (found
    2026-09-14). kim declared `settle: {"key": "moving", "index": i}`, but
    scan-core ignored `index`, and `bool([False, False, False])` is `True` -- a
    kim position sweep could never settle. scan-core now resolves `index` for
    every key-based policy and RAISES if a key names a list without one.
17. **An acquire wait can be fooled exactly like a settle wait** (2026-09-14).
    `flag_only` on a busy flag returns on the frame from BEFORE the trigger and
    reads the previous acquisition. Number acquisitions, return the number from
    the trigger, and declare `target_key` so the wait targets THAT number.
18. **Qt number widgets follow the Windows locale** (2026-09-14). On the lab PC a
    10 ms time constant displayed as "10,000". hf2's `run_app` sets
    `QLocale.c()` with `OmitGroupSeparator`; `QDoubleValidator` has the same
    problem, so parse text with `float()` instead.
19. **A child Python's print() reaches a pipe only when it exits** (2026-09-15).
    stdout on a pipe is block-buffered, so the launcher log stayed empty while a
    service ran. The launcher sets `PYTHONUNBUFFERED=1` for everything it starts.
20. **Two copies of one module need distinct prefixes** (2026-09-15). A remote hf2
    and the local hf2 would both register `hf2.r1` and share the acquire group
    `hf2.sample`. scan-core names each connection by its discovery slug
    (`hf2_lab2`) via `endpoints=`/`inst.alias`.
21. **Probing a switched-off remote host blocks for the whole timeout**
    (2026-09-15). Both the launcher and the suite probe on background threads.
22. **One global flag the launcher passes must exist in EVERY script.** clMag's
    service had no `--real`, so ticking "real" crashed it with an argparse error.
    `tools/check_modules.py` checks the command-line contract of every module.
23. **A vendor "is it available?" flag can lie** (2026-09-15, pm16). In a process
    that has created a ZeroMQ context, TLPMX's `getRsrcInfo` reported the free
    PM16 as unavailable, while `TLPMX_init` opened it fine. `list_devices.py`
    worked and the service did not -- the difference was the zmq context.
    Trying to open is the only honest availability test.
24. **Don't name a scratch script after a stdlib module** (`bisect.py`,
    `random.py`, `queue.py`): Python imports it instead of the real one and a
    library deep inside (zmq -> random -> bisect) fails with a baffling error.
25. **A hard kill can leave hardware stuck until it is unplugged** (2026-09-15, pm16).
    On Windows `QProcess.terminate()` / `Popen.terminate()` cannot stop a console
    program gracefully, so the launcher's Stop and its window close always ended
    in a kill. The PM16 service sits inside a 58 ms USB read almost all the time;
    a kill there left the meter answering "I/O error" to every open, reset
    included, until replugged. Fix: a universal **`shutdown` verb** (close the
    instrument, exit), in ALL nine services since 2026-09-15; the launcher sends
    it first and kills only if a service does not answer or does not exit within
    8 s. `check_modules.py --live` checks every service exits on it. Close the REP
    socket with linger, or the reply is dropped. A service started BEFORE this
    change still runs the old code: restart it once.
26. **PowerShell 5.1 `Get-Content -Raw | Set-Content` corrupts UTF-8 files.** It
    reads a BOM-less UTF-8 file as the Windows codepage, so "—" comes back as
    "â€”". Edit text files with the editor tool or Python, never that pipeline.
27. **`open(path)` without `encoding=` is cp1252 on Windows** (2026-09-16). Same
    mojibake, from Python this time: `Recipe.load` read a UTF-8 YAML whose comment
    had a "—", and the garbled text went into every .nc made from it. Found by
    looking at the data viewer's screenshot. Always pass `encoding="utf-8"`.
28. **Clear "busy" and publish the result in ONE critical section** (2026-09-16,
    vna). If an acquisition's `acquiring=False` is set under the lock and the
    sample is written in a second lock section, a status frame between the two says
    "#n finished" while `sample` is still #n-1 -- and a scan waiting for #n reads
    the previous trace, silently. The id guard (#17) does not help: the id is
    already n. Same reason `abort` must LATCH an aborted sample, not just clear.
29. **`uv sync` REMOVES every extra you did not name** (2026-09-17, vna). pyvisa is
    in vna-control's extra `real`; a later `uv sync --extra gui` uninstalls it and
    `--real` fails with "pyvisa is not installed". Sync with
    `--extra gui --extra real`; `tools/deploy_lab.ps1` adds `real` when a
    pyproject declares it, and so does `installer/postinstall.ps1`.
30. **A process started by Setup.exe may not follow a junction** (2026-09-22,
    installer). Installing without admin rights failed at every `uv sync` with
    `failed to query metadata of ...\cpython-3.14-windows-x86_64-none\python.exe:
    The path cannot be traversed because it contains an untrusted mount point.
    (os error 448)`. uv keeps its managed interpreters as a two-part-version
    JUNCTION (`cpython-3.14-...`) pointing at the real three-part-version folder
    (`cpython-3.14.7-...`), and Windows blocks a Setup-spawned process from
    traversing it -- while the SAME script run from a normal window works, which
    is what makes it look impossible. Elevating also works, which is a red
    herring, not a fix. One trap while investigating: reading the ATTRIBUTES of
    those entries is blocked too (so `Get-ChildItem ... | Where-Object {
    $_.Attributes ... }` silently returns nothing -- use
    `[IO.Directory]::GetDirectories` and match on the name).
    **CORRECTED 2026-09-24, and this is the part that matters.** The first fix
    -- `postinstall.ps1` resolving the real folder and passing it as `--python`
    -- was aimed at the wrong thing and did NOT work. The log proves it found
    and passed `cpython-3.14.6-...\python.exe`, and every component of that path
    is a real directory with no junction to follow, yet all 15 modules still
    failed with 448. uv **enumerates its managed interpreter directory before it
    uses anything**, that directory holds one junction per version, and reading
    any of them is what is refused. Pointing at the right interpreter therefore
    cannot help: the scan comes first. The fix is not a better path but a
    different PROCESS -- `installer/run_envs_task.ps1` registers a one-shot
    scheduled task, which the Task Scheduler starts rather than Setup, and
    follows its log so a long build does not look hung. Verified: same
    interpreter, same managed directory, 0 errors, 15/15 built. The general
    lesson is the one this cost a day to learn twice: when a workaround for a
    path problem does not work, check whether the error is about the path you
    fixed -- here the failing path had no junction in it at all.
31. **A Windows console titled "Select ..." is FROZEN, not busy** (2026-09-22).
    Clicking inside a console with QuickEdit on enters selection mode and
    suspends the program at its next write -- a long install looks hung halfway
    through. Press Esc (or Enter) to release it. Worth knowing before debugging
    a "stuck" install or service window.
31b. **Inno Setup SILENTLY IGNORES `/DIR` with forward slashes** (2026-09-24,
    and it did real damage). `Setup.exe /VERYSILENT /DIR=C:/Users/me/test`
    does not error and does not warn -- it installs to the DEFAULT directory
    instead. A "safe isolated test" therefore ran straight over the live
    install. Worse, because the default group name is also used, the test's
    `[Icons]` OVERWROTE the real install's Start-menu shortcuts, its Desktop
    shortcut and its Add/Remove Programs entry, all now pointing at the test
    folder; the files of the real install were untouched, so nothing looked
    wrong until the shortcuts were inspected. Use BACKSLASHES, and before
    trusting anything else confirm from `/LOG` that Setup named the directory
    you asked for. Repairing it took uninstalling the test copy (its uninstaller
    removes the hijacked group and registry entry) and re-running the real
    installer to recreate them.
32. **Windows caches a taskbar icon against the AppUserModelID, not against the
    process** (2026-09-23, the measurement suite). The suite's taskbar button
    showed the blank "unknown application" icon while its title bar and Alt-Tab
    were correct -- and the window really did hold the right icon
    (`WM_GETICON` returns handles that render as scan-core's own drawing). Four
    windows identical but for one variable found it: the same SVG under the ID
    `Aalto.AaltoFlow.scan-core` -> blank; the same multi-size `.ico` under that ID
    -> still blank; the same SVG under an UNUSED ID -> correct at once. The
    cause was `apply_window_icon` claiming the ID *unconditionally* and only
    then setting the icon `if ICON_FILE.exists()`: ONE run without the icon
    file poisons that ID for good, and a later run with a good icon does not
    repair it. Fixed in all 13 modules (missing file -> return, ID claimed only
    behind an icon actually set) and the installer's shortcuts now declare the
    same ID, which also makes a running window pinnable. **Repairing an already
    poisoned ID needs the shell, not the code**: restarting Explorer cleared it
    (verified on the lab PC). Two lessons beyond Windows trivia: a symptom that
    appears in ONE surface (taskbar) but not its siblings (title bar, Alt-Tab)
    names the subsystem, since only the taskbar reads the AppID; and when every
    measurement says the code is right, vary one input at a time against a live
    system rather than re-reading the code.

---

## 9. Verifying work (you can now run everything locally)

Verify **in place**, on the PC the suite runs on:

```powershell
cd "<root>\kim-control"
.\dev.ps1 sync --extra gui        # or: uv sync --extra gui   (copy dev.ps1 from clMag-control if missing)
.\dev.ps1 run pytest -q
.\dev.ps1 run python scripts\smoke_test.py
```

Expected test counts (measured 2026-09-15; scan-core/vna/mag2d 2026-09-17): clMag 22 · smb 29 ·
stage 50 · piezo 37 · camera 111 · zpiezo 14 · kim 85 · hf2 52 · pm16 44 · vna 96 · mag2d 46 ·
mag2dcal 96 · scan-core 262 · mission-control 12 · suite-common 45 = **1001**
(2026-09-24, evening)
(+ aaltoview 42, own repo). Plus the contract check:
`python tools/check_modules.py --live` (107 checks, 0 failed on 2026-09-19).

**Offscreen GUI render**: this is now a tool, not a recipe to retype ---
`tools/render_panels.py` (one panel, run from inside that project) and
`tools/render_all.py` (all of them). Output goes to `front-panels\`, and each
module README embeds its own. Refresh after any GUI change:

```powershell
python tools/render_all.py                     # all 8 panels
python tools/render_all.py clMag --theme light  # one, in the light palette
```

Four things it has to get right, all learned the hard way on 2026-09-10:
- **`QT_QPA_FONTDIR`.** The offscreen platform uses Qt's *basic* font database,
  which on Windows is EMPTY --- every label renders as a tofu box. Point it at
  `C:\Windows\Fonts`.
- **Pin the application font AFTER the widgets exist.** clMag and smb declare a
  base `font-family` in their stylesheet; stage, piezo, camera and kim do not and
  inherit the app font. Every `run_app` calls `app.setStyle("Fusion")`, whose
  re-polish drops a font pinned earlier --- which is why some panels came out
  clean and others rendered "POSITION" as "P-SITI---".
- **Start the brain.** Only clMag and smb start it in `MainWindow`; stage, piezo,
  kim and camera have `scripts/run_gui.py` do it. stage and kim *look* fine
  without it because their sims interpolate position from the wall clock, but
  piezo's software ramp needs the thread (readout frozen at 0.000 while the log
  reports the move) and camera never grabs a frame ("no frame").
- **Pump the event loop, and drive the sim first.** Panels poll status at
  30--60 ms and several indicators animate on their own ~33 ms timers, so an
  immediate grab catches empty readouts. Each target has a `warm_up` that puts
  the instrument somewhere interesting; watch the per-module travel limits
  (a negative stage target just clamps to 0) and speed presets (kim defaults to
  SLOW, 300 steps/s, so a big target is still crawling a minute later).

Render **both themes** when checking a GUI change, and look for dark-on-dark /
light-on-light.

**Network tests** must use non-default ports so they don't collide with services
you have running.

---

## 11. Hardware-pass checklist (per module, on the lab PC)

General: uncomment the vendor dep in `pyproject.toml`, `uv sync`, run the service
with `--real`, exercise it from the console first, then the GUI, and fix every
`# VERIFY`. Keep the sim path working.

- **clMag:** `pyvisa` + `nidaqmx` (+ NI-VISA/NI-DAQmx runtime). Confirm the GPIB
  address, Kepco SCPI, Hall conversion constants, and AUX channel names in NI
  MAX. **Retune PI** on the real magnet. Test the safety ramp-down on
  kill/disconnect.
- **smb:** `pyvisa`. Check the `PHAS` unit, the `OUTP:STAT?` reply format, and
  widen the freq/power limits to the installed options.
- **stage:** `pylablib` + Thorlabs Kinesis. Check the BSC203 serial, the
  axis→channel map, `scale="stage"` units, and which handle style works:
  `KinesisMotor((serial, channel))` (A) or one multi-channel handle (B).
- **piezo:** `pyserial`. Fill the `_CMD` block in `backends/ddrive.py` from the
  d-Drive manual (verbs, channel indexing, units, slew unit, terminator); COM
  port.
- **zpiezo:** `pylablib` + Kinesis. Serial; the volts vs fraction-of-max mapping in
  `backends/kcube.py`.
- **camera:** install IDS peak + `ids-peak` + `ids-peak-ipl`, confirm the camera in
  IDS Cockpit, `driver="ids"`, run `--real`, tune through the live parameters
  panel, recalibrate `objectives.ini`. It needs piezo (XY) and zpiezo (Z)
  services running.
- **kim:** `pylablib` + Kinesis (`KinesisPiezoMotor`). Serial (starts `97…`),
  channels 1/2/3, measure `um_per_step` per axis at the voltage you will use,
  re-derive `max_steps ≈ 25000 µm / um_per_step`.
- **mag2d:** `nidaqmx` + NI-DAQmx runtime. Which card is `Dev1`; water line polarity;
  what the enable line switches; positive volts -> positive field per axis
  (`ao_sign_x/y`); the temperature scale (the VI says 10e3 C/V -- an LM35 is 100);
  measure `ff_mT_per_V`, retune kp/ki; decide the tolerance (see 7.15); consider a
  DAQmx watchdog so a killed process cannot leave the coils driven.
- **mag2dcal:** the mag2d list applies (same backend file, byte-identical). Then:
  measure the coil time constant tau and set `jump_settle_s` ~ 4 tau and
  `calibration.dwell_s` ~ 6 tau (both sized for the sim's 0.08 s -- the two numbers
  most likely to be wrong); run the FIRST calibration at a small `v_max`, watched;
  check the measured legs really are separated; retune kp/ki/`trim_slew_V_per_s`;
  set `tolerance_mT` from what the VNA needs.
- **vna:** NI-VISA/Keysight IO Libraries, the VISA alias, then the 10 # VERIFY in
  `backends/pna.py` (sweep-done detection, byte order vs a front-panel marker,
  cal-set activation `,0` vs `,1`, SDATA of the corrected measurement, port power).
  `uv sync --extra gui --extra real` (gotcha #29).
- **hf2:** LabOne (HF2 data server + USB driver) + `zhinst-core` matching its
  release. Device id from LabOne, `--real --device devNNNN`. Resolve the VERIFYs
  in `backends/zhinst_hf2.py` side by side with the LabOne UI: PLL nodes and
  `adcselect` numbering for the external reference (and whether the unit needs
  the PLL option), aux fields of the demod sample, the real τ range, and the
  cost of `getSample` at the poll rate over USB.

---

## 11b. Deploying to a machine (added 2026-09-13)

`tools\deploy_lab.ps1 -Target <folder>` puts the whole suite on a PC and proves it
runs: checks git/uv (installs uv if missing; uv brings its own Python), clones or
pulls, `uv sync` for all 9 Python projects (8 until hf2 was added), runs every test suite, runs `run_demo.py`,
then a LIVE check -- starts the clMag service with its venv python (not `uv run`,
gotcha #7), runs `run_lab_demo.py` against it, stops it -- and writes
`deploy_report.txt`. On local disk each project gets `.venv` (what mission-control
expects); inside OneDrive it uses `%LOCALAPPDATA%\uv-venvs` and says so.

```powershell
git clone https://github.com/FlashLukas/AaltoFlow.git C:\AaltoFlow
cd C:\AaltoFlow
powershell -ExecutionPolicy Bypass -File tools\deploy_lab.ps1 -Target .
```

**Its first run on a fresh clone found a real regression** that 297 passing tests
did not: `run_lab_demo.py` asked for detectors `field_measured` / `magnet_current`
-- the ids of scan-core's old hand-written clMag declaration -- while clMag's own
`describe` calls them `measured_field` / `current`. The same instrument had two
id schemes depending on which path built the registry, so a saved recipe could
work against one and fail against the other. Fallback ids now follow the
manifest. Lesson: **unit tests do not cover demo scripts; a fresh-clone end-to-end
run does.** Re-run the deploy script after anything that touches ids or scripts.

Simulation only: vendor runtimes (NI-VISA, NI-DAQmx, Kinesis, IDS peak) are NOT
installed by it -- that is the per-module hardware pass in section 11.

## 11c. The installer (added 2026-09-22)

`installer\` builds **AaltoFlow-Setup-<date>-<commit>.exe** (Inno Setup 6, ~15 MB):
a wizard where you tick the modules to install. Full notes in
`installer\README.md`. It is the "give it to someone else" path; `deploy_lab.ps1`
(11b) stays the developer path, since it also runs the tests.

```powershell
winget install --id JRSoftware.InnoSetup -e --scope user   # once, dev PC only
powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
```

- **The checkbox list is GENERATED from the module.toml manifests**
  (`installer\gen_components.py` -> `build\components.iss`). The launcher
  discovers modules from those same files, so the installer cannot drift out of
  date: a new module is still just a folder with a manifest. Name, description
  and list order all come from `[module]`.
- **Built from the last COMMIT** (`git archive HEAD`), never the working tree
  (which holds `.venv`, caches, scan output). The version names the revision.
- **Bundles your uv.exe** in `<root>\uv\`, so the target PC needs neither uv nor
  Python. `installer\postinstall.ps1` then runs `uv sync` per module -> one
  `.venv` each, which is what mission-control launches services from.
  `suite-common` is always installed and synced first: mission-control and
  scan-core depend on it by PATH, and `uv sync` fails without the folder.
  The `--extra gui` decision is read from each `pyproject.toml`, not listed.
- **That sync step needs internet** (PyPI, the Python download, and scan-core's
  git dependency aaltoview). It is a tickable task, with a Start-menu
  entry "Rebuild Python environments" to re-run it later -- the answer for the
  lab subnet where PyPI is blocked. Repeating it is safe.
- Per-user, no admin. Default `%LOCALAPPDATA%\Programs\AaltoFlow`. Setup REFUSES a
  folder inside OneDrive (gotcha #8) or longer than 80 characters (PySide6's
  deep paths + the 260-character limit) -- checked in `PrepareToInstall` as
  well, so a silent install gets the same guard. Note a per-user install cannot
  create a folder in `C:\` root: it fails with Access denied.
- **Lab data survives upgrades:** any `<module>\*.ini`, `*calibration*.json` and
  `Calibrations\*` is installed `onlyifdoesntexist` and left on uninstall. The
  rule is generic, so a future module is protected without anyone editing the
  installer (it already caught mag2dcal's `Calibrations\`).
- Uninstall removes code, shortcuts and `.venv`, and sweeps the `__pycache__`,
  `*.egg-info` and `.pytest_cache` that `uv sync` created (Setup never installed
  those, so Inno would otherwise leave the folder as a shell of caches).
- Silent: `/VERYSILENT /DIR=... /COMPONENTS="core,scan,inst\kim" /TASKS="buildenvs"`.
  Component names are `core`, `scan` and `inst\<module key lowercased>`.
- **No admin rights needed** -- but getting there took gotcha #30: a process
  Setup starts cannot follow the junction uv uses for its managed Python, so
  `postinstall.ps1` resolves the real interpreter folder and pins it with
  `--python`. Running Setup as administrator also works and was how the first
  real install got through, but it is a workaround, not the fix.
- **Icons:** `installer\make_icons.py` renders every `<module>\icon.svg` into a
  multi-size `.ico` with Qt's SVG renderer -- the same one the launcher paints
  its cards with -- so the Start menu shows the same picture as the dashboard.
  mission-control and scan-core had no icon and got one in the same language
  (grey chassis, amber for what is live). Setup.exe, the Apps & features entry,
  Mission Control, the Measurement Suite and Rebuild all use them.
- **Window icons (2026-09-22):** every GUI now shows its module's icon in the
  title bar, Alt-Tab and the taskbar. `apply_window_icon(app)` lives in each
  module's `apps/theme.py` (the suite's convention: shared appearance code is
  COPIED per module, like `theme.py` itself), and each entry point calls it
  right after creating the QApplication -- 14 of them: the 11 module GUIs,
  mission-control, scan_builder and suite (zpiezo is headless). It does two
  things, and on Windows both are needed: `setWindowIcon` for Qt, and
  `SetCurrentProcessExplicitAppUserModelID`, without which Windows groups every
  pythonw process under one generic "Python" taskbar button and draws PYTHON's
  icon however the window icon was set. Failures are swallowed: no icon file,
  or a non-Windows box, just leaves the default.
- Every declared extra is synced (`gui` and `real`), not just `gui` -- see
  gotcha #29.
- Verified 2026-09-22 on this PC: partial install (launcher + suite + clMag +
  camera + kim) -> environments built -> the launcher discovered exactly those
  three modules -> kim's service came up on 5567 -> `apps/suite.py` loaded;
  then a re-install ADDED smb and kept a marker written into `camera.ini` and
  clMag's `Calibrations\`; then uninstall left only the calibration files.

---
