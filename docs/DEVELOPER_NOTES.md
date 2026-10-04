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

The most frequent form of it (2026-09-25, three projects in one afternoon): a
re-sync after a `pyproject.toml` change fails with `failed to remove directory
...\.venv\Lib\site-packages\<package>-0.1.0.dist-info: Access is denied`.
The folder carries a READ-ONLY attribute (`attrib` shows `R`), which OneDrive
sets. Clearing it is enough -- nothing has to be deleted:
```powershell
Get-ChildItem .venv\Lib\site-packages -Directory -Filter *.dist-info |
    ForEach-Object { attrib -R $_.FullName /S /D }
uv sync --extra gui --extra real
```

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

### Folder layout (since 2026-09-27)
```
<root>/
  modules/<category>/<key>-control/   every instrument module, sorted by what it is
                                      FOR -- the `category` in its module.toml:
                                      motion, imaging, detector, source, field,
                                      environment (e.g. modules/motion/kim-control)
  suite-common/  mission-control/  scan-core/     the suite's own projects
  tools/  installer/  docs/  front-panels/  spikes/
```
Why: 35 modules in the root buried the few folders a newcomer should open
first, and the category is already in each module.toml. Only the FOLDER moved:
module keys, ports, package names and the environments'
`%LOCALAPPDATA%\uv-venvs\<key>-control` names are unchanged. Everything finds
modules through ONE function, `suite_common.modules.manifest_paths` (launcher,
scan-core, tools, installer generator); it still accepts a module dropped
straight into the root (the old layout), with a warning from `check_modules.py`.
A module's README links up to the root with `../../../`. See gotcha #36 for
checkouts and installs made before the move.

---

## 3. Architecture overview

```
suite-common     ── module discovery: <root>/modules/<category>/*/module.toml + suite_local.json (this PC)
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
                                                           vna     PNA-X, C1209 or simulated VNA   5573/5574
                                                           mag2d   2-axis vector magnet on NI DAQ  5575/5576
                                                           mag2dcal the same magnet, calibrated seek 5577/5578
                                                           ppms    QD DynaCool: field, T, chamber  5579/5580

ONE of mag2d / mag2dcal runs at a time (same coils): they speak the SAME verbs
and status keys, so everything else is unchanged apart from the id prefix.
In real mode the second one is refused because its DAQ card's address is
already claimed (the address lock, gotcha #37); in simulation both may run.

vna is a SUBSCRIBER of a magnet's status stream (mag2d by default, mag2dcal, clMag or
the DynaCool's ppms): it
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
- **Streams (optional, 2026-09-27) -- for FLY SCANS.** A parameter whose
  descriptor carries `"stream": {"group", "channel"}` can be recorded
  continuously: verbs `stream_start` / `stream_read` / `stream_stop`, replies
  `{"stream": {"t", "values", "delay_s", "overflow", "now"}}` with `time.time()`
  stamps and each channel's lag (a lock-in: order x tau). scan-core's `fly` axis
  moves a stage without stopping and bins the streamed detectors by the streamed
  position. hf2 (all scan detectors), pm16 (power), kim (position_x/y/z) and
  camera (laser_x/y: the laser on the sample, from the tracked template) stream
  so far. A fly axis with `move: <stage>` flies in ANOTHER parameter's
  coordinates (camera.laser_x): the grid, row placement and binning are the
  camera's, the stage only moves; rows end when the camera sees the far edge. Values travel in WIRE units; the descriptor's `scale` applies.
  Spec: `INSTRUMENT_MODULE_GUIDE.md`, "Streams"; `check_modules.py --live`
  checks the verbs wherever a stream is declared.
- **Can the readings be trusted? `hw_error` and `fault` (2026-09-28).** Two
  optional status keys, both strings, both `""` when all is well (a missing key
  also means "fine", so older modules need no change):
  - `hw_error`: the LAST HARDWARE READ FAILED; the values in this frame are not
    the instrument's. Set it when a read raises, clear it on the next good one.
  - `fault`: the module says a measurement NOW would give wrong data and a
    person may be needed (the camera lost its pattern; mag2d lost its water). A
    module may LATCH it until someone clears it; such a module offers an action
    **`clear_fault`** in `describe` (it may refuse while the cause is still
    there).
  scan-core (Lukas's decisions A and B): a settle wait never accepts a frame
  that carries either (it raises after ~1 s instead of timing out on it); the
  engine checks every instrument a scan uses before the detectors of a point are
  read and again after, and on a fault it PAUSES (the measurement suite shows
  the faults and a "Clear fault on <module>" button, resumes by itself when they
  are gone and measures the point again) or, headless, stops with `ScanFault`
  keeping the points so far. A service whose status has not arrived for 2 s and
  that does not answer `status` either is a fault too -- its last frame is
  never served forever.
- **Resonance window (optional, 2026-09-28) -- for SLOW swept detectors.** An
  array detector whose descriptor carries `"window": {"arg": "window", "unit":
  "bin", "min_bins": n}` accepts `window: [i0, i1]` (inclusive BIN indices of its
  full grid) on its acquire trigger, sweeps only those bins and returns the trace
  FULL LENGTH with `null` outside. A recipe's `window` block (scan-core
  `window.py`, `resonance.py`) then sweeps only +- margin around the FMR line a
  Kittel model (in-plane with uniaxial Hk, or out-of-plane) predicts from the
  field (and angle) at each point, corrects mu0 Meff from every clean measured
  line, widens and re-measures a point whose line is not in its window, sweeps
  the full band at the first point and every `full_every`-th, and fills the rest
  from that baseline -- with a `<det>_measured` mask in the file. Stepped scans
  only. Spec: `INSTRUMENT_MODULE_GUIDE.md`, "Resonance window".
- **Target echo (motion settle, 2026-09-28).** A module whose move ends with a
  `moving` flag also publishes the TARGET it is moving to (kim/stage/piezo:
  `target_um`, per axis) and declares
  `{"policy": "adopt_then_flag", "setpoint_key": "target_um", "flag_key":
  "moving", "invert": true, "index": i}` -- `index` applies to BOTH keys; add
  `"tol"` when the echoed target may differ from the request (a stepper rounds
  um to whole steps; a clamped move echoes the clamp). A newer set of the same
  knob ends an older set's wait at once in scan-core (a "stop here" must not
  leave the move's wait waiting for an echo that will never come). Why, and the
  ordering rule the module must follow: gotcha #40.
- **Control: one controller, many viewers (2026-09-29; kim + camera first,
  every module since 2026-09-30 -- the per-module safety verbs are listed in
  the README, "What counts as safety, per module").** Many clients can connect to one service: GUIs
  on several PCs, scan-core, the camera driving kim, scripts, consoles. Lukas,
  for a lab where several people train on one instrument: the FIRST GUI gets
  control, every later GUI opens as a VIEWER (live readouts, nothing can be
  changed), and control changes hands only deliberately. The rules
  (`suite-common/src/suite_common/control.py`, copied byte-identical into
  every module as `src/<pkg>/control.py`, like hwlock):
  - Every request may carry `"client": {"id", "kind", "name", "host"}`;
    `kind` is `gui`, `script` or `machine`. Clients send `heartbeat` every
    2 s; a holder silent for 10 s loses control (a crashed GUI never locks an
    instrument for good).
  - The SERVICE enforces it: `ControlLease.handle(req)` runs first in
    `_dispatch` and refuses a command that changes something unless it comes
    from the holder -- `{"ok": false, "refused": "control", "error":
    "read-only: <who> has control ... take control first"}` (the client raises
    `ControlRefused`). Always allowed: read verbs (`status`, `info`,
    `describe`, `get_config`, anything `get_*` / `read_*` / `list_*`, plus
    extra read verbs the module names, e.g. `stream_read`), the module's
    SAFETY verbs (kim `stop`, camera `kill_af` -- a viewer who sees a stage
    run away must be able to stop it), `shutdown` (the launcher's clean stop,
    gotcha #25), and the control verbs.
  - `kind: machine` bypasses the lock: the camera moving kim during an
    autofocus, scan-core during a scan (Lukas's choice: opening a kim GUI
    must not break a running autofocus). SELF-declared -- the lock guards
    against mistakes between people who follow the rules; it is not security
    (whoever reaches the port can send anything; the firewall is the
    security).
  - Nobody holds control -> everything is allowed, with or without an id,
    exactly as before (a headless setup is unchanged).
  - **Control belongs to a PC, not to one window** (Lukas: "if it is the same
    machine you can leave kim unlocked"; the trainee sits at a DIFFERENT PC).
    Every client whose `host` ("user@PC") names the holder's PC may change
    things, keeps the lease alive and may release it; a GUI on another PC is a
    viewer. `same_pc()` compares the PC part only (any user).
  - **"also driving"**: a machine client that changed something in the last
    10 s is marked `driving` in the status's client list, and every control
    bar (and the suite's Control tab) says "also driving: scan-core" -- a stage
    moving under a person's GUI is never a mystery. Status `control.always`
    lists the verbs a viewer may still send.
  - **One scan at a time per instrument** (Lukas: "no more than one scanning
    core running the same instruments"). scan-core's identity carries
    `role: "scan"`; `engine.run` claims every instrument the scan uses
    (`registry.scan_claim` -> `Lab.claim_scan` -> verb `claim_scan{label}`)
    BEFORE anything moves and releases them at the end (also after an abort
    or an error). While a scan holds the claim, the service refuses another
    scan engine's claim and its changes (`refused: "scan"`, "busy: scan
    '<label>' from <who> ... since hh:mm") -- a second suite on the same PC as
    much as one on another PC; that scan fails with `ScanBusy` before it has
    sent anything. Heartbeats keep the claim alive through long settles; a
    crashed scan frees it after 10 s. The camera's autofocus (machine, no scan
    role), people and safety verbs are not affected. A service without
    control cannot be claimed: the scan runs and logs that it is unprotected
    -- another reason to roll control out to every module. Bars and the
    Control tab say "scan '<label>' running (<PC>)".
  - **A scan needs control** (Lukas, 2026-09-30: "I don't know why you would
    not want this"). The claim is refused while ANOTHER PC holds control of
    the instrument (`ScanBusy` naming the holder, nothing sent); allowed when
    the scan's own PC holds it (the person keeps it afterwards); when nobody
    holds it, the scan's PC takes control for the length of the scan (a GUI
    elsewhere is a viewer meanwhile) and it is freed again at the end.
  - **The measurement suite's Control tab is a PERSON**: its clicks go as a
    "gui" client (`Instrument.gui_command`, one identity per module
    connection, heartbeats from `start_gui_heartbeat`), while the scan engine
    stays "machine". One strip of small chips at the top, one per connected
    module (green ● you / amber ◆ another PC / grey ○ nobody or no control
    yet; "▶" while a scan runs; the tooltip says who and since when; a click
    opens Take control / Release, asking before taking over another PC's
    control) and a padlock on each module in the AVAILABLE tree; it greys the
    knobs and the actions not in `control.always` while another PC holds
    control. It does NOT take control by itself when it connects: with
    nobody holding control everything passes as before.
  - Verbs `take_control{force}` (without force only when free; with force it
    takes over and the old holder becomes a viewer and is told who took it),
    `release_control`, `heartbeat`, `clients`. Status carries `control:
    {"holder", "clients", "lease_s"}`.
  - A SCRIPT is a client like a GUI (Lukas's option C): while a GUI holds
    control it can read and stop, and must `take_control(force=True)` to
    change anything -- visible to the GUI it took it from. The consoles have
    `take` / `take!` / `release` / `clients`.
  - GUI side: `apps/control_bar.py` (master in suite-common, copied like
    control.py) -- a bar under the title ("You have control" + who else is
    connected / "VIEWER -- <who> has control since hh:mm" + Take control, which
    asks before taking it over) and an application event filter that swallows
    input to the window's inputs while viewing. Safety buttons are marked with
    `mark_always(widget)`; dialogs (Settings) are not guarded -- a viewer may
    look, the service refuses the OK. `tools/check_modules.py` checks the
    copies wherever a module has them.
- **Encryption: CurveZMQ (2026-09-30; prototype in kim + camera).** Why: the
  control lock trusts what a client says about itself (`kind`, `host`), and
  anybody on the network can read, command or impersonate a service. How to
  use it: README, "Encryption and keys". How it is built:
  - `suite-common/src/suite_common/secure.py` is the master, copied
    byte-identical into a module as `src/<pkg>/secure.py`, like control.py
    (check_modules compares it). It imports only the standard library at the
    top; zmq only where a socket or a key is made.
  - Per PC, `security_dir()` (%LOCALAPPDATA%\AaltoFlow\security, moved by
    AALTOFLOW_SECURITY_DIR): `security.json` {"keyring": folder},
    `this_pc.key` / `this_pc.key_secret` (the zmq certificate text format,
    metadata `pc`, `host`, `machine`, `addresses`). Per lab, the keyring
    folder: one `<pc>.key` per trusted PC and `policy.json` {"mode": off |
    warn | enforce, "modules": [...] or ["*"]}. Missing anything = "off".
  - Service: `self._guard = secure.secure_server(ctx, [rep, pub], "<key>",
    on_event=...)` between creating the sockets and binding them (it sets
    `curve_server`, a ZAP domain of its own, and starts one
    `secure.ZapHandler` per context -- our own key checker, a plain thread,
    NOT pyzmq's ThreadAuthenticator: see gotcha #43); the commander receives with
    `sock.recv(copy=False)` and, before `_dispatch`, `refused =
    self._guard.check(req, secure.user_id(frame))` -- the ZAP handler sets
    each message's User-Id to the sender's public key. `release_server` in
    `stop()` and on every failed start.
  - Client: `secure.secure_client(sock, host, "<key>")` before EVERY
    `connect` (REQ and SUB, and the rebuilt socket after a timeout). It is a
    no-op unless the policy lists the module, so a client can call it for a
    module that has no secure.py yet (the camera's piezo/zpiezo links do).
    The server key: this PC's own for localhost / its own name, else the
    keyring entry whose `pc`, `host` or `addresses` match the host.
  - The guard: a key must be in the keyring (or be the service PC's own);
    the PC part of `client.host` must be one of that entry's names; `kind:
    machine` needs `machine = yes` (the service's own PC: yes unless the
    keyring says no). `warn` logs each problem once and lets it through.
    Refusal: `{"ok": false, "refused": "security", "error": "refused
    (security): ..."}`. The keyring is re-read at most every 2 s.
  - Generic clients: scan-core's `Instrument` and mission-control's
    `fetch_describe` / `request_shutdown` use `suite_common.secure` with the
    instance name ("kim", "kim_pc-a", "kim@pc-a:5567" all count as kim).
    "Add a service on another PC" does not know the module yet: plain first,
    then each secured module's way.
  - Consoles load `../src/<pkg>/secure.py` by file path (so they still import
    no package), registering it in `sys.modules` first (its dataclasses need
    that).
  - Tests: every conftest of a project that uses secure.py points
    AALTOFLOW_SECURITY_DIR at an empty temp folder, so the security setup of
    the PC running the tests never changes them; `check_modules --live` runs
    its services and probes the same way (it tests the contract, not the
    keys). kim-control/tests/test_secure.py builds a three-PC lab in a temp
    folder (the other PCs reach "pc-a" as 127.0.0.2).
  - Known limits of the prototype: a plain client to a secured module just
    times out (CurveZMQ servers do not answer NULL clients); the keys sit in
    the user's profile, so a second Windows account on the same PC needs its
    own key (`new --pc <pc>-<user>`, with `--address`); the keyring's write
    protection is the whole trust anchor; `scripts/kim_xy_calibration.py`
    (camera) talks plain.
- **Finding instruments (2026-10-01).** Mission Control's *Instruments…*
  lists what this PC can reach; the logic is
  `suite-common/src/suite_common/instruments.py` (no Qt, tested with a fake
  pyvisa / pyserial), the window is `InstrumentsDialog` in mission_control.py.
  Rules, and why:
  - VISA resources ending in `::INSTR` / `::SOCKET` on GPIB, USB or TCPIP are
    asked `*IDN?` (1.5 s timeout: an empty GPIB address costs that much, so
    the scan runs in a thread). Interfaces (`::INTFC`, e.g. the Prologix
    adapter pyvisa-py lists on every serial port) are not listed at all.
  - A serial port is NEVER written to by the scan -- not even through VISA's
    `ASRLn::INSTR` -- only described from pyserial (USB description,
    manufacturer, VID:PID, serial number). `ask_serial` sends `*IDN?` to one
    port, on a click, at the baud rate the person picked.
  - An address in `hwlock.held()` is never opened; the row names the holder.
    VISA's `ASRL5::INSTR` and pyserial's `COM5` are merged into one row
    (hwlock's normalisation; `ASRL/dev/ttyUSB0::INSTR` = `/dev/ttyUSB0`).
  - "Use for module…": a module declares `[hardware] address_arg` / `bus`
    in its module.toml (guide section 11); `instruments.address_for` turns
    a found address into that module's form, `modules.set_address` stores it
    in suite_local.json, `service_args` passes it after `--real` (only then:
    the simulator opens nothing). check_modules checks that the flag exists.
  - The extra `instruments` of mission-control: pyvisa, pyvisa-py (when no
    NI / Keysight VISA is installed), pyserial, psutil + zeroconf (pyvisa-py
    searches the LAN on every network card and finds HiSLIP instruments).
  - **Vendor probes (2026-10-03).** Instruments that are not VISA / COM come
    from two list-only sources, run in a second thread next to the VISA scan:
    (1) `suite_common/usb_devices.py` -- the OS's USB list (Windows: ONE
    PowerShell `Get-CimInstance Win32_PnPEntity` call for `USB\VID_*` and
    `FTDIBUS\*`; Linux: /sys/bus/usb/devices), stdlib only, never raises.
    `KNOWN_USB` names a device and its module; generic FTDI ids (0403:6001,
    0403:6015) are named "could be ..." with NO module, because every
    USB-serial cable looks the same. A composite device's interfaces and the
    FTDIBUS twin of an FTDI device fold into one row; unknown devices are
    hidden unless "show every USB device". (2) module probes: `[hardware]
    probe` in module.toml, run with the module's venv python
    (`instruments.module_python`: `.venv`, then `%LOCALAPPDATA%\uv-venvs`),
    20 s timeout, all in parallel; the contract is in guide section 11.
    `instruments.merge` folds a probe row, a USB-list row and a VISA/COM row
    of one device together (same hwlock key, or the USB serial found in the
    other row's address/lock/details); "Found by" says who saw it.
    Bus `device` (a module takes an id verbatim): a probe's row is offered
    only to the module whose probe found it, a USB-list row only to the
    module KNOWN_USB names (its serial; a `visa` module gets
    `USB0::0x<vid>::0x<pid>::<serial>::INSTR`). Lab finding: a device a
    service has OPEN can be missing from its vendor's list (pylablib's
    Kinesis list was empty while kim held the KIM101) -- the USB list still
    shows it, hwlock marks it held, and each probe reports its own module's
    held addresses, so the row reads "held by the running kim service", not
    "nothing found". Found on the lab PC the same day: (a) Kinesis' FTDI
    scan lists the Signal Hound TG44A too ("SignalHoundTG") -- a probe marks
    such entries `"other": true`, which never suggest its module; (b) TLPMX
    lists a held meter as `USB0::0x1313::0x807B::::INSTR`, serial "n/a" --
    folded into the held row; (c) FTDI's channel letter: pyserial / FTDIBUS
    say `<serial>A`, the `USB\` entry `<serial>` -- the merge compares both
    spellings; (d) a row a running service HOLDS suggests that service's
    module (the best evidence there is, also for an ambiguous FTDI cable).

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
  | 12 | ppms-control  | `ppms`   | 5579 | 5580 | set-and-forget, MultiVu runs the loops (Cryostat) |
  | 13 | kepco-control | `kepco` | 5581 | 5582 | set-and-forget + software ramp + acquire (BipolarSupply) |
  | 14 | windfreak-control | `windfreak` | 5583 | 5584 | set-and-forget, two channels (Synthesizer) |
  | 15 | gsp818-control | `gsp818` | 5585 | 5586 | array detector, real dBm trace + TG thru reference (SpectrumAnalyzer) |
  | 16 | signalhound-control | `signalhound` | 5587 | 5588 | array detector, real dBm trace + TG thru reference (SpectrumAnalyzer) |
  | 17 | dsphase-control | `dsphase` | 5589 | 5590 | set-and-forget (PhaseShifter) |
  | 18 | dssg-control | `dssg` | 5591 | 5592 | set-and-forget (Synthesizer) |
  | 19 | dsamp-control | `dsamp` | 5593 | 5594 | set-and-forget, gain envelope (Amplifier) |
  | 20 | agilis-control | `agilis` | 5595 | 5596 | set-and-forget, open-loop steps (AgilisStage) |
  | 21 | smaract-control | `smaract` | 5597 | 5598 | set-and-forget, closed-loop encoder (1 axis) |
  | 22 | sr830-control | `sr830` | 5599 | 5600 | set-and-forget + settle-aware acquire (DspLockIn) |
  | 23 | cs260-control | `cs260` | 5601 | 5602 | set-and-forget, move-done settle (Monochromator) |
  | 24 | ccs200-control | `ccs200` | 5603 | 5604 | array detector, spectrum + dark (Spectrometer) |
  | 25 | ddr25-control | `ddr25` | 5605 | 5606 | set-and-forget, rotary (Rotator) |
  | 26 | elliptec-control | `elliptec` | 5607 | 5608 | set-and-forget, rotary, bus of addresses (RotationMount) |
  | 27 | chopper-control | `chopper` | 5609 | 5610 | set-and-forget, spin-up settle (Chopper) |
  | 28 | superk-control | `superk` | 5611 | 5612 | set-and-forget, class 4 interlocked (SuperK) |
  | 29 | tc200-control | `tc200` | 5613 | 5614 | closed-loop setpoint, reached = held in band (Heater) |
  | 30 | ls455-control | `ls455` | 5615 | 5616 | fresh-reading acquire (Gaussmeter) |
  | 31 | pm400-control | `pm400` | 5617 | 5618 | fresh-reading acquire (Pm400Meter) |
  | 32 | hp8648-control | `hp8648` | 5619 | 5620 | set-and-forget (SignalSource) |
  | 33 | sr7230-control | `sr7230` | 5621 | 5622 | set-and-forget + settle-aware acquire (lock-in) |
  | 34 | k2450-control | `k2450` | 5623 | 5624 | set-and-forget + fresh-reading acquire (SourceMeter) |
  | 35 | *next module* |          | 5625 | 5626 | |

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
  modules/<category>/<inst>-control/  pyproject.toml  README.md  .gitignore
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
33. **A git dependency pinned by COMMIT breaks when its history is rewritten**
    (2026-09-25). scan-core's `uv.lock` pinned AaltoView at `f1df823`; the
    repo's history was rewritten for the public release (main is now
    `dba8682`), and every `uv sync` of scan-core then failed with
    `fatal: remote error: upload-pack: not our ref f1df823...` -- on every PC,
    fresh installs included, while an EXISTING environment kept working, which
    hides it. Fix: `uv lock --upgrade-package aaltoview` and commit the lock.
    Rewriting a dependency's history means relocking every project that pins it.
34. **On Windows a timed `Event.wait()` sleeps at least one 15.6 ms timer tick**
    (2026-09-27, found building the fly scan). hf2's poll thread ran
    `while not stop.wait(1 / poll_hz)`, and its "50 Hz" was really ~32 Hz; the
    simulator's "200 Hz" stream was ~130 Hz. Nothing looked wrong until a fly
    scan counted a third fewer samples per pixel than the arithmetic said.
    `time.sleep()` uses a high-resolution timer on Windows (Python >= 3.11)
    and is exact to ~1 ms. For a periodic loop: schedule on DEADLINES
    (`next_t += period; time.sleep(next_t - now)`), and check the stop event
    between iterations instead of sleeping on it. `threading.Event.wait`,
    `Lock.acquire(timeout=)` and `Queue.get(timeout=)` all have the coarse
    tick; `time.time()` itself is fine (precise since Python 3.13).
35. **A settle rule that is right for a STEP can end a fly row before it
    starts** (2026-09-27). kim declares its position's settle as
    `flag_only(moving)`. Right after `move_to`, the cached status can still say
    "not moving" (gotcha #2), so the blocking set returns at once -- for a step
    that only costs a point measured early; for a fly scan it ended the ROW
    before the stage had left, with every pixel empty. The fly engine therefore
    ends a row on the MEASURED position (at the far end and at rest, or stalled
    for 1 s -- logged), never on the settle rule alone -- and (found on the rig
    2026-09-28) it waits for the APPROACH to each row's run-in the same way:
    the stale frame had made row 0 start wherever the stage happened to be. General lesson: a
    "done" signal is only as good as the wait it was designed for.
36. **An old flat module folder left behind after the move to modules/**
    (2026-09-27). `git pull` moves the files git TRACKS into
    `modules/<category>/<key>-control`, but the ignored ones -- a rig's tuned
    `.ini`, `Calibrations\`, `px_calibration.json`, `CLAUDE.local.md`, data, a
    `.venv` -- stay in the old `<key>-control` folder, where the module no
    longer looks. Nothing fails; the module just quietly starts from factory
    settings. And if a whole old copy survives (with its module.toml), there are
    two modules with one key. Discovery never resolves that silently any more:
    the `modules/` copy wins and the other is reported as a PROBLEM naming both
    folders (launcher log, `check_modules.py`). The cure on a checkout: commit or
    `git stash` locally edited tracked lab files BEFORE `git pull` (camera.ini,
    objectives.ini, px_calibration.json, clMag's Calibrations), pull (then
    `git stash pop`), run `python tools/migrate_layout.py` (dry run) and
    `--apply` -- it moves the leftovers across without overwriting (a differing
    file is kept as `*.old-layout`) -- then re-sync each module's environment,
    because the editable install inside it still points at the old `src`. An
    INSTALLED suite needs none of this: Setup moves each old module folder
    whole before copying (`MigrateModuleLayout` in `installer/AaltoFlow.iss`),
    and Mission Control's "Add module" moves an old copy instead of making a
    second one.
37. **One instrument is one PHYSICAL ADDRESS, not one module** (2026-09-27).
    Two services commanding one supply fight, and neither knows the other
    exists. The first fix listed pairs of module NAMES that must not run
    together (`[run] excludes`: kepco <-> clMag, mag2d <-> mag2dcal). That was
    the wrong key: clMag's Kepco sits on GPIB0::6 on one rig and could sit on
    another address elsewhere, while any NEW module pointed at GPIB0::6 would
    not be in anybody's list; and a module that drives two instruments on
    different addresses was forbidden for nothing. Lukas: "the same instrument
    has to be defined by the same physical address." So every real backend
    claims its address with `hwlock.claim()` in `open()` (INSTRUMENT_MODULE_GUIDE
    section 3) and a second claim -- from any module -- raises `HardwareBusy`
    naming the holder; the name-based `excludes` is gone. What to know:
    - **Per PC.** It is an operating-system file lock in
      `%LOCALAPPDATA%\AaltoFlow\locks` (tests set `AALTOFLOW_LOCK_DIR`). GPIB,
      USB and serial hang on one PC, so that covers them; a network instrument
      shared between two PCs is NOT protected across the PCs.
    - **A crash cannot leave an instrument "busy".** The OS drops the lock
      when the process ends, however it ends; there is no stale-file cleanup to
      get wrong. `held()` skips a lock file whose lock can be taken.
    - **Windows frees a KILLED process's lock a moment later**, not at once.
      A service restarted straight after a hard kill would be refused by its
      own ghost, so `claim()` retries for up to 2 s (`wait_s`) before it
      gives up.
    - **The pid in the lock is not the pid the launcher started.** A venv's
      `python.exe` is a small launcher that starts the real interpreter as a
      CHILD (the same thing that makes `uv run` orphan services, gotcha #7), and
      the child holds the lock. Mission Control therefore matches a card to its
      locks by module KEY (what the backend passed to `claim`), with the pid only
      as a second chance. The card shows "holds GPIB0::6"; a service refused its
      address shows red "address busy: GPIB0::6 held by clMag (pid N)".
    - **Every module carries a byte-identical copy** of the master
      `suite-common/src/suite_common/hwlock.py`: two copies that normalise
      addresses differently would each think they hold a different instrument.
      `tools/check_modules.py` fails a differing copy.

38. **Two installer traps found by the first upgrade test** (2026-09-28).
    (a) *Inno Setup reads any line starting with `[` as a new section* -- even
    inside a Pascal comment in `[Code]`. A comment line beginning "[Files]
    entries ..." made the whole script fail to compile ("Invalid section tag").
    Nothing but a real build shows this: never start a comment line in the
    code section with a square bracket.
    (b) *Never make a test install by copying a real one.* The copy carries the
    real install's `unins000.dat`, Setup APPENDS to it, and that record holds
    the real install's absolute paths -- running the test copy's uninstaller
    could delete files of the REAL install. For an upgrade test, copy the
    folders but not `unins000.*`, or install the old version fresh into the
    test folder; clean up by hand if in doubt (the uninstall registry entry,
    the Start-menu group). Also: every install runs `[InstallDelete]`, which
    removes the old "TRMOKE" Start-menu group and desktop link -- back them up
    before a test on a PC that has a real install. The upgrade migration itself
    was verified this way: 13 flat folders moved, tuned `camera.ini`,
    `px_calibration.json` and clMag `Calibrations\` byte-identical afterwards,
    old `.venv`s removed, 35 modules discovered, no problems.

39. **A service must bind its ports before it opens the instrument, and must
    answer EVERY request** (2026-09-28, deep cleaning; fixed in all 35 modules).
    Two bugs copied from the templates into the modules' `net/service.py`:
    (a) *Port taken -> deaf service* (33 of 35 modules). The PUB and REP
    sockets were bound inside the two daemon threads. When a port was already in use (a second copy, an
    orphan -- gotcha #7) only that THREAD died, with a traceback nobody reads;
    the process lived on, holding the instrument and its hwlock claim (#37) and
    answering nothing. (b) *One malformed request kills the command port*
    (the 9 modules built from the camera/kim template). Their REP loop did
    `try: recv_json() except: continue`. A REP socket that has received MUST send before it can receive
    again, so skipping without a reply left it stuck: one non-JSON message and
    the service never answered anyone again. **Rules:** `start()` binds BOTH
    sockets in the calling thread first and raises `PortInUse` (a
    RuntimeError) before the brain is started -- `run_service.py` prints one
    line on stderr and exits 2; if the brain then fails to start, close both
    sockets before re-raising. The commander `recv()`s raw bytes, parses and
    dispatches inside one try, serialises the reply BEFORE sending (an
    unencodable reply gets an error reply instead), and always sends
    something. `tools/check_modules.py --live` tests both on every module:
    "exits non-zero when its port is taken" and "malformed request answered,
    port survives".

40. **Settling on the frame from BEFORE the move** (2026-09-28; gotcha #2 in
    its motion-stage form). kim, stage and piezo declared their position settle
    as `flag_only(moving, invert)`: "settled when not moving". Commands are
    fire-and-forget, so for a few status frames after `move_to` is accepted the
    service still publishes the state from BEFORE the command -- and that state
    says "not moving". The blocking set returns at once, the scan reads its
    detectors at the OLD position, and the grid comes out one step behind with
    nothing raised. (It bit the fly scans first, gotcha #35: an approach that
    "arrived" before the stage had left.) **The fix is the target echo:** the
    module publishes the target it is moving to (`target_um`), and the settle is
    `adopt_then_flag(target_um, moving, invert, index)` -- a frame is only
    believed once it shows OUR target, and only THEN is its `moving` trusted.
    **The ordering rule that makes the echo honest:** (1) the setter issues the
    hardware move FIRST and only THEN stores the echo target -- storing it
    first would let a frame show the new target together with a `moving` read
    before the move began ("not moving": settled at the old place again); (2)
    the status worker reads the echo target BEFORE it reads `moving` from the
    hardware -- so any frame that shows the new target carries a `moving` read
    AFTER the command. A STOP sets the echo to where the axis stopped, a clamped
    move echoes the clamped target, and a target rounded to whole steps needs a
    `tol` in the settle block, or the echo never matches and the point waits out
    its timeout. In scan-core a newer set of the same knob ends an older set's
    wait (the fly row's "stop here" supersedes its move), so an echo that will
    never come cannot hold a row for its whole timeout.
41. **A port that times out may simply have nothing listening** (2026-09-29).
    After IT opened TCP 5550-5600 from the office PC to the lab network, every
    connect still hung for 5 s -- because the services had been closed. On the
    Windows firewall's PUBLIC profile (the lab PC's Ethernet) a closed port is
    DROPPED silently, exactly like a filtered one; only a private/domain
    profile answers "refused" at once. So a timeout proves nothing until
    something is known to listen. Test with a bare listener run by the SAME
    interpreter the services use (a program rule allows that python.exe) and
    confirm on the far side that the connection arrived; then start the
    services. The connection test that settled it: a plain `socket.accept()`
    loop on 5563 and 5590, both accepted in 2-19 ms.
42. **Guard a viewer's inputs with an event filter, not `setEnabled(False)`**
    (2026-09-29, control_bar.py). Every GUI switches its own widgets on and
    off from its status timer (during an autofocus, while the stage is down).
    Disabling from outside fights that code, and re-enabling afterwards
    switches on buttons that ought to stay off. An application event filter
    that swallows mouse / wheel / key input to the main window's input widgets
    leaves every enabled state alone; widgets carrying the `control_always`
    property (STOP, Kill AF) pass. `QTest.mouseClick` goes through the
    application's filters, so the guard is testable offscreen.
43. **pyzmq's ThreadAuthenticator dies on Windows without `tornado`**
    (2026-09-30, found on the lab PC). It runs an asyncio loop in its thread,
    and Windows' default (proactor) loop cannot watch zmq sockets unless
    tornado >= 6.1 is installed: "Proactor event loop does not implement
    add_reader ...". The thread died at start, so NO encrypted client was ever
    answered (a secured service was deaf), every later start failed with
    "Address in use (inproc://zeromq.zap.01)", and `stop()` waited forever for
    the dead thread -- a test run hung for five hours. It passed on Linux,
    where the default loop can. `secure.ZapHandler` answers the ZAP requests
    itself: a plain thread and poll, `start()` raises if it cannot run,
    `stop()` returns within ~2 s, a failed bind closes its socket (an open
    socket makes the context's `term()` wait forever), and the loop answers
    "500" rather than dying. Lesson: code that starts a background thread
    must check that the thread is really running, and its stop must have a
    timeout -- a dead helper thread should be a loud error, not a hang.
44. **On a share that maps Linux permissions, a file written from one PC may
    be unreadable from another** (2026-09-30, the lab keyring). A new file
    got read access for its owner and one group only; Windows `icacls` /
    `Set-Acl` could not change it ("No mapping between account names and
    security IDs"). A key file is public, so the fix was to write it from the
    PC whose files everyone can read (or `chmod 644` from a Linux login).
    `Keyring.problems` and `keys.py list/status` now name every key file that
    could not be read, instead of that PC silently missing.
45. **A status poll must not write into a box whose value waits for Apply**
    (2026-10-01, signalhound; the same bug then fixed in ccs200, gsp818,
    shsna, vna and agilis on 2026-10-03). The panels refresh their input boxes
    from status every ~60 ms so they follow changes made elsewhere (console,
    scan, another GUI), and the only guard was `not spin.hasFocus()`. Where a
    box is sent only by an Apply / Set button, that guard protects the ONE box
    being typed in: type a value into Centre, click into Span, and Centre is
    back to the old value before Apply is pressed -- Apply then sends the old
    value ("whenever I change any settings it comes back to the original
    ones"). Rules: (a) a box the user changed is *dirty* until its button (or
    Enter in it) sends it; the poll skips dirty boxes, which get an amber
    outline and a tooltip; (b) the poll's own `setValue` must not count as a
    user edit (a `_syncing` flag, or `blockSignals` as in gotcha #13); (c) an
    action that sets the same values another way (Settings dialog, a preset)
    drops the unsent edits. Two other patterns are safe and were left alone:
    copying a value in only when the SERVICE's value changed since the last
    copy (hf2, sr830, sr7230, kim's step-size and leash boxes), and syncing a
    form only on events, never on the poll (camera). A box whose value is sent
    the moment it changes is not affected either.
46. **A fake stream sampled by a test thread measures the test machine**
    (2026-10-01, scan-core fly tests). The fly tests failed now and then in
    full runs ("samples per pixel >= 3") and always passed alone. Their fake
    streams sampled the world in a Python thread every 1/rate s; under CPU
    load Windows woke that thread a scheduler quantum late, so a "400 Hz"
    stream gave one sample per ~35 ms (gaps up to 170 ms) and a pixel meant to
    hold 10 samples held 0-2 -- while the engine had flown every pixel. The
    fakes now sample on a fixed TIME GRID, computed at read time from where
    the stage WAS at each grid time (`scan-core/tests/timed_stream.py`: a
    stage kept as a history of moves), as a hardware-timed buffer does. The
    tests still prove what they guard: re-introducing the approach bug or
    gotcha #35 still empties 24 of 25 pixels. To reproduce load: run a few
    dozen busy-loop processes next to pytest. Do NOT reproduce it by running
    the same suite twice in parallel: the fixed test ports then collide, which
    is a different failure.

---

## 9. Verifying work (you can now run everything locally)

Verify **in place**, on the PC the suite runs on:

```powershell
cd "<root>\modules\motion\kim-control"
.\dev.ps1 sync --extra gui        # or: uv sync --extra gui   (copy dev.ps1 from clMag-control if missing)
.\dev.ps1 run pytest -q
.\dev.ps1 run python scripts\smoke_test.py
```

Expected test counts (all measured 2026-09-27): clMag 22 · smb 29 · stage 50 · piezo 37 · camera 129 · zpiezo 14 · kim 92 · hf2 60 · pm16 49 · vna 110 · mag2d 46 ·
mag2dcal 96 · ppms 43 · scan-core 357 · mission-control 16 · suite-common 53 = **1203**
(2026-09-27: fly scans -- scan-core +49 over 308, hf2 +8, kim +5, pm16 +5, camera +10)
(+ aaltoview 42, own repo). Plus the contract check:
`python tools/check_modules.py --live` (120 checks, 0 failed on 2026-09-27; since
2026-09-27 it also exercises the stream verbs of every module that declares one).

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
- **vna (PNA-X):** NI-VISA/Keysight IO Libraries, the VISA alias, then the 10 # VERIFY in
  `backends/pna.py` (sweep-done detection, byte order vs a front-panel marker,
  cal-set activation `,0` vs `,1`, SDATA of the corrected measurement, port power).
  `uv sync --extra gui --extra real` (gotcha #29).
- **vna (Copper Mountain C1209):** S2VNA running with the C1209 plugged in, its
  socket server ON (System > Misc Setup > Network Setup > Socket Server, port
  5025). No vendor VISA needed (pyvisa-py). On that PC, `vna-control\vna.ini`
  with `[hardware] driver = cmt` and `[field] source = ppms`, since the launcher
  only passes `--real`. Then the # VERIFY in `backends/cmt.py`: `TRIG:SING` is
  accepted (channel waiting for a trigger), `*OPC?` returns at the END of the
  sweep, `ABOR` acts while a `TRIG:SING` is pending, one point of
  `CALC1:TRAC1:DATA:SDAT?` against S2VNA's marker, power coupling for S12/S22,
  the model's real frequency range.
- **ppms (DynaCool):** MultiVu running on the same PC; `uv sync --extra gui
  --extra real` (MultiPyVu + pywin32). First `run_service.py --real --scaffold`
  (MultiPyVu's own simulation), then `--real` with MultiVu. Check: the adopted
  setpoints match MultiVu's front panel (nothing may move at start or stop);
  `field_max_mT` = YOUR magnet (9, 12 or 14 T) in `ppms-control\ppms.ini`; the
  field status MultiVu reports at field is `Holding (driven)` (else add it to
  `FIELD_HOLDING`); the accepted rate range; whether 0.1 mT / 3 s is the right
  "reached" rule at high field.
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
