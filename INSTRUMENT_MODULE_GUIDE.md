# Instrument-module blueprint (AaltoFlow)

This is the shared pattern behind every instrument module in this project
(`clMag-control` = Kepco magnet; `smb-control` = R&S SMB100A RF generator). Read
this first when building the next one so it looks and behaves like the others and
so one coordinator can drive them all. It is written to be self-contained: a new
session can build a fresh module from this document alone.

---

## 1. The mental model

One **service** process owns exactly one instrument (or its simulator) and is the
single source of truth for it. Everything else — GUIs, consoles, a coordinator —
is a **client** that talks to the service over ZeroMQ. The same client code drives
the instrument whether it's in the same process or across the lab Ethernet; only
the address changes.

Two families of instrument:

- **Closed-loop** (like the magnet): needs a control loop, threads, a state
  machine, PID/ramp/calibration. The "brain" class is `Controller`.
- **Set-and-forget** (like the RF generator): you command values and it holds
  them; no loop. The brain is a trivial `Generator`. Drop all the control
  machinery.

Decide which family the new instrument is BEFORE writing code. Most bench
instruments (signal generators, power supplies in fixed mode, attenuators,
sources) are set-and-forget → copy `smb-control`. Anything that has to servo a
measured quantity to a setpoint is closed-loop → copy `clMag-control`.

---

## 2. Package layout (src layout, always)

```
<inst>-control/
  pyproject.toml            # name "<inst>-control", pyzmq dep, GUI extra, pytest dev group
  README.md                 # run instructions + the OneDrive venv gotcha (§8)
  .gitignore                # .venv/ *.egg-info/ __pycache__/ .pytest_cache/
  src/<inst>/
    __init__.py             # docstring describing the module + __version__
    config.py               # dataclasses + INI save/load (§4)
    backends/
      __init__.py
      base.py               # typing.Protocol interface for the hardware (§3)
      sim.py                # simulator implementing the Protocol
      visa_scpi.py          # real instrument; LAZY-import the driver (pyvisa/nidaqmx)
    generator.py OR controller.py   # the "brain" (§5)
    sim_system.py           # build_sim_system(cfg) -> (brain, backend) wired, not started
    net/
      __init__.py
      protocol.py           # ports, topics, *_to_dict / *_from_dict (§6)
      service.py            # <Inst>Service: owns brain, PUB status + REP commands
      client.py             # <Inst>Client: brain-compatible facade over the socket
    apps/                   # optional GUI (only if wanted) (§7)
      __init__.py
      theme.py              # COPY of the shared theme verbatim
      gui.py                # MainWindow(ctrl,cfg,remote=bool) + run_app(...) + an indicator widget
      settings_dialog.py    # tabs = the config groups
  scripts/
    run_service.py          # start the service (sim default, --real for hardware)
    <inst>_console.py       # standalone raw-protocol console (only pyzmq, NO package import)
    run_gui.py              # launch GUI (--connect HOST for remote)
    smoke_test.py           # offline sanity check
  tests/
    conftest.py             # add src/ to sys.path
    test_config.py          # INI round-trip incl. any bool field
    test_<brain>.py         # clamp/lifecycle/state
    test_net.py             # service<->client round-trip on NON-default ports
    test_gui_smoke.py       # pytest.importorskip("PySide6"); offscreen build
```

`<inst>` is the short package name (`clMag`, `smb`, …). Keep it short and lowercase.

---

## 3. Backends — the hardware interface

`base.py` defines a `typing.Protocol` (`@runtime_checkable`) listing exactly the
methods the brain is allowed to call: `open()`, `close()`, and paired
`set_*`/`read_*` for each quantity, plus `idn()` where relevant. The brain depends
ONLY on this Protocol, so the simulator and the real driver are interchangeable.

- `sim.py`: a plain class that remembers state and implements every Protocol
  method. Add just enough "physics" to be lifelike (the magnet sim has
  saturation + hysteresis + noise; the RF sim just remembers values and reports
  RF off until opened).
- `visa_scpi.py` (real): the ONLY file that imports the hardware driver, and it
  imports it **lazily inside `open()`** — never at module top — so the whole
  package still imports and the simulator still runs on a PC with no VISA/NI
  drivers. In `pyproject.toml` the hardware dep stays **commented out** until the
  lab PC (`# "pyvisa"`).

Setters command directly; ramping/sequencing/clamping is the brain's job, not the
backend's.

---

## 4. Config — dataclasses + INI

Every tunable number is a field on a small `@dataclass`, grouped by concern
(e.g. `Signal`, `Limits`, `Hardware`; the magnet also has `Ramp`, `PID`,
`Acquisition`, `Stabilizer`, `Hall`). A top `Config` holds one of each, filled in
`__post_init__` (dataclasses can't have mutable defaults). Include a `Limits`
group defining the **safety envelope** — the brain clamps every setpoint to it.

Persistence is plain-text INI via `configparser`, one section per group,
`asdict`-based on save. On load, because `from __future__ import annotations`
makes every field type a string, route every value through a `_cast(raw, type)`
helper that handles `bool` / `int` / `float` / `str`. **The `bool` case matters** —
`"False"` is truthy as a bare string, so cast it as
`raw.strip().lower() in ("1","true","yes","on")`.

---

## 5. The brain (Generator / Controller)

Holds the DESIRED state, clamps each request to `cfg.limits`, pushes accepted
values to the backend, and reports a `status()` snapshot (a small `@dataclass`).
Required surface (both families expose this so the net + GUI layers are identical):

```
start()                      # open backend, push start-up state
shutdown()                   # safe state (output off), close backend; idempotent
status() -> Status           # snapshot dataclass; must NEVER throw
get_config() -> Config
apply_config()               # re-clamp/re-tune after cfg edited in place
self._on_event = lambda level, msg: None   # hook the service replaces
```

Plus one setter per controllable quantity (`set_rf`, `set_power`,
`set_frequency`, `set_phase` for the RF gen; `set_field`, `set_current`, `demag`,
`calibrate`, … for the magnet). Every setter clamps and emits an event:
`self._emit("warn", "power clamped to ...")` on clamp, `"info"` otherwise. Levels
are `"info" | "warn" | "error"`.

Closed-loop brains additionally run threads (acquisition + a command-queue control
loop) and a state machine; keep the FIRE-AND-FORGET contract (setters queue work
and return; status reports progress).

---

## 6. Communication — the wire protocol (this is fixed across all modules)

Two ZeroMQ sockets, bound to `tcp://0.0.0.0:<port>` so localhost and Ethernet are
the same code:

- **Commands** — `REQ`/`REP`, JSON in, JSON out. Every reply is
  `{"ok": true, ...}` or `{"ok": false, "error": "..."}`. **Fire-and-forget**:
  `ok:true` means *accepted/queued*, not necessarily physically settled; clients
  poll `status` to see the effect.
- **Telemetry** — `PUB`/`SUB`, multipart `[topic, json]`. Topics are
  `b"status"` and `b"event"`. Status is published at **5–10 Hz**; events
  (`{"level","msg"}`) are forwarded as they happen.

**Universal commands every module implements** (so a coordinator can treat any
instrument uniformly): `status`, `info`, `get_config`, `set_config`. Then add the
instrument-specific verbs (one per setter). Reply shapes: `status` →
`{"ok":true,"status":{...}}`, `info` → `{"ok":true,"info":{...}}` (limits + idn),
`get_config` → `{"ok":true,"config":{...}}`.

**`shutdown`** (universal since 2026-09-15) → `{"ok":true,"stopping":true}`, then
the service stops itself: set the stop flag that ends `serve_forever`, whose
`finally: stop()` shuts the brain down and closes the hardware. The launcher sends
it before it would kill a service, because on Windows a launcher cannot stop a
console program any other way than a hard kill -- and a killed service never
closes its instrument (a USB power meter was left answering "I/O error" until it
was unplugged). Close the REP socket with `linger` (not `close(0)`), or the reply
can be dropped. `tools/check_modules.py --live` checks the service really exits.

`protocol.py` holds the port constants, topic bytes, and the
`status_to_dict` / `config_to_dict` / `apply_config_dict` helpers — one file so
service and client can never drift. `apply_config_dict` writes into the existing
Config **in place** (so shared references stay valid).

### Port allocation (IMPORTANT — keep them distinct so all services coexist)

Rule: instrument *n* (0-based) uses `cmd = 5555 + 2n`, `pub = cmd + 1`. Since
2026-09-15 a module's ports are declared in its `module.toml` (section 11) and
checked for clashes by `tools/check_modules.py`; `tools/new_module.py` picks the
next free pair for you. Keep `DEFAULT_CMD_PORT` / `DEFAULT_PUB_PORT` in
`protocol.py` equal to them (they are what a hand-started service uses), and
remember the launcher can override both per PC.

### Service structure (`service.py`)

`<Inst>Service(brain, host, cmd_port, pub_port, status_hz)`. On `start()` it
routes `brain._on_event` into an event queue, calls `brain.start()`, and spawns
two daemon threads: **publisher** (owns the PUB socket — one socket per thread —
sends status every `1/status_hz` and drains the event queue) and **commander**
(owns the REP socket, `poller.poll(200)`, `recv_json` → `_dispatch` → `send_json`;
wrap in try/except so the loop never dies). `_dispatch` is a big `if cmd == ...`
that calls brain methods and returns `{"ok":true}`.

### Client facade (`client.py`)

`<Inst>Client(host, cmd_port, pub_port, timeout_ms)` presents the SAME method
names as the brain plus a `RemoteStatus` with the same attributes as the brain's
`Status`. A background SUB thread caches the latest status dict; commands go out
on a REQ socket under a lock (`RCVTIMEO`, `LINGER 0`; on `zmq.Again` timeout,
close and rebuild the REQ socket). `start()` fetches `info` + `get_config`. This
is what lets the GUI drive local or remote identically.

### Standalone console (`scripts/<inst>_console.py`)

A single file that speaks the raw protocol with **only `pyzmq` + `json`, no
package import**, so it can be copied to any machine. Interactive REPL + one-shot
mode. Great for poking the wire by hand and for verifying a new service fast.

---

## 6b. `describe` — the module's self-description (added 2026-09-10)

Every module answers one more universal verb: **`describe`**, returning a
manifest of what it can show and what it can be told to do. This is what lets a
client build a control panel — or a scan registry — for a module it knows
nothing about.

It began as camera-control's GenICam-style `features()`, which already built its
"Camera parameters (live)" panel generically. `describe` is that idea promoted to
a suite-wide contract.

**Two consumers, two projections of one source:**

| consumer | wants | how |
|---|---|---|
| the reconfigurable control screen | everything: numeric controls, indicators, buttons, their argument lists, `danger` flags, `group`/`order` layout hints | reads the manifest **directly** |
| scan-core | values it can sweep or record | `scan_core/manifest.py` turns controls into `Settable`s and indicators into `Gettable`s |

scan-core therefore needs **no per-instrument code** for a module that describes
itself. Actions are deliberately absent from the registry: a `Parameter` is a
value you sweep or record, and "Home" is neither.

### The rule that keeps it honest

**Nothing in a manifest may restate a value that lives somewhere else.** Every
bound is *looked up* from `cfg` / the calibration / the current mode at build
time, never typed as a literal. The old LabVIEW VI was painful because adding one
knob meant editing seven places; a hand-maintained manifest quietly becomes the
eighth, and a wrong limit there does not announce itself — it just draws a
slider with the wrong range.

Write `net/describe.py` as a table of descriptors whose bounds are expressions
over `cfg`, and adding a knob stays one edit.

### Limits are dynamic — this is the part that bites

Several modules move their own bounds at runtime:

- **clMag** — the field range *is* the loaded calibration's range. No calibration
  means no range, and the service refuses every setpoint.
- **piezo** — travel depends on the loop mode: ~200 µm open-loop, ~160 µm
  closed-loop, and switching to CL re-clamps a standing target.
- **kim** — an armed **leash** *replaces* the absolute min/max clamp.

So a client cannot fetch a manifest once at connect and cache it forever. The
manifest carries a **`revision`**, and every status frame carries
**`describe_rev`**, so a client compares one integer per poll and re-fetches only
when it changed.

`revision` is **derived** — a CRC over the manifest with `value` fields removed —
not a counter someone remembers to bump, because that counter would eventually be
wrong. Excluding `value` matters: it changes many times a second and travels in
the status stream anyway, so including it would tell clients to re-fetch
constantly and mean nothing.

### Descriptor fields

```python
{
  "id":        "field",            # stable, unique within the module
  "label":     "Magnetic field",
  "kind":      "control" | "indicator" | "action",
  "type":      "float"|"int"|"bool"|"enum"|"string"|"action",
  "unit":      "mT",               # "" if dimensionless
  "group":     "Field",            # layout hint for a panel
  "order":     10,                 # ordering hint within the group
  "min": ..., "max": ...,          # LIVE bounds; omit if genuinely unbounded
  "step": ..., "decimals": ...,    # display hints
  "options":   [...],              # enum only
  "writable":  True,
  "plottable": True,               # a scalar worth graphing over time
  "read_path": ["aux", "ai", "Dev1/ai1"],   # path into the status dict, or None
  "set":       {"verb": "set_field", "arg": "field_mT",
                "extra": {...}, "scale": 1.0},      # controls only
  "settle":    {"policy": "adopt_then_flag", ...},  # controls only
  "args":      [{"name","label","type","unit","default","min","max"}],  # actions
  "danger":    True,               # confirm before firing
  "help":      "one or two sentences",
}
```

`read_path` is a **list**, not a dotted string: AUX channel names contain `/` and
ids may contain `.`, so a dotted path could not be split back apart.

Expand multi-axis modules **flat** — `x`, `y`, `z` as three independent
descriptors each with its own limits, not one descriptor taking an axis
argument. A panel can then place a single axis, and scan-core can sweep one.

### Array detectors (a VNA, a spectrometer, a scope trace)

Not every detector returns a number. A VNA returns a whole trace per scan
point, because **the frequency sweep happens in hardware, on the instrument** —
far faster than the odometer could step it. That frequency axis is a genuine
dimension of the measurement; it is simply *hardware-swept* rather than
*software-swept*.

So a scan has two kinds of axis, and both are real:

| | swept by | declared in |
|---|---|---|
| software-swept | the engine's odometer | `recipe.axes` |
| hardware-swept | the instrument itself | the detector's `dims` |

The dataset is `(software dims…) + (hardware dims…)`. **The recipe format does
not change**: `detectors: [s21]` is still just an id.

An array indicator adds three fields:

```python
"dtype": "complex",              # float | int | complex
"shape": ["vna_freq"],           # inner dims, outermost first
"dims": [{
    "name":  "vna_freq",
    "label": "Frequency",
    "unit":  "Hz",
    "length": 1601,              # for a UI's benefit; the engine measures it
    "coord_verb": "get_frequencies",   # a command returning the coordinate array
    "coord_key":  "values",            # where in the reply (default "values")
}],
```

**A trace is fetched with a command, not read from status** (added 2026-09-16,
`vna-control` is the worked example). The status stream goes to every subscriber
ten times a second; 1601 complex points in it would be ~60 kB/s of mostly
repeated data. So the descriptor says which verb serves the value:

```python
"read": {"verb": "get_trace", "key": "s21", "args": {"which": "sample"}},
```

scan-core calls it after the acquisition wait and takes `reply["s21"]`. **JSON
has no complex numbers**: send complex as `{"re": [...], "im": [...]}` (a plain
list for a real array; `null` for NaN). `read_path` stays `null` for such a
detector, and a control panel simply shows no value for it.

Prefer `coord_verb` over inlining `values`: a VNA's frequency grid follows its
start/stop/points, so it must be *derived*, not restated — and 1601 floats
inside every `describe` reply is a lot of wire traffic for something that
changes rarely. The engine reads the coordinate **once per scan**, not per
point.

Detectors that share an axis **name** share one coordinate, which is what you
want for `s11`/`s21`/`s12`/`s22` off a single sweep. If two detectors declare
the same axis name with different lengths, the engine refuses the scan rather
than guessing.

**Complex is stored as a real/imaginary pair.** `h5netcdf` will happily write a
complex array — it round-trips through Python exactly — but it then warns the
file is not conforming netCDF-4 and "might not be readable by other netcdf
tools". Lab data does not stay in Python; it gets opened in MATLAB, in Igor and
by collaborators. So the engine carries complex in memory and writes
`<id>_real` and `<id>_imag`, and `scan_core.data.as_complex(ds, "s21")` puts it
back together.

**Ragged data is refused, not padded.** If the instrument's sweep changes
mid-scan (a span or point-count change will do it) the array stops being
rectangular. The engine stops and names the detector, both shapes and the grid
index, because padding with NaN would hand back a file that looks fine and is
wrong.

### Acquisition — does the caller wait for the detector?

`settle` answers "has the knob arrived?". **`acquire` answers the same question
for the detector**, and it is just as easy to get wrong.

Reading a fast detector is one call: an NI analog input hands back a sample in
microseconds, and nothing needs waiting for. A VNA is not like that. A sweep
takes real time and the sequence is **trigger → wait → read**. Read it cold and
it hands back whatever is still in its buffer — the last sweep it actually took
— and *nothing raises*. The map comes out not tracking the swept axis at all,
and the file is perfectly well formed.

A slow detector therefore declares:

```python
"acquire": {
    "group":        "sweep",       # detectors sharing this = ONE acquisition
    "trigger_verb": "sweep",       # fire-and-forget: start the measurement
    "ready": {"policy": "flag_only", "key": "sweeping", "invert": true},
    "timeout_s":    60
}
```

`ready` takes the same policy vocabulary as `settle`, because it is the same
question. No `acquire` block means a plain read is already fresh.

**`flag_only` on a busy flag has the stale-status hole here too.** Right after
the trigger, the cached status can still be the frame from before it, saying
"not busy", so the wait returns at once and the read gets the previous
acquisition. If the module can, number its acquisitions, return the number from
the trigger verb, and name that reply field as `target_key` — it becomes the
target of the ready policy (added 2026-09-14 for the hf2 lock-in):

```python
"acquire": {
    "group": "sample", "trigger_verb": "acquire",
    "target_key": "acq_id",        # take the wait target from the trigger REPLY
    "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
              "flag_key": "acquiring", "invert": True},
}
```

**`group` is what stops four S-parameters costing four sweeps.** `s11`, `s21`,
`s12` and `s22` come off one acquisition, so they share a group: the engine
triggers it once, waits once, and reads all four from it.

The engine triggers **every** group before waiting on **any** of them, so two
instruments acquire concurrently rather than one after the other.

**Prefer trigger-and-poll over a blocking read verb.** A verb that blocks for
the whole sweep will hit the client's 3 s command timeout, which rebuilds the
REQ socket and fails the scan. If a driver leaves you no choice, pass
`_timeout_ms` to that one call — but a service that returns immediately and
reports readiness in `status` is the suite's contract, and it keeps the command
thread free.

### Settle policies

A control declares how a caller knows it has arrived. This is the module's
knowledge, not the coordinator's, and it belongs here:

| policy | for | fields |
|---|---|---|
| `adopt_then_flag` | closed loop (clMag field) | `setpoint_key`, `flag_key`, `invert` |
| `echoes` | set-and-forget with a readback (smb power) | `key`, `tol` |
| `state_in` | state machines with no per-knob flag (clMag current, demag) | `key`, `states` |
| `flag_only` | a busy/moving flag with no echoed setpoint | `key`, `invert` |
| `immediate` | genuinely instant, and honest about never being verified | — |

**Per-axis / per-channel values published as lists** need an `index` in the
settle block: `{"policy": "echoes", "key": "tc_set_s", "index": 1}` reads
`status["tc_set_s"][1]`. It applies to every key the policy names. Without it a
list reaches the policy whole, and `bool([False, False, False])` is `True` — so
scan-core now refuses a list-valued key with no index rather than hanging.

`adopt_then_flag` exists because commands are fire-and-forget: for a moment
after a set, the service still reports the **previous** point, done-flag and all.
Watching the flag alone returns instantly at the old value and measures a whole
grid one step behind — data that looks perfectly clean and is wrong.

### Actions a scan can run (routines)

An action becomes available to scan routines (before, during and after a scan)
when its descriptor has a `wait` block: how the caller knows it has finished,
with the same `ready` vocabulary as the settle policies, an optional
`target_key` (the reply field holding the run's number, gotcha #17) and an
optional `check` (`{"key": "af_error", "equals": "OK"}`: finished is not
succeeded). An action that is done when its command replies declares
`"wait": {"ready": {"policy": "immediate"}}`.

A routine runs the action with the **defaults** of its `args` (there is no
dialog). Text defaults may contain placeholders that scan-core fills in, so a
module can save something of its own next to the measurement:

| placeholder | becomes |
|---|---|
| `{data_dir}` | the folder of the `.nc` file being written ("" if the run is not saved) |
| `{data_stem}` | its file name without `.nc`, e.g. `134501_fmr_map` |
| `{moment}` | `before`, `after`, or `p00042` (the point number) during a scan |

Example, the camera's "Save camera picture": `folder` = `"{data_dir}"`, `name` =
`"{data_stem}_{moment}_camera"`. An empty folder means "use your own default".

### Checklist for adding `describe` to a module

1. `src/<pkg>/net/describe.py` with `build_manifest(brain)` and
   `manifest_revision(manifest)`. Copy clMag's and edit the table.
2. Service: a `describe` verb, plus `st["describe_rev"] = self.describe_rev()`
   in the publisher (cache it; rebuilding at the status rate is waste).
3. Client: a `describe()` method, and `describe_rev` on `RemoteStatus`.
4. Tests: bounds follow `cfg` (change a config value, assert the manifest
   follows); `revision` changes on a bound change and **not** on a value change;
   `read_path` resolves; every control has a `set` block; every indicator has a
   `read_path`.

---

## 7. GUI — theme, colors, indicator

PySide6, Fusion style. `run_app(ctrl, cfg, remote=False)` does:
`app.setStyle("Fusion")`, `apply_dark_palette(app)`, `app.setStyleSheet(STYLESHEET)`,
then `MainWindow(ctrl, cfg, remote=remote)`. `MainWindow` polls `ctrl.status()` on
a 30–60 ms `QTimer`, sends commands on button clicks, and forwards `ctrl._on_event`
into the log via a `Bridge(QObject)` signal (events cross a thread boundary, so
they MUST go through a Qt signal, not a direct call).

Each package carries its **own copy of `theme.py`** (verbatim) so all windows look
identical — do not try to import another package's theme.

### The exact palette (dark, amber accent) — copy these hex values

```
bg         #0e1013   window background, near-black
panel      #171a1f   card background
panel_hi   #1e222a   inputs, hover
border     #2a2f37
text       #e8eaed
muted      #8b929c
accent     #ff9e2c   amber  (primary action, energized/on)
accent_hi  #ffb454   amber bright (hover, captions)
accent_dim #7a4d16   amber dim (timestamps, pressed)
ok         #3ddc84   green  (stable / RF on / good)
danger     #ff5c5c   red    (over-limit / fault / stop)
grid       #20242b
```

Conventions baked into the stylesheet: `QFrame#card` = rounded panel; primary
buttons `setObjectName("primary")` (amber, dark text); destructive buttons
`setObjectName("danger")` (red); big readouts `QLabel#bigValue` (34px bold);
`QLabel#cardTitle` = uppercase muted 11px letter-spaced; log is
`QPlainTextEdit#log` monospace on `#0a0c0f`. When you flip a button's objectName at
runtime, re-polish it: `btn.style().unpolish(btn); btn.style().polish(btn)`.

### The signature indicator widget (do this for every instrument)

Each module has ONE custom `QWidget` with a `paintEvent` that visualizes the
instrument's live state — it's the personality of the window:

- magnet: `MagnetIndicator` — a GMW-3470 dipole glyph that glows amber in the gap,
  intensity ∝ |current|, N/S labels flip with polarity.
- RF gen: `AntennaIndicator` — a transmitter tower that **radiates animated amber
  arc-waves when RF is on**, brightness/# of arcs ∝ power (mapped to its position
  in the power-limit band). Idle grey when off.

Pattern for an ANIMATED indicator: give the widget its **own `QTimer` (~33 ms)**
that advances a `_phase` and calls `update()`, started/stopped in `set_state()`
based on whether it should animate — independent of the (slower) status poll, so
motion stays smooth. Draw with `QPainter` + `Antialiasing`; concentric arcs via
`drawArc(rect, startAngle*16, span*16)`; fade alpha with radius. Feed it live
state each `_refresh()` from `status()`.

Pick a glyph that reads instantly for the instrument (a dish/tower for RF, poles
for a magnet, a gauge/bar for a supply, a shutter for a laser, etc.).

---

## 8. Stack, tooling, gotchas

- **uv** + **src layout**. `pyproject.toml`: `dependencies = ["pyzmq>=25"]`;
  `[project.optional-dependencies] gui = ["PySide6>=6.7"]`; `[dependency-groups]
  dev = ["pytest>=8"]`; `[tool.setuptools.packages.find] where = ["src"]`.
- Hardware deps (`pyvisa`, `nidaqmx`) stay **commented out** until the lab PC.
- **OneDrive venv gotcha** (this whole project lives in OneDrive): OneDrive locks
  files inside `.venv` as it syncs and `uv` dies with *"Access is denied (os error
  5)"*. Fix once per machine — put the venv OFF OneDrive:
  ```powershell
  [Environment]::SetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', "$env:LOCALAPPDATA\uv-venvs\<inst>-control", 'User')
  $env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\uv-venvs\<inst>-control"
  uv sync
  ```
  Document this in every README's Troubleshooting.
- The user (Lukáš) is a physicist and a near-beginner in Python doing this as a
  learning exercise: **teach the tooling and the why, comment code richly, explain
  process — pair-programmer, not code-dumper.** Prefers answers in English.

---

## 9. Testing checklist (all offline, no hardware)

- `test_config.py`: INI save/load round-trip, explicitly assert a `bool` field
  survives (both True and False).
- `test_<brain>.py`: set/read-back, clamping at both ends of each limit, lifecycle
  (start → shutdown leaves output safe), events fire on clamp.
- `test_net.py`: spin up service + client on **non-default ports** (e.g.
  15690/15691) so it never collides with a running service; assert commands take
  effect and clamping works over the wire; poll a few cycles for the PUB frame.
- `test_gui_smoke.py`: `pytest.importorskip("PySide6")`, set
  `QT_QPA_PLATFORM=offscreen`, build the window, `_refresh()`, toggle, advance the
  indicator animation — catches import/layout/signal wiring without a display.
- Also ship `scripts/smoke_test.py` for an instant offline check.

Verify in the cloud sandbox before delivering: `pip install pyzmq pytest` (and
`PySide6` for the GUI test), run `pytest -q`, and render the GUI offscreen to a PNG
(`widget.grab().save(...)`) to confirm it actually looks right.

---

## 10. Recipe: add instrument "X" in a new session

1. Read this guide + the memory files. Decide closed-loop vs set-and-forget.
2. Confirm with the user: connection/interface (GPIB/USB/LAN + VISA address),
   the quantities to control, and their safe limits.
3. **Generate it:** `python tools/new_module.py x --like smb --name "..." --description "..."`
   (`--like clMag` for closed-loop, `--like hf2` for a detector with an
   acquisition). This copies the template, renames package / classes / imports,
   takes the next free port pair and writes `module.toml`, a placeholder
   `icon.svg`, a stub README and private notes (`CLAUDE.local.md`, not in git). `uv sync --extra gui; uv run pytest`
   passes at once. The launcher and scan-core already list it.
4. Rewrite `config.py` groups for X's quantities + a `Limits` envelope.
5. Rewrite `backends/base.py` Protocol; write `sim.py`; write the real driver with
   a lazy hardware import and the right SCPI/API.
6. Trim/extend the brain: set-and-forget → strip loop/PID/state; closed-loop →
   keep and retune.
7. `net/protocol.py` `*_to_dict` helpers; `service.py` `_dispatch` verbs;
   `client.py` facade + `RemoteStatus`; `net/describe.py` for X's variables.
8. Scripts: `run_service.py`, `<x>_console.py`, `run_gui.py`, `smoke_test.py` --
   keeping the command-line contract of section 11.
9. GUI: `theme.py` came with the copy; build `MainWindow` + a new signature
   indicator widget for X; `settings_dialog.py` tabs = config groups. Draw a real
   `icon.svg`.
10. Tests (§9), then `python tools/check_modules.py x --live`, then an offscreen
    render (`python tools/render_all.py x`).
11. Update the module's README (and `docs/DEVELOPER_NOTES.md` if a shared rule changed); commit.

## 11. The module contract: how the suite finds a module (added 2026-09-15)

Nothing in the suite lists modules by hand. The launcher (mission-control),
scan-core, the render and deploy tools all ask **module discovery**
(`suite-common/src/suite_common/modules.py`), which reads every
`<root>/<folder>/module.toml`. A folder with that file IS a module.

```toml
[module]
key = "hf2"            # unique; letters/digits/_; must equal "module" in describe
name = "Lock-in"       # launcher card title
description = "Zurich HF2LI 50 MHz - 2 demodulator channels + aux inputs"
category = "detector"  # what it is FOR: motion | field | source | detector |
                       # imaging | environment | other  (a typo is an error)
tags = ["lock-in", "Zurich Instruments", "HF2LI"]   # search words
icon = "icon.svg"      # 40x40 viewBox; accent colours are swapped for the theme
order = 80             # position in lists

[ports]                # defaults; the launcher can override them per PC
cmd = 5569
pub = 5570

[run]
service = "scripts/run_service.py"
gui = "scripts/run_gui.py"    # "" for a headless module
start_after = []              # keys to start first when started together
```

**Identity only.** The controls and measured variables are NOT in this file: the
running service reports them through `describe` (section 6b), and a copy here
would go stale. The launcher shows them from `describe`, cached for when the
service is down.

**Command-line contract** (the launcher relies on it; `check_modules.py` tests it):

| script | must accept |
|---|---|
| `run_service.py` | `--cmd-port N --pub-port N --real` (a module without real hardware yet accepts `--real` and refuses clearly) |
| `run_gui.py` | `--connect HOST --cmd-port N --pub-port N` (and `--theme`) |

**Environment the launcher sets** for every process it starts:
`AALTOFLOW_ENDPOINTS` = JSON `{key: [host, cmd, pub]}` of every module (remote ones
under their full id), so a module that talks to another -- the camera to kim --
follows ports changed in the launcher; and `PYTHONUNBUFFERED=1`, so prints reach
the launcher's log live.

**This PC's choices** live in `<root>/suite_local.json` (gitignored), written by
the launcher, read by everyone: `{"modules": {key: {"real", "cmd", "pub"}},
"remote": [{host, cmd, pub, key, name, description}]}`. A remote service's `key`
is what its `describe` reported; it borrows the icon, description and GUI of the
local module with that key. In registry ids it is called by its slug
(`hf2_lab2`), so it cannot collide with the local one.

**Tools:** `tools/new_module.py` (generate; `--category`, `--tags`),
`tools/check_modules.py [--live]` (verify), `tools/make_catalog.py` (regenerate
`catalog.json` after editing any module.toml -- a test fails while it is stale),
`tools/pack_module.py` (a module pack, see below), `tools/render_all.py` and
`tools/deploy_lab.ps1` (both discover).

**Getting a module onto another PC (2026-09-24).** `python tools/pack_module.py
<key> [<key>...] [--wheels]` zips the module folder(s) AS COMMITTED plus a
`pack.json` label into `dist/modules/`. With `--wheels` the pack also carries
every Python package pinned in the module's `uv.lock` (Windows x64 wheels for the
module's Python), so it installs with NO internet -- a GUI module is ~250 MB,
mostly PySide6. On the target: Mission Control > **Add module…** > From module
pack (or From folder), filter by "What do you need?", Install. What it does is
`suite_common.catalog`: new / update / conflict per module; an update keeps the
rig's `*.ini`, `*calibration*.json` and `Calibrations\`; a new module whose
default ports are taken gets the next free pair on that PC; the `.venv` is built
with `uv sync` (online) or from `<root>\wheelhouse\` (offline, `--no-index`).
A module must therefore stand alone: no path dependencies on sibling folders.

The end goal is a **coordinator** that holds one client per instrument and
sequences them ("set field → wait field_stable → set RF → trigger measurement"),
which works precisely because every module speaks the same protocol on its own
port pair.
