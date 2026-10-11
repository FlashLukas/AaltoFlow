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
modules/<category>/<inst>-control/     # e.g. modules/source/smb-control (section 11)
  pyproject.toml            # name "<inst>-control", pyzmq dep, GUI extra, pytest dev group
  README.md                 # run instructions + the OneDrive venv gotcha (§8)
  .gitignore                # .venv/ *.egg-info/ __pycache__/ .pytest_cache/
  src/<inst>/
    __init__.py             # docstring describing the module + __version__
    config.py               # dataclasses + INI save/load (§4)
    hwlock.py               # EXACT copy of suite-common/src/suite_common/hwlock.py (§3)
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

### One physical address, one service (`hwlock.py`, 2026-09-27)

An instrument is identified by its PHYSICAL ADDRESS (`GPIB0::6`, `COM5`, a USB
serial number, an IP address), not by the module that drives it: clMag and
kepco can both be pointed at the same Kepco BOP, mag2d and mag2dcal at the same
DAQ card. So every real backend claims its address before it touches the
instrument, and a second claim of the same address -- from ANY module on this
PC -- is refused with `HardwareBusy`, whose message names the holder
(`GPIB0::6 is already in use by clMag (pid 4242) -- ...`). The recipe:

```python
from .. import hwlock                      # the module's own copy, see below
MODULE = "smb"                             # the module key: the launcher matches on it

class VisaScpiBackend:
    def __init__(self, resource: str):
        self.resource = resource
        self._lock: hwlock.HardwareLock | None = None

    def open(self) -> None:
        # 1. claim FIRST: a refused claim must leave the instrument untouched
        self._lock = hwlock.claim(self.resource, MODULE)   # raises HardwareBusy
        try:
            import pyvisa                  # 2. the lazy vendor import, as above
            ...                            # 3. open the session, adopt its state
        except Exception:
            self._lock.release()           # never opened it: do not keep it claimed
            self._lock = None
            raise

    def close(self) -> None:
        try:
            ...                            # close the session
        finally:
            if self._lock is not None:     # release even if closing failed:
                self._lock.release()       # a stuck claim would block a restart
                self._lock = None
```

- **Claim in `open()`, release in `close()`.** Keep the returned lock for as
  long as the instrument is open.
- **The simulator never claims.** Ten simulated services can run side by side;
  only hardware is exclusive.
- **Claim the address the user configured**, spelled any way VISA accepts it:
  `normalize()` makes `GPIB::6`, `gpib0::6::INSTR` and `GPIB0::6` one
  instrument, `ASRL5::INSTR` and `COM5` another. A network instrument is keyed
  by its HOST (two ports on one box are one box). A backend that discovers its
  device (a USB meter with no address set) claims each candidate before
  opening it and moves on to the next one when it is busy.
- **Let the refusal reach the user.** The service prints the one-line message
  and exits with **code 4** (`EXIT_HARDWARE_BUSY = 4` in `scripts/run_service.py`
  -- the same number in every module, so "busy" can be told apart from any other
  start failure by the number alone; 2 stays "could not start", and mag2d's 3
  "no cooling water"). Mission Control recognises "is already in use by" and
  shows the card as *address busy: GPIB0::6 held by clMag* instead of a generic
  crash.
- **Keep the copy identical.** Every module carries its own `src/<pkg>/hwlock.py`
  (a module installs without suite-common, the same convention as `theme.py`).
  Edit only the master `suite-common/src/suite_common/hwlock.py`, then copy it
  into every module; `tools/check_modules.py` FAILS a missing or differing copy
  and WARNS about a real backend file that never calls `claim(`.
  `tools/new_module.py` refreshes the copy from the master.
- The lock is **per PC** and the operating system releases it when the process
  ends, even on a crash -- details in docs/DEVELOPER_NOTES.md, gotcha #37.

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
shutdown(keep_outputs=False) # safe state (output off), close backend; idempotent
                             # keep_outputs=True: a RESTART -- close, change nothing
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

`shutdown{keep_outputs: true}` (since 2026-10-06) is a RESTART for a code update:
close the instrument, the hwlock claim and the sockets exactly as usual, but skip
every step that changes what the instrument outputs (RF off, output off, ramp to
zero, emission off, a park position). The next start adopts the state. Stopping a
running MOVE is not an output change: do it anyway. Parse the flag tolerantly
(`"false"` is False, gotcha #3) and reply `kept_outputs` so a caller can tell an
old service (which ignores it and switches off) from a new one.
A module whose output must never outlive its service may REFUSE the flag in the
verb handler and reply `kept_outputs: false` with a `note` (the magnets, the
chopper and cs260 do, Lukas 2026-10-11: nobody should come back to a coil left
driven by a code update).

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
**first binds both sockets** (PUB and REP, in the calling thread) and raises
`PortInUse` (a `RuntimeError`) if a port is taken -- BEFORE the instrument is
opened, so a clash never leaves a deaf process holding the hardware;
`run_service.py` turns it into one line on stderr and exit code 2. Then it
routes `brain._on_event` into an event queue, calls `brain.start()` (closing
both sockets again if that raises), and spawns two daemon threads, each handed
its already-bound socket: **publisher** (owns the PUB socket — one socket per
thread — sends status every `1/status_hz` and drains the event queue) and
**commander** (owns the REP socket, `poller.poll(200)`, `recv` → parse JSON →
`_dispatch` → serialise → `send`). **Every request received gets a reply**, also
one that is not JSON or not an object (`{"ok": false, "error": ...}`), and the
reply is serialised before sending so an unencodable one is answered with an
error too: a REP socket that received and did not answer refuses everything
after. `_dispatch` is a big `if cmd == ...` that calls brain methods and returns
`{"ok":true}`. Both rules are docs/DEVELOPER_NOTES.md gotcha #39, and
`tools/check_modules.py --live` tests them on every module.

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

### Control — one controller, many viewers (2026-09-29)

Several clients can connect to one service at once (GUIs on several PCs,
scan-core, another module, scripts). The first GUI gets **control**; every
later GUI is a **viewer** that sees everything live and changes nothing; control
changes hands only by a deliberate "Take control". The rules and the reasons
are in `docs/DEVELOPER_NOTES.md` section 4 ("Control"). Every module has it
(kim and camera first, all 38 since 2026-09-30); a module generated with
`tools/new_module.py` inherits it from its template. What a module needs:

1. **Copy two files, never edit them:** `suite-common/src/suite_common/control.py`
   → `src/<pkg>/control.py`, and `.../control_bar.py` →
   `src/<pkg>/apps/control_bar.py`. `tools/check_modules.py` compares them with
   the masters, as it does hwlock.py.
2. **Service** — in `__init__`, name the module's SAFETY verbs (always allowed,
   also for a viewer: stop, abort, kill) and any read verb whose name does not
   start with `get_` / `read_` / `list_`:
   ```python
   self.control = ControlLease(safety={"stop"}, read={"stream_read"},
       on_event=lambda level, msg: self._events.put({"level": level, "msg": msg}))
   ```
   first thing in `_dispatch`:
   ```python
   gate = self.control.handle(req)
   if gate is not None:
       return gate
   ```
   and in `status_payload()`: `st["control"] = self.control.status()`.
   Nothing else: the gate answers `take_control` / `release_control` /
   `heartbeat` / `clients` / `claim_scan` / `release_scan` itself.
   **Every safety verb is also an ACTION in `describe`** (Lukas, 2026-09-30),
   so the suite's Control tab offers it to a viewer (it keeps exactly the
   actions listed in `control.always` usable). If the module has no verb that
   ONLY makes things safe -- its "off" is `set_current(0)` / `set_rf(false)`,
   which can also switch things on or drive anywhere -- add one (clMag
   `ramp_to_zero`, shsg `rf_off`) and use it for the GUI's off button and the
   console. Acquisition triggers (`acquire`, `take_reference`) are NOT safety:
   a trigger replaces the sample other clients wait on. A refused command
   RAISES in the client (`ControlRefused`) -- scripts see "read-only: ...".
3. **Client** — `class <Inst>Client(ControlClient)`; `__init__` takes
   `kind="script", name="<inst> client"` and calls
   `self._control_setup(kind, name)`; `_rpc` calls `self._with_identity(req)`
   before sending and `self._raise_refusal(reply)` on a failed reply; the SUB
   loop feeds `self._control_from_status(payload)` on every status frame;
   `start()` calls `self.start_heartbeat()`, `close()` `self.stop_heartbeat()`.
   `run_gui.py` builds its client with `kind="gui", name="<inst> GUI"`. A client
   inside ANOTHER module that must not be locked out (the camera driving kim)
   sends `"client": make_identity("machine", "<who>")`.
4. **GUI** — only when `remote` and the client has `take_control`: a
   `ControlBar(self.ctrl, self, log=...)` at the top of the window,
   `bar.refresh()` from the status timer, `bar.claim_if_free()` once after the
   UI exists, and `mark_always(button)` on every safety button (the ones whose
   verbs are in `safety`) and on buttons that only read (Refresh, Show curve).
   A local GUI (its own brain) has no bar.
5. **Console** — include `"client": IDENTITY` in every request (kind
   `script`), add `take` / `take!` / `release` / `clients`, and run a heartbeat
   thread on its own socket in the REPL.
6. **Tests** — see `kim-control/tests/test_control.py`: first GUI controls,
   second is refused naming the holder, safety verbs pass, a `machine` client
   passes, a script must take control, a forced take-over is announced, a
   silent holder loses control, and the GUI viewer blocks a click while STOP
   still works.


### Encryption -- CurveZMQ (every module, 2026-10-04)

Every module speaks CurveZMQ when the lab's policy secures it, so that only
PCs in the lab keyring reach it and every identity it receives is checked
against the sender's key (README, "Encryption and keys"; developer notes
section 4, "Encryption", and gotcha #47). With the policy "off" -- the
default, and every PC that was never set up -- nothing changes. kim is the
reference implementation; copy its code, do not invent a variant.

1. **Copy, never edit:** `suite-common/src/suite_common/secure.py` ->
   `src/<pkg>/secure.py` (byte-identical; `check_modules` compares).
2. **Service** (`net/service.py`, see kim's `start` / `stop` / `_commander`):
   - before binding: `self._guard = secure.secure_server(ctx, [rep, pub],
     "<key>", on_event=...)` -- inside a try that closes both sockets on
     `secure.SecurityError` and re-raises;
   - every later failure path in `start()` (port taken, brain refused) and
     `stop()` call `secure.release_server(self._guard)`;
   - receive with `frame = sock.recv(copy=False)`; before `_dispatch`:
     `refused = self._guard.check(req, secure.user_id(frame))` when a guard
     is set, and reply with `refused` if it is not None.
3. **Client** (`net/client.py`), every REQ socket:
   - `RCVTIMEO` AND `SNDTIMEO` (a plain socket whose handshake an encrypted
     service refused blocks forever in `send` -- gotcha #47);
   - `secure.secure_client(sock, host, "<key>")` before `connect`;
   - on `zmq.Again`: close, `flipped = secure.no_answer(host, "<key>")`,
     rebuild, and resend ONCE when it flipped (kim's `_rpc` loop). Safe: a
     request in the wrong mode never reaches the service.
   - SUB loop: `secure.secure_client(sub, ...)` before `connect`, and rebuild
     the SUB socket when `secure.flip_generation()` changes (kim's `_sub_loop`).
4. **Other clients in the module** (a backend that talks to ANOTHER module,
   e.g. camera -> kim, vna -> a magnet): the same three points, with the
   OTHER module's key (`secure.secure_client(sock, host, "kim")`).
5. **Console** (`scripts/<x>_console.py`): load `secure.py` by file path
   (kim's `_load_secure`), `SNDTIMEO`, and the same retry on a timeout.
6. **Tests:** `tests/conftest.py` points `AALTOFLOW_SECURITY_DIR` at an empty
   temporary folder at import (the PC's own keys must never change a test).
   `python tools/check_modules.py <key> --live` then proves it end to end:
   it starts the service with a throw-away keyring in `enforce` and checks
   that a plain client gets NO answer while a keyed one gets describe, the
   status stream and shutdown. Module-specific tests are only needed for
   what the module adds (kim's `tests/test_secure.py` covers the shared code).

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
  "step": ..., "decimals": ...,    # display hints (a GUI increment, NOT a quantisation)
  "resolution": 0.5,               # optional, float controls: the instrument only
                                   # realises multiples of this (an attenuator's
                                   # step). scan-core rounds a setpoint to it and
                                   # settles on the rounded value; the axis preview
                                   # shows it. Declare it when the hardware IGNORES
                                   # or rejects an off-grid value (DS SG12000L, 2026-10-06)
  "options":   [...],              # enum only
  "bits":      12,                 # optional, int detectors: unsigned 0..2^bits-1
  "store":     "float32",          # optional, float/complex detectors: halve the file
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

### The type of a detector is how scan-core STORES it (2026-10-04)

scan-core picks the storage of every recorded value from its descriptor
(full table: `docs/DEVELOPER_NOTES.md` section 4b): `bool` -> uint8, `int` ->
the narrowest integer its `min`/`max` (or `bits`) allow, `enum` -> an integer
code with the option names in the file's attributes, `string` -> text,
`float` -> float64. So declare the type a value really has -- a state name is
an `enum` or a `string`, not a float -- and both are recordable since then.

- **`min` / `max` on an indicator are a PROMISE.** They choose its storage
  type, and a measured value outside them STOPS a scan (never clipped). Give
  them only where the instrument cannot report anything else (a 0..100 %
  reading, a counter that cannot go negative), and never narrower than the
  instrument really reports. On a CONTROL they stay setting limits: a control
  recorded as a detector is never narrowed by them.
- **`enum` options must cover every value status can report.** A readback that
  is not one of the options (a "--", an empty string, a front-panel setting
  outside the offered list) is stored as "not measured", with one warning in
  the scan log -- the value is lost, so list every value the instrument can
  report. `None` is stored as "not measured" too.
- **`bits`** (new, optional): an int detector that is an N-bit count (a 12-bit
  camera, a 16-bit digitiser) -> stored unsigned, 0..2^N-1 allowed. With
  `bits`, a `max` may TIGHTEN the top (2026-10-10): a 4 x 4 bin of 12-bit
  counts declares `bits: 16, max: 65520` and is stored as uint16 with 65535
  spare for "not measured" (`bits: 16` alone needs uint32). `min` is ignored
  with `bits` (bits means unsigned).
- **`store: "float32"`** (new, optional): a float/complex detector whose
  precision is ~7 significant digits or worse (most ADCs) may be stored in
  half the space. Default float64.
- Both fields are backwards compatible: a client that does not know them
  ignores them.

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

Optional dim fields (2026-10-10, the camera image): `"attrs": {...}` is
copied onto the coordinate in the file (`{"um_per_px": 0.413}`), and
`"aux": [{"name": "image_x_um", "key": "x_um", "unit": "um"}]` names MORE
coordinates of the same axis that `coord_verb`'s reply carries (`reply["x_um"]`)
-- written as non-index coordinates, so a reader can plot against pixels or
micrometres.

### Images -- a frame per point (2026-10-10)

An array detector with **two** dims is an IMAGE to scan-core (`camera.image`
is the worked example, `modules/imaging/camera-control/src/camera/recording.py`).
Nothing new to declare beyond the array fields, but four things follow:

- declare the pixels honestly: `type: "int"`, `bits` (unsigned) and `max` =
  the largest value a stored pixel can take at the CURRENT depth and binning;
  the revision changes when they do. scan-core then stores uint16 for a
  12-bit camera, one frame per compression chunk, with a per-point mask
  `<id>_measured`;
- give each dim its `length` (the frame shape now): the Scan tab estimates
  the file size from it before the run, without a network call;
- an `acquire` block with numbered acquisitions, the frame taken AFTER the
  trigger (gotchas #17, #28) -- a camera that keeps its last frame would
  otherwise hand every point the previous point's picture;
- serve it as a **binary reply part** (next section), `"read": {..., "binary":
  true}`.

Above ~1 GB (uncompressed) scan-core writes the frames into the data file as
they arrive instead of holding them in memory (scan-core README, "Images").

### Binary replies (2026-10-10; additive to the wire contract)

JSON is the right format for everything in the suite but a large array: a
1936 x 1096 frame of 12-bit counts is 4 MB as raw bytes and ~6 MB as base64,
plus the time to encode and parse it. So a reply MAY carry binary parts --
**only when the request asked for them** with `"binary": true`:

```text
request   {"cmd": "get_image", "which": "sample", "binary": true, "client": {...}}

reply     part 0   {"ok": true, "image_meta": {...},
                    "binary": [{"key": "image", "dtype": "<u2",
                                "shape": [1096, 1936]}]}
          part 1   1096 * 1936 * 2 raw bytes, C order
```

- Part 0 is an ordinary reply dict; its `binary` list names the extra parts
  IN ORDER: `key` (where the array belongs in the reply), `dtype` (numpy's
  explicit string with byte order: `"<u2"`, `"|u1"`, `"<f4"` ...) and
  `shape`. Part *i* + 1 holds the bytes of `binary[i]`. A client puts each
  array at `reply[key]` (scan-core: `instrument.decode_reply`) and REFUSES a
  reply whose parts do not match the header (count or byte size).
- Without `"binary": true` the same verb answers in ONE JSON part, the array
  as `{"dtype": "<u2", "shape": [h, w], "b64": "<base64 of the raw bytes>"}`
  -- so a console or a client that does `recv_json()` keeps working. A
  module that predates this ignores the flag and answers in JSON, which
  scan-core decodes too. That is what makes the change additive.
- An error is always a plain one-part `{"ok": false, "error": ...}`.
- Encryption: CurveZMQ encrypts every part of a message; nothing changes.
- Service side (camera `net/service.py`): `_dispatch` returns
  `{"ok": true, ..., "_binary": [(key, ndarray)]}`, and the commander turns
  that into `[json header, raw bytes...]` with `send_multipart`.
- `check_modules.py --live` checks every descriptor with `read.binary`:
  acquire (if declared), binary read (header vs parts vs describe's dims and
  max), and the same read without binary (one part, the same values).

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

### Resonance window — sweeping only part of the band (optional, 2026-09-28)

A slow SWEPT array detector (a spectrum analyser with tracking generator, a
VNA) spends most of an FMR field sweep measuring the empty baseline. scan-core
can ask it to sweep only a window of bins around the line it predicts from the
field (recipe `window` block, `scan_core/window.py`). A detector opts in by
adding one key to its array descriptor:

```python
"window": {"arg": "window", "unit": "bin", "min_bins": 5},
```

Meaning, and the whole contract the module must keep:

- The descriptor must be a 1-D array detector (one `dims` entry: the frequency
  axis) **with an `acquire` block**; the window travels with the trigger.
- The acquire trigger verb accepts an extra argument named by `arg`:
  `window: [i0, i1]` = **inclusive bin indices of the module's FULL frequency
  grid** (the grid `coord_verb` returns). The module then sweeps only those
  bins. No argument (or a window covering every bin) = the usual full sweep.
  An out-of-range pair is clamped, never refused.
- The fetched trace (`read` verb) stays **FULL LENGTH**: the measured values at
  i0..i1 and `null` everywhere else. The reply may also carry
  `"window": [i0, i1]` (what was actually swept).
- Bins, never Hz: the measured bins then land exactly on the dataset's regular
  frequency axis, with no interpolation, ever. `min_bins` = the fewest bins the
  module will sweep (scan-core never asks for fewer).
- Detectors off the same acquisition (same `group`, same axis) are windowed
  together; scalar results computed by the module from the trace (peak, mean)
  describe the swept bins only.

scan-core fills the unswept bins from the last full sweep (its own resonance
region bridged by a straight line) and writes a `<det>_measured` mask plus the
predicted / fitted resonance, the window and the Meff used, per point.

### Streams — recording continuously, for a fly scan

A **fly scan** (scan-core, `type: fly` axis) does not stop at points: it moves
a stage slowly across a whole row while the detectors AND the stage position are
recorded continuously, then averages the detector samples per pixel of
**measured** position. For that a module offers a *stream*: every reading it
takes anyway, with the time it was taken. A parameter that can be recorded this
way declares (added 2026-09-27):

```python
"stream": {"group": "demod", "channel": "x1"}
```

`group` = the parameters recorded together (one start / read / stop for all of
them, like an acquire group); `channel` = this parameter's name in the stream
reply. Declare it on the DETECTORS a fly scan should record and on the POSITION
it should bin by (kim: `position_x/y/z`, group `position`). The same parameter
keeps its `acquire` block for ordinary stepped scans.

Three verbs, all optional -- a module without `stream` blocks needs none:

    stream_start  -> {"ok": true, "stream_id": n}     clear the buffer, start recording
    stream_read   -> {"ok": true, "stream": {...}}    everything since the last read (drains it)
    stream_stop   -> {"ok": true, "stream": {...}}    the rest, and stop

    "stream": {"id": n,
               "t": [...],                            # time.time() of each reading
               "values": {"x1": [...], ...},          # one list per channel, len(t) each; NaN -> null
               "delay_s": {"x1": 0.04, ...},          # how LATE each channel is (see below)
               "overflow": false,                     # the bounded buffer dropped samples
               "now": 1790499384.81}                  # time.time() at the reply

Rules that matter:

* **Stamp with `time.time()`**, the wall clock, not `time.monotonic()`: the
  coordinator lines this stream up with another instrument's, possibly on
  another PC, and only wall time is shared. `now` lets scan-core estimate the
  offset between the two clocks (from the read with the shortest round trip).
* **`delay_s` is the channel's lag, stated live.** A lock-in's output at time t
  is its input averaged over the preceding few time constants, so it describes
  where the stage WAS. For n identical RC stages the right number is the
  **group delay n·τ** (a convolution moves a feature's centroid by the kernel's
  mean), with the τ the hardware actually applied. Unfiltered channels (a
  position counter, AUX IN) say 0. scan-core moves every sample back by it.
* **Record in the thread that already reads the hardware** (hf2's poll thread),
  or in a small sampler thread started by `stream_start` (kim), under the same
  lock as every other backend call. `append` must stay cheap.
* **Stream in WIRE units**, like status: scan-core applies the descriptor's
  `scale` (pm16 streams W, the scan records mW).
* **Stamp an averaged reading at the MIDDLE of its window** (pm16: each
  reading is a 60 ms mean) and declare delay 0; stamping the end would add
  half the window as an uncorrected lag.
* **Bound the buffer** and say `overflow` when it drops samples.
* **Time the loop with `time.sleep`, not `Event.wait(period)`**: on Windows a
  timed wait rounds up to the 15.6 ms system tick (docs/DEVELOPER_NOTES.md
  gotcha #34), and a "50 Hz" stream quietly runs at 32.

The recorder is `stream.py` (`StreamRecorder`), copied into each module that
streams, as `theme.py` is. `tools/check_modules.py --live` checks the verbs for
every module that declares a stream.

**Array streams -- a whole trace per sample (2026-10-09, `vna-control`).** An
ARRAY detector (one with `dims`, a VNA trace) can stream too: declare the same
`stream` block on it, and send one whole trace per sample. A few optional keys
in the reply make that honest (all optional; a module that needs none sends
none):

    "values":   {"s":  {"re": [[...], [...]], "im": [[...], [...]]},   # (samples, points), complex
                 "p1": {"re": [...], "im": [...]}},                    # a scalar complex channel
    "t":        [...],                 # one stamp per sample: the sweep's MIDDLE
    "t_start":  [...], "t_end": [...], # when each sweep began and ended
    "t_ch":     {"p1": [...]},         # a channel's OWN stamps (default: "t")
    "errors":   {"u": "u needs a reference and there is none: ..."},
    "settings": {"sparam": "S21", "start_Hz": 1e9, "stop_Hz": 6e9,
                 "points": 1601, "channels": {"p1": 640}}

* **Complex** travels as `{"re", "im"}` (JSON has no complex numbers), nested
  one level deeper for a trace. scan-core bins it COHERENTLY.
* **Stamp a trace at the middle of its sweep and declare delay 0.** The points
  are measured one after another; while the knob moves at a constant pace the
  mean position over the sweep is the position at its middle. What remains is
  smearing over one sweep, which scan-core logs (from `t_start` / `t_end`).
* **Single points of the same measurement** are scalar channels of the same
  group with their own stamps in `t_ch` -- the VNA stamps point i at
  `t_start + (i + 0.5) T / n`, the centre of its dwell. They are 0-D, so they
  bin like any scalar, with no smear.
* **`errors`, not silence.** A channel that cannot be produced now (u without
  a reference) is NAMED in `errors` with the reason, and left out of `values`;
  a scan that records it stops with that message. Never send a row of NaN.
* **`settings` = what the samples are only comparable under.** scan-core pins
  the first `settings` it sees and refuses any change for the rest of the scan
  (the stream is restarted every row, so a change between rows would
  otherwise go unnoticed). Within one stream the module itself should fail the
  read (`ok: false` with the reason) when they change.
* The trace's coordinate still comes from the `dims` block (`coord_verb`), read
  once per scan; the streamed traces must have exactly that many points.

### Ramps — a knob the module can SWEEP, so a fly scan can fly it (2026-10-09)

A stage moves continuously by itself; most knobs do not -- a generator jumps
to the frequency it is told. A control that the module CAN sweep at a set pace
(field, frequency, power, phase, wavelength, temperature) declares a `ramp`
block, and scan-core's fly axis then flies it like a stage: the module sweeps
the knob over each row, the detectors stream, and every sample is binned by
the ramp's readback. Pilots: clMag `field`, dssg `frequency`, ppms `field`;
since 2026-10-10 also tc200 `temperature` (software, measured), ppms
`temperature` (hardware), superk `wavelength_n` (software, one sweep at a
time over 8 lines, command) and elliptec `angle_<addr>` (hardware, measured).

```python
"ramp": {
  "kind":     "software",                 # or "hardware": who walks the value --
                                          # the SERVICE (its own thread/loop) or the
                                          # INSTRUMENT itself (MultiVu, a VNA sweep)
  "start":    {"verb": "ramp_field",      # sweep from where it is now ...
               "args": {"to": "field_mT", # ... the wire NAMES of the target and
                        "rate": "rate_mT_per_s"},   # the pace
               "extra": {}},              # optional fixed arguments
  "stop":     {"verb": "ramp_stop"},      # end it WHERE IT IS (Abort)
  "rate":     {"unit": "mT/s",            # the knob's unit per second
               "min": 0.01, "max": 20, "default": 1},   # live, from cfg
  "readback": {"stream": {"group": "field", "channel": "field"},
               "measured": True},         # what a fly row is BINNED BY
  "done":     {"key": "ramping", "id_key": "ramp_id"}   # status keys
}
```

Rules that matter:

* **Units are the descriptor's SCAN units.** `to` and the rate are multiplied
  by the descriptor's `scale` on the wire, exactly like a set (dssg: MHz and
  MHz/s in the scan, Hz and Hz/s on the wire). The readback stream carries
  wire units, like every stream.
* **The start reply carries the sweep's number** (`{"ok": true, "ramp_id": n}`),
  and the status publishes `ramp_id` (the newest sweep TAKEN UP) and `ramping`.
  The sweep is over when the status shows OUR number and `ramping` false --
  numbered for the reason acquisitions are (gotcha #17): a "not ramping" frame
  from before the start must not pass for the end. A module whose control
  thread takes commands up later (clMag) publishes the number only after the
  state has changed, and its status() reads the number FIRST (as `cmd_done`).
* **`readback.measured`** says what the samples are binned by -- and the data
  file's fly coordinate says it too (`fly_binned_by`):
  * `true` -- the instrument's REAL value while it sweeps (clMag's Hall probe,
    the PPMS magnet's field from MultiVu): binned by measurement;
  * `false` -- the value the service COMMANDED, recorded with the time it was
    sent (a generator: reading FREQ:CW? back on every step would halve the
    step rate on a serial line): binned by command.
  The readback is a `stream` (as for any streamed parameter, same verbs, same
  group rules), or a status key (`"read_path": [...]`, scan-core samples it
  itself -- coarse: the status rate), or absent with `measured: false`
  (scan-core then computes the commanded value from the start time and rate).
* **A set takes the knob over**: any ordinary set of the knob (and the
  module's shutdown) stops a running sweep first. A new start replaces a
  running sweep from wherever it got to.
* **`ramp_stop` is a stop**: put it in the control lease's `safety` set (a
  viewer may send it), and `stream_read` in `read`.
* **Clamp, never refuse silently**: a target or a rate outside the limits is
  clamped and warned like every setter; a target the module cannot reach at
  all (clMag without a calibration) is REFUSED in the caller's thread
  (ok:false), and the block is then better not declared at all.
* **A software ramp computes each value from the ELAPSED TIME**, not one step
  per tick: a late tick then just sends a value further along and the pace
  stays exact. Use `suite_common/softramp.py` (`SoftRamp`, copied byte for
  byte into the module as `softramp.py`, like `hwlock.py`; check_modules
  compares the copies): deadline timing (gotcha #34), clamped to live limits,
  stoppable, and it records every value it sent in the stream format, so the
  stream verbs can hand its record out as is. Call `stop()` BEFORE taking the
  lock its setter needs.
* **A closed loop follows a moving setpoint** (clMag): no freeze while it
  moves, the drive never steps back against the sweep (hysteresis, gotcha
  #11), and at the end the ordinary endgame (freeze, seek, STABLE) settles it.

Add a small **Sweep** control to the GUI where it fits (rate, "Sweep to",
"Stop"). `tools/check_modules.py --live` starts a short sweep on the scratch
service for every declared ramp: its number must show up finished, the
readback stream must have recorded it, and the stop verb must answer.

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
`adopt_then_flag` also takes `tol` (default 1e-6): how far the echoed setpoint
may be from the requested one.

**A stage settles on a TARGET ECHO, not on `moving` alone (2026-09-28).** Publish
the target each axis is moving to (`target_um`, a per-axis list) and declare

    "settle": {"policy": "adopt_then_flag", "setpoint_key": "target_um",
               "flag_key": "moving", "invert": true, "index": 0, "tol": 0.01}

(`index` applies to both keys.) Two ordering rules make it honest: the setter
issues the hardware move FIRST and only then stores the echo target; the status
worker reads the echo target BEFORE it reads `moving` from the hardware. A STOP
sets the echo to where the axis stopped; a clamped move echoes the clamped
target; a target rounded to whole steps needs `tol`. Why: developer notes,
gotcha #40.

### Status keys that say "do not trust me" — `hw_error`, `fault`

Every module should publish these two strings in its status (both `""` when all
is well; a missing key counts as fine):

| key | meaning | set it when | clear it |
|---|---|---|---|
| `hw_error` | the last hardware read FAILED; this frame's values are not the instrument's | a read raises (timeout, I/O error, device gone) | on the next good read |
| `fault` | measuring now would give wrong data; a person may be needed | the module detects it (the camera lost its pattern, water lost) | by itself when the cause goes, or — if the module LATCHES it — by the `clear_fault` action |

A module that latches its fault declares an action `clear_fault` in `describe`
(it may refuse while the cause is still there). scan-core never settles on a
frame carrying either key, checks them for every instrument a scan uses before
and after each point is read, and PAUSES the scan until they are gone (the
measurement suite shows a "Clear fault on <module>" button for a latched one),
then measures the point again. So: **never publish a stale value as if it were
fresh** — say `hw_error` instead.

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
3. **Generate it:** `python tools/new_module.py x --like smb --category source --name "..." --description "..."`
   (`--like clMag` for closed-loop, `--like hf2` for a detector with an
   acquisition). It lands in `modules/<category>/x-control`. This copies the template, renames package / classes / imports,
   takes the next free port pair and writes `module.toml`, a placeholder
   `icon.svg`, a stub README and private notes (`CLAUDE.local.md`, not in git). `uv sync --extra gui; uv run pytest`
   passes at once. The launcher and scan-core already list it.
4. Rewrite `config.py` groups for X's quantities + a `Limits` envelope.
5. Rewrite `backends/base.py` Protocol; write `sim.py`; write the real driver with
   a lazy hardware import and the right SCPI/API, claiming its address with
   `hwlock.claim()` in `open()` and releasing it in `close()` (§3). Do not edit
   `hwlock.py` in the module: it is a copy of the suite-common master.
6. Trim/extend the brain: set-and-forget → strip loop/PID/state; closed-loop →
   keep and retune.
7. `net/protocol.py` `*_to_dict` helpers; `service.py` `_dispatch` verbs;
   `client.py` facade + `RemoteStatus`; `net/describe.py` for X's variables.
   **Control (§6, "Control — one controller, many viewers"):** if the template
   did not bring `control.py` + `apps/control_bar.py`, copy them from
   suite-common and wire them in; decide which of X's verbs are SAFETY verbs
   (stop / abort / off -- always allowed, also for a viewer).
   **Encryption (§6, "Encryption -- CurveZMQ"):** the template brings
   `secure.py` and the wiring; keep the service, client and console code that
   calls it when you rewrite them.
8. Scripts: `run_service.py`, `<x>_console.py`, `run_gui.py`, `smoke_test.py` --
   keeping the command-line contract of section 11.
9. GUI: `theme.py` came with the copy; build `MainWindow` + a new signature
   indicator widget for X; `settings_dialog.py` tabs = config groups. Draw a real
   `icon.svg`. Remote mode gets the control bar; `mark_always` X's safety
   buttons.
10. Tests (§9), then `python tools/check_modules.py x --live`, then an offscreen
    render (`python tools/render_all.py x`).
11. Update the module's README (and `docs/DEVELOPER_NOTES.md` if a shared rule changed); commit.

## 11. The module contract: how the suite finds a module (added 2026-09-15)

Nothing in the suite lists modules by hand. The launcher (mission-control),
scan-core, the render and deploy tools all ask **module discovery**
(`suite-common/src/suite_common/modules.py`), which reads every
`<root>/modules/<category>/<folder>/module.toml`. A folder with that file IS a
module.

**Where a module lives (since 2026-09-27).** In `modules/<category>/<key>-control`,
where `<category>` is the `category` of its own `module.toml` -- so the folder
tree reads like the Add-module wizard: `modules/motion/kim-control`,
`modules/detector/hf2-control`, `modules/field/clMag-control`. The suite's own
projects (`mission-control`, `scan-core`, `suite-common`) and `tools`,
`installer`, `docs` stay in the root. A module folder dropped straight into the
root is still found (the old layout), but `check_modules.py` warns about it, and
if the same key exists in both places the `modules/` copy wins and the other is
reported as a problem. Because a module sits three folders below the root, its
README links back up with `../../../` (e.g. `../../../front-panels/kim.png`), and
the tools are `python ../../../tools/check_modules.py <key>` from inside it.

**One deliberate exception (2026-10-05): `scan-core/module.toml`**, the SCAN
SERVER (key `scanserver`, category `coordination`). It is not an instrument but
a service that runs scans over the instruments, and it IS scan-core (same
environment), so its manifest sits in that suite project. Discovery marks it
`suite_project` (`suite_common.modules.SUITE_PROJECTS`): it is not moved by
migrate_layout, not packed, not in catalog.json, installed with scan-core;
`is_instrument` is False, so no scan engine builds parameters from it. Do not
copy this for an instrument module. Developer notes, section 4f.

```toml
[module]
key = "hf2"            # unique; letters/digits/_; must equal "module" in describe
name = "Lock-in"       # launcher card title
description = "Zurich HF2LI 50 MHz - 2 demodulator channels + aux inputs"
category = "detector"  # what it is FOR: motion | field | source | detector |
                       # imaging | environment | io | coordination | other
                       # (a typo is an error; coordination = scan servers)
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

# [hardware]           # optional (smb and kim, for example, have one):
# address_arg = "--visa"      # the run_service.py flag that takes the address
# bus = "visa"                # what it wants: visa | serial | ip | device
# probe = "scripts/probe.py"  # optional: LISTS the devices its vendor library sees
```

**`[hardware]` (2026-10-01).** A module whose service takes the instrument's
address on the command line declares the flag and the kind of address here.
Mission Control's **Instruments…** (README, "What it looks like") then offers
a found GPIB / USB / LAN / COM address to every module it fits, stores the
choice for this PC in `suite_local.json`, and starts the service with
`--real <address_arg> <address>`. `bus`: `visa` takes a VISA resource string
(a COM port becomes `ASRL5::INSTR`), `serial` a COM port name, `ip` a host or
IP, `device` an id the module's own vendor library understands verbatim (a
Kinesis serial, `Dev1`, `dev1234`, a camera serial). `check_modules.py` checks
that the script accepts the flag. Add the flag WITHOUT changing the default:
absent, the service uses its config exactly as before.

**A probe (2026-10-03)** is how an instrument that is not VISA or COM shows up
in *Instruments…*. Declare `probe = "scripts/probe.py"`; Mission Control runs
it with the module's own venv python (where the vendor library lives -- the
launcher installs no vendor SDK), in a thread, with a 20 s timeout. Contract:

- It **only lists**. It never opens a device, never sends a byte, never changes
  a setting: enumerate (`list_kinesis_devices`, `DeviceManager.Update()`,
  `System.local().devices`, `saGetSerialNumberList`, `ziDiscovery.findAll`,
  `TLPMX_findRsrc` with vi = 0), then stop. Mark each vendor call `# VERIFY`
  until it ran on the instrument.
- It prints ONE ASCII JSON line and exits 0, also when nothing is found:
  `{"devices": [{"address": "...", "identity": "...", "detail": "...",
  "lock": "..."}], "note": "..."}`. `address` is what `address_arg` takes;
  `lock` (optional) the address the backend claims in hwlock when it is spelled
  differently (`CAMERA::<serial>`), so a held device is shown as held.
- The vendor library is imported LAZILY inside the probe function; missing, the
  probe prints `devices: []` and a note saying what to install (`uv sync
  --all-extras`, the vendor runtime). A probe is the one place besides the real
  backend that may import the vendor library.
- A vendor list can hide a device a service has OPEN (pylablib listed nothing
  while kim held the KIM101), or list it differently (TLPMX: no serial). So
  the probe also reports what its own module holds (`hwlock.held()`), as
  "held by the running <key> service" -- appended to a listed device's detail
  too, and a serial-less listing is folded into the held row, never a second row.
- A device the vendor library lists that is NOT for this module (Kinesis' FTDI
  scan also sees the Signal Hound TG44A) gets `"other": true`: shown as
  information, never suggested for or offered to the module.
- Put the logic in `src/<pkg>/probe.py` (a `probe() -> dict` function) and keep
  `scripts/probe.py` a thin wrapper; test it with a FAKE vendor library in
  `sys.modules` whose open call raises (see kim-control's `tests/test_probe.py`).
  `check_modules.py` runs the probe twice: as installed, and with the vendor
  SDK made missing; both must print the JSON line.

The operating system's USB list (`suite_common/usb_devices.py`) needs nothing
from the module: add the device's VID:PID to `KNOWN_USB` there, with the module
key, once it has been seen in Device Manager.

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
the launcher, read by everyone: `{"modules": {key: {"real", "cmd", "pub",
"address"}},
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
