# AaltoFlow

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22959230.svg)](https://doi.org/10.5281/zenodo.22959230)

**Lab automation built from independent instrument modules.** Every instrument
runs as its own small service with its own GUI; a launcher starts them, and one
scan engine drives any combination of them through N-dimensional measurements.
Nothing in the engine knows what a magnet or a lock-in is -- a new instrument is
a new folder, and it appears in the launcher, the control panel and the scan
builder by itself. The companion data viewer is
[AaltoView](https://github.com/FlashLukas/AaltoView).

Several people can work on one setup at once: the first GUI to connect **controls**
an instrument, every other window on another PC is a live **viewer**, and the
STOP / RF-off buttons work from all of them -- see
[Control](#control----one-controller-many-viewers).

*Aalto* is Finnish for "wave". AaltoFlow was born as the Python replacement of
the LabVIEW software of the TR-MOKE (time-resolved magneto-optical Kerr effect)
setup of the NanoSpin group, Aalto University, and was called TRMOKE until
2026-09-24. An installation can still say which setup it drives: Setup asks for a
**setup name** (e.g. "TR-MOKE"), and every window title leads with it.

The point of the rewrite is not a one-for-one port. It is to separate **the
measurement engine from the instruments**, so that arbitrary N-dimensional
acquisition is a generic capability rather than something hardcoded for one
experiment. The old `ControlTRMOKEV21setparam.vi` had a hard two-loop ceiling and
a fixed parameter set; adding one knob meant editing seven places. Here, adding a
knob means registering one `Parameter`.

> **Status: mostly simulation.** Every module runs, is tested and has a GUI.
> Hardware passes are under way on the lab PC: pm16 (Thorlabs PM16 power meter),
> kim (KIM101 inertia stage), the IDS camera and the Signal Hound SA44B have run
> on the real instruments. **[Verified instruments](docs/VERIFIED_INSTRUMENTS.md)**
> lists what was checked, the tests and commits behind it, and the caveats found.
> Elsewhere, every unverified hardware call is marked `# VERIFY`. See
> [Hardware passes](#hardware-passes).

One-page **flyers** (PDF and PNG) for the suite, the camera, clMag, the KIM stage
and the Navigator are in [`docs/flyers/`](docs/flyers/); two demo **videos**, a short
one and a screen recording on the lab setup, are in [`docs/video/`](docs/video/).

## Architecture

Three layers, each of which can be run, tested and replaced on its own.

```mermaid
flowchart TB
    MC["mission-control<br/><i>launcher: spawns services and GUIs</i>"]
    SC["scan-core<br/><i>recipe + registry + N-D engine</i><br/><b>no instrument knowledge</b>"]
    subgraph SVC["instrument services -- one process per instrument"]
        direction LR
        M["clMag<br/>5555/6"]
        S["smb<br/>5557/8"]
        ST["stage<br/>5559/60"]
        P["piezo<br/>5561/2"]
        C["camera<br/>5563/4"]
        Z["zpiezo<br/>5565/6"]
        K["kim<br/>5567/8"]
        H["hf2<br/>5569/70"]
        PM["pm16<br/>5571/2"]
        V["vna<br/>5573/4"]
        M2["mag2d<br/>5575/6"]
        M2C["mag2dcal<br/>5577/8"]
        PP["ppms<br/>5579/80"]
    end
    G["GUIs and consoles<br/><i>clients, not owners</i>"]
    SC -- "ZeroMQ" --> SVC
    G -- "ZeroMQ" --> SVC
    C -. "drives XY" .-> P
    C -. "drives Z" .-> Z
    V -. "listens to the field" .-> M2
    V -. "or the DynaCool's" .-> PP
    MC -.->|spawns| SVC
    MC -.->|spawns| G
```

**1. Instrument services.** One process per instrument, and that process is the
single source of truth for it. Each speaks the same wire contract: ZeroMQ,
REQ/REP JSON for commands, PUB/SUB for telemetry. Commands are
*fire-and-forget* — a reply of `{"ok": true}` means *accepted*, not *done*; the
caller polls `status` for the effect. Every module has the same universal verbs
(`status`, `info`, `get_config`, `set_config`, `describe`, `shutdown`) plus one
verb per setter.

Because every service accepts many clients at once, each one also decides
**who may change the instrument**: one PC has control, every other GUI is a live
viewer whose knobs are locked, and STOP-type buttons work from everywhere (see
[Control](#control----one-controller-many-viewers)).

Because the transport is the network, a client does not care whether it runs on
the same PC or across the lab Ethernet — only the host changes. `camera-control`
demonstrates this: it owns no motion hardware at all and drives XY through
`piezo-control` and Z through `zpiezo-control` as an ordinary client.

**2. scan-core — the coordinator.** This is the generic part, and it contains no
TR-MOKE physics:

- **A scan is data.** A *recipe* is YAML: `fixed` values, an ordered list of
  `axes` (outer → inner, no length limit), `detectors`, and `hooks`. You can
  save it, diff it, version it and re-run it headless.
- **Every knob is a registered Parameter.** A `Settable` has limits, a blocking
  `set` and a `get`; a `Gettable` has a `get`. Adding an instrument means adding
  Parameters — they then appear in the builder and the engine with no other
  edits.
- **The engine is an odometer** over the compiled dimensions. It sets only the
  axes whose index changed, fires hooks, reads every detector, and returns an
  `xarray.Dataset` with named coordinates, units and the full recipe in its
  metadata, written to netCDF. 1-D and 5-D are the same code path.

Nothing in `recipe.py`, `registry.py` or `engine.py` knows what a magnet is, and
scan-core imports no instrument package at all: it reaches the services through
one generic ZeroMQ client, because they all speak the same contract. Connecting
an instrument is a declaration in `scan_core/lab.py` — which verb sets the knob,
which status field reads it back, and how you know it has arrived — not new
engine code.

**3. mission-control.** The launcher. It *finds* the modules (every folder
`modules/<category>/<name>-control` with a `module.toml`), starts their services, opens their GUIs, lets you change ports and
add services running on other PCs, and shows each module's variables as the
service reports them. It never imports the instrument packages; it only spawns
their scripts, so there is no version coupling. The measurement suite follows
what the launcher has.

## Control -- one controller, many viewers

Every module is a network service, so many clients can be connected to one
instrument at the same time: GUIs on several PCs, the measurement suite,
scan-core, another module (the camera drives the piezo stages), scripts and
consoles. That is what makes remote work and teaching possible -- and it is
also how a trainee's click on another PC ends up moving a stage under somebody
else's measurement. AaltoFlow therefore has one rule for all 38 modules:

> **One PC controls an instrument; everybody else watches.** The first GUI to
> connect gets control. Every later GUI opens as a **viewer**: it sees all
> readouts live, but nothing in it can be changed -- except the buttons that
> make the instrument *safe* (STOP, RF off, output off, ...). Control changes
> hands only by a deliberate "Take control".

The GUI that holds control shows a quiet line at the top of its window -- who
else is watching, and which programs are driving the instrument as well:

![a GUI that holds control](front-panels/control-holder.png)

The same instrument seen from another PC. The amber bar says who has control
and since when. Every knob swallows its clicks; a blocked click flashes the bar
red and says why, and **RF Off** still works because it is a safety verb:

![a viewer GUI](front-panels/control-viewer.png)

### How a GUI locks itself

A module window started on its own (no `--connect`) owns its instrument
in-process and has nobody to share it with, so it has no lock and no bar.
Everything below applies to a window **connected to a service**, and that is
how the launcher opens a GUI whenever the module's service is running.

```mermaid
stateDiagram-v2
    [*] --> Asking: the window opens and connects
    Asking --> Control: nobody holds control
    Asking --> Viewer: another PC holds control
    Control --> Viewer: Release, or another PC takes over
    Control --> Viewer: silent for 10 s (lease ran out)
    Viewer --> Control: Take control (asks first if another PC holds it)
    Viewer --> Viewer: a click on a knob is swallowed, the bar flashes red
```

1. **At start the window asks once.** When the window has been built, it
   asks the service for control *without* force. If nobody holds control, it
   gets it, and the log says "this window has control". If another PC holds
   control, the window opens as a viewer, and the log says who holds control.
   It never takes control from somebody by itself, not even later, when control
   becomes free. That is why the *first* GUI gets control and why control changes
   hands only when somebody clicks.
2. **The bar follows the service, not the window.** Every status frame the
   service broadcasts (several per second) carries `control: {holder,
   clients, lease_s}`. The window's status timer passes it to the bar, and the
   bar redraws only when something in it changed:
   - **holder** (this PC): "You have control (this PC) · watching: <who> ·
     also driving: <machine>", with a **Release** button;
   - **viewer**: an amber bar, "VIEWER -- <who> has control since hh:mm;
     nothing can be changed here", with a **Take control** button;
   - **nobody holds control** (the holder released it or went silent): still a
     viewer, "nobody has control at the moment", and one click on Take control
     makes it yours. Nothing becomes yours without a click, so a person who
     walks up to a screen always knows whether it can change the instrument.
3. **A viewer's inputs swallow their input.** The bar installs one event
   filter for the whole application. In viewer mode it throws away mouse
   presses, releases, double clicks, the wheel and key presses on every *input*
   widget of the main window: buttons, number boxes, combo boxes, text fields,
   sliders. A blocked press turns the bar red for a moment, so the person sees
   *why* nothing happened. Everything that only shows keeps working: readouts,
   live plots and camera pictures, tabs, scrolling, selecting text in the log.
4. **Safety buttons are exempt.** A button marked with `mark_always` (STOP,
   RF off, Kill AF, Cancel zero, Settings, Refresh, ...) passes the filter,
   because the service accepts its verb from anyone. A module with one on/off
   toggle and no separate off button (kepco, chopper, cs260, mag2d, ...) marks
   the toggle *while it reads "off"*: a viewer can then switch the output off
   (the toggle sends the safety verb), but not on.
5. **Dialogs are not guarded.** A viewer may open Settings and *look*. Its OK
   goes to the service, and the service refuses it with a message that names
   the holder.

**Why an event filter and not `setEnabled(False)`:** every GUI already enables
and disables its own widgets from its status timer (during an autofocus, while
a stage is homing, when the instrument is disconnected). Greying the window
from outside would fight that code, and switching everything back on at the
next hand-over would enable buttons that should stay off. The filter leaves the
widgets exactly as their own code wants them and only drops what a person does
to them.

**Why the GUI locks at all, if the service already refuses:** the service is
what protects the instrument. Without the lock in the window, a viewer could
turn a number box to a new value and click Set; the box would show the new
value, and only then would an error arrive. The window would then show a
setting the instrument does not have. With the lock, what a viewer window shows
is always what the instrument is doing.

### The rules

- **The service enforces it, not the GUI.** Every request may carry a
  `"client": {"id", "kind", "name", "host"}`. The first thing a service does with
  a command (`ControlLease.handle`, in `control.py`) is check who sent it. A
  command that would change something is refused unless it comes from the
  holder's PC, with `{"ok": false, "refused": "control", "error": "read-only:
  <who> has control since hh:mm ... take control first"}`. The client libraries
  raise `ControlRefused` on that reply, so a script never believes a setting
  was taken when it was not. The viewer bar in the GUI only makes this visible
  -- it is not what protects the instrument.
- **Control belongs to a PC, not to a window.** Any client whose `host`
  (`user@PC`) names the holder's PC may change things, keep control alive and
  release it: a GUI and a console on the same PC work together. A window on a
  different PC is a viewer.
- **Always allowed, for everyone:** reading (`status`, `info`, `describe`,
  `get_config`, every `get_*` / `read_*` / `list_*` verb, plus a module's own
  read verbs such as `stream_read`), the module's **safety verbs** (below),
  `shutdown` (the launcher's clean stop) and the control verbs themselves. A
  person who sees a stage run away must be able to stop it from any window.
- **Safety verbs only make things safe.** If a module's only "off" is a
  setter that can also switch something *on* (`set_rf(false)`,
  `set_output(false)`), the module has a dedicated verb that can only switch
  off (`rf_off`, `output_off`, `ramp_to_zero`, ...). Acquisition triggers
  (`acquire`, `take_reference`) are *not* safety: a new trigger replaces the
  sample another client is waiting for. Every safety verb is also an action in
  the module's `describe`, so the suite's Control tab can offer it to a viewer.
- **Machines are not locked out.** A client with `kind: "machine"` passes the
  lock: the camera moving the piezo stages during an autofocus, the
  signal-generator module driving the Signal Hound's tracking generator,
  scan-core during a scan. Opening a GUI must never break a running autofocus.
  So that a moving stage is never a mystery, a machine that changed something
  in the last 10 s is shown as **"also driving: <name>"** in every control bar
  and in the suite.
- **A silent holder loses control.** Clients send a `heartbeat` every 2 s. A
  holder that has been silent for 10 s (a crashed GUI, a closed laptop) loses
  control, so no instrument stays locked for good.
- **Taking over is announced.** `take_control` succeeds when nobody holds
  control. `take_control(force=True)` ("Take control" in a viewer, `take!` in
  a console) takes it over from another PC (the GUI asks first); the old
  holder becomes a viewer, and its log says who took over.
- **Nobody holding control means no lock.** With no holder every command
  passes, with or without an identity, exactly as before. A headless setup and
  an old script therefore keep working unchanged.
- **A script is a client like any other.** A script or console (`kind:
  "script"`) can always read and stop. While a GUI on another PC holds control
  it must take control before it may change anything, and that take-over is
  visible to the GUI it took it from.

### Scans and control

- **A scan needs control.** Before anything moves, scan-core claims every
  instrument the scan uses (`claim_scan`). The claim is refused while another
  PC holds control of one of them: the scan stops with `ScanBusy` naming the
  holder, and nothing has been sent. If the scan's own PC holds control, the
  person keeps it afterwards. If nobody holds control, the scan's PC takes it
  for the length of the scan, and it is freed again at the end (also after an
  abort or an error).
- **One scan at a time per instrument.** While a scan holds the claim, a
  second scan engine is refused (`busy: scan '<label>' from <who> ... since
  hh:mm`), whether it runs on the same PC or on another one. Heartbeats keep
  the claim alive through long settles, and a crashed scan frees it after 10 s.
  Control bars and the Control tab show `scan '<label>' running (<PC>)`.

### In the measurement suite

The suite's Control tab counts as a person, not a machine: its clicks go out as
a GUI client, while its scan engine stays a machine. A strip of chips at the
top shows the state of every connected module -- green ● you have control,
amber ◆ another PC has it, grey ○ nobody has it (▶ while a scan runs). The
tooltip says who and since when, and a click on a chip offers Take control or
Release. A padlock on each module in the AVAILABLE tree shows the same state.
While another PC holds control, the tab greys that module's knobs, except
the actions the service still accepts. The suite does not take control by
itself when it connects.

![the Control tab with control chips](front-panels/suite-control.png)

Above, this PC holds clMag (●), a GUI on another PC holds kim and hf2 (◆, closed
padlocks), and smb and piezo are free (○).

### From a script or a console

```python
from kim.net.client import KimClient

c = KimClient(host="lab-pc", kind="script", name="my alignment script")
c.start()                         # starts the heartbeat as well
c.take_control()                  # False if a GUI on another PC holds it ...
c.take_control(force=True)        # ... this takes it over (the GUI is told)
c.move_to_step(0, 4000)           # raises ControlRefused if control was lost
c.release_control()
c.close()
```

Every console (`scripts/<module>_console.py`) has `take`, `take!` (take
over), `release` and `clients` (who holds control, who is connected), and
sends heartbeats while it is open.

### What counts as safety, per module

| module | safety verbs (always allowed) | extra read verbs |
|---|---|---|
| agilis, ddr25, smaract | `stop` | `stream_read` |
| elliptec, piezo, stage | `stop` | |
| kim | `stop`, `abort_px_calibration` | `stream_read` |
| chopper | `stop` (wheel to standby) | |
| camera | `kill_af`, `cancel_laser_target` | `stream_read`, `camera_features` |
| clMag | `ramp_to_zero` | `aux_read_ai` |
| mag2d, mag2dcal | `zero`, `output_off` (mag2dcal: `zero` also aborts a calibration) | |
| kepco | `output_off` (ramped), `output_off_now` | `ping` |
| k2450, dsphase | `output_off` | |
| sr830 | `output_off` (SINE OUT to its minimum, AUX OUTs to 0 V) | `stream_read` |
| sr7230 | `output_off` (oscillator to 0 V) | `stream_read` |
| smb, hp8648, dssg, shsg | `rf_off` | |
| windfreak | `all_rf_off` | |
| superk | `emission_off` | `ping` |
| dsamp | `amp_off` | |
| tc200 | `heater_off` | |
| cs260 | `abort`, `close_shutter` | |
| gsp818 | `abort`, `tg_off` | |
| signalhound | `abort`, `tg_abort` | `tg_grid` |
| ccs200, shsna, vna | `abort` | |
| pm16 | `cancel_zero` | `stream_read` |
| pm400 | `cancel_zero` | |
| hf2 | -- (drives no output) | `stream_read` |
| ls455 | -- (a gaussmeter: nothing to switch off) | `reread_probe` |
| ppms | -- (every verb is a setpoint or a rate) | |
| usb6001 | -- (an output value that is safe depends on the setup) | |
| zpiezo | -- (a voltage is a focus position; nothing moves on its own) | |

The lock guards against mistakes between people who follow the rules. It is
**not security**: the `kind` is declared by the client itself, and whoever
reaches the port can send anything. The firewall is what keeps other people
out. The full rules and the reasons behind them are in
[docs/DEVELOPER_NOTES.md](docs/DEVELOPER_NOTES.md), section 4 ("Control");
how to add control to a new module is in
[INSTRUMENT_MODULE_GUIDE.md](INSTRUMENT_MODULE_GUIDE.md), section 6.

## Adding a module

Nothing lists modules by hand. A folder with a `module.toml` (name, description,
icon, default ports, scripts) *is* a module, and the launcher, scan-core and the
tools all find it. The module's controls and measured variables are not in that
file: the running service reports them itself through `describe`.

The modules are sorted by what they are for: `modules/<category>/<name>-control`,
where the category (`motion`, `imaging`, `detector`, `source`, `field`,
`environment`) is the one written in the module's `module.toml`. The suite's own
projects -- `mission-control`, `scan-core`, `suite-common` -- and `tools`,
`installer`, `docs` stay in the top folder.

```powershell
python tools/new_module.py vna --like smb --category detector --name "Network analyser" --description "R&S ZNB"
cd modules\detector\vna-control
uv sync --extra gui
uv run pytest -q                      # passes as generated
python ../../../tools/check_modules.py vna --live
```

`new_module.py` copies a working template under the new name and takes the next
free port pair. Then you replace its insides. Contract and walkthrough:
[INSTRUMENT_MODULE_GUIDE.md](INSTRUMENT_MODULE_GUIDE.md), sections 10 and 11.

## What it looks like

Everything below runs with no hardware attached -- against the simulator, or
against services running in simulation mode. The data in the screenshots is
simulated FMR from a **patterned sample**: discs and bars of different sizes,
each resonating at its own frequency because its shape anisotropy differs, on a
continuous film between them. That is not decoration -- a spatial map that
*changes* with frequency and field is what makes the difference between a viewer
that shows one picture and one that lets you move through a cube.

The **measurement suite** is the operator application: one window, five tabs.
Its Control tab is built entirely from what each module reports about itself,
so it knows nothing about magnets or piezo stages -- it asks, and lays out what
it is told:

![control tab](front-panels/suite-control.png)

Define a scan on the Scan tab: axes stacked outer to inner (indented by loop
depth, so which one is the slow one is visible rather than deduced), and
underneath them the **conditions** -- parameters held at a single value for the
whole run -- and the **routines** that run before, during and after it (set the
magnet to a reference field, take a VNA reference; autofocus at the start of
every row, or every 100 points; field back to 0 at the end). All of
it is saved with the definition and inside the measurement file, so a file says
what it was taken *under* and *how*, not only what was swept:

![scan tab](front-panels/suite-scan.png)

Then watch it run on the Measurement tab. Defining takes a minute; running takes
an hour, and they want different screens:

![measurement tab](front-panels/suite-measurement.png)

Several scans can run one after another. Select more than one definition in
**Load scan…** (recipes, measurement files, or a saved queue) and a dialog lets
you name them, put them in order and drop the ones you do not want. Every entry
is checked against the connected instruments first; one that names a module
that is not running is shown in red and the queue will not start until it is
fixed or removed:

![queue dialog](front-panels/suite-queue-dialog.png)

While it runs the Measurement tab says which scan it is on and how long the rest
will take. Each scan goes into its own file. **Abort** skips to the next scan,
**Stop queue** ends the whole queue, and an error stops it too, since a module
that died would fail the next scan the same way:

![a queue running](front-panels/suite-queue.png)

A finished scan is a cube, and the Data tab shows it two dimensions at a time:
choose which dims are the image axes, and every dimension left over gets a
control of its own -- hold it at one value, or average over it. Below is a
frequency x Y x X scan of the array, held at 900 MHz: the film between the
elements is near its own resonance, so the pattern reads as dark elements on a
lit background, and the one element whose resonance sits at that frequency is
lit right through. Drag the frequency slider and a different element lights up.
The controls are built from the data, so a five-dimensional scan needs no new
code:

![data tab](front-panels/suite-data.png)

```bash
cd scan-core
uv run python apps/suite.py                          # simulator
uv run python apps/suite.py --modules clMag,smb      # live services
```

The Data tab is also a program of its own, the **data viewer** (the successor
of the LabVIEW AaltoView). It needs no instruments: it lists the data folder
newest first, draws maps with a cursor and row/column cuts, overlays 1-D curves
(from one file or several, normalised and stacked), and exports what is on
screen as a figure (PNG/PDF/SVG), as data (.dat/.csv, or the clipboard), into a
running **Origin** (worksheet or matrix, with a graph), or as a **Jupyter
notebook** that recomputes the view from the measurement files:

![data viewer, 1D plots](front-panels/viewer-1d.png)

```bash
cd scan-core
uv sync --extra gui --extra origin                   # origin = the optional Origin push
uv run python apps/viewer.py [file.nc] [--folder D:\data]
```

The viewer is its own (private) repository,
[FlashLukas/AaltoView](https://github.com/FlashLukas/AaltoView), so
colleagues who only analyse data can install it without the instrument suite.
scan-core installs it as a dependency.

Mission Control starts every service and opens every GUI from one place:

![mission control](front-panels/mission-control.png)

The Scan Builder stacks axes outer-to-inner with no length limit, and runs the
engine against whichever registry it was given -- simulated here, real
instruments on the bench:

![scan builder](front-panels/scan-core.png)

Each instrument has its own panel, shown in its own README:
[clMag](modules/field/clMag-control/README.md) ·
[smb](modules/source/smb-control/README.md) ·
[stage](modules/motion/stage-control/README.md) ·
[piezo](modules/motion/piezo-control/README.md) ·
[camera](modules/imaging/camera-control/README.md) ·
[kim](modules/motion/kim-control/README.md) ·
[hf2](modules/detector/hf2-control/README.md) ·
[pm16](modules/detector/pm16-control/README.md) ·
[vna](modules/detector/vna-control/README.md) ·
[mag2d](modules/field/mag2d-control/README.md) ·
[mag2dcal](modules/field/mag2dcal-control/README.md) ·
[ppms](modules/environment/ppms-control/README.md) ·
[kepco](modules/source/kepco-control/README.md) ·
[windfreak](modules/source/windfreak-control/README.md) ·
[gsp818](modules/detector/gsp818-control/README.md) ·
[signalhound](modules/detector/signalhound-control/README.md) ·
[dsphase](modules/source/dsphase-control/README.md) ·
[dssg](modules/source/dssg-control/README.md) ·
[dsamp](modules/source/dsamp-control/README.md) ·
[agilis](modules/motion/agilis-control/README.md) ·
[smaract](modules/motion/smaract-control/README.md) ·
[sr830](modules/detector/sr830-control/README.md) ·
[cs260](modules/source/cs260-control/README.md) ·
[ccs200](modules/detector/ccs200-control/README.md) ·
[ddr25](modules/motion/ddr25-control/README.md) ·
[elliptec](modules/motion/elliptec-control/README.md) ·
[chopper](modules/source/chopper-control/README.md) ·
[superk](modules/source/superk-control/README.md) ·
[tc200](modules/environment/tc200-control/README.md) ·
[ls455](modules/detector/ls455-control/README.md) ·
[pm400](modules/detector/pm400-control/README.md) ·
[hp8648](modules/source/hp8648-control/README.md) ·
[sr7230](modules/detector/sr7230-control/README.md) ·
[k2450](modules/source/k2450-control/README.md) ·
[zpiezo](modules/motion/zpiezo-control/README.md) (headless -- a console, not a window).

They are rendered offscreen and reproducibly, so they do not go stale:

```bash
python tools/render_all.py            # refresh every panel in front-panels/
python tools/render_all.py clMag       # or just one
```

## The instruments

| # | project | package | cmd/pub | hardware |
|---|---------|---------|---------|----------|
| 0 | `clMag-control` | `clMag` | 5555/5556 | Kepco BOP + GMW 3470 dipole, Hall probe on NI USB-6259, plus AUX analog/digital I/O |
| 1 | `smb-control` | `smb` | 5557/5558 | Rohde & Schwarz SMB100A RF generator |
| 2 | `stage-control` | `stage` | 5559/5560 | Thorlabs BSC203, 3-axis coarse stepper stage |
| 3 | `piezo-control` | `piezo` | 5561/5562 | piezosystem jena d-Drive + PXY-200 XY flexure |
| 4 | `camera-control` | `camera` | 5563/5564 | IDS uEye+ U3-38J0XCP — spot/pattern tracking, stabilisation, autofocus |
| 5 | `zpiezo-control` | `zpiezo` | 5565/5566 | Thorlabs KCube piezo, Z focus (headless) |
| 6 | `kim-control` | `kim` | 5567/5568 | Thorlabs KIM101 + 3× PIA25 piezo-inertia stage |
| 7 | `hf2-control` | `hf2` | 5569/5570 | Zurich Instruments HF2LI 50 MHz lock-in — 2 demodulator channels + aux inputs |
| 8 | `pm16-control` | `pm16` | 5571/5572 | Thorlabs PM16 USB power meter (PM16-121) — **verified on hardware** |
| 9 | `vna-control` | `vna` | 5573/5574 | Keysight PNA-X N5222A **or** Copper Mountain C1209 (both untested on the instrument) **or** a simulated YIG film: S-parameters, reference, permeability u and ln(S/S_ref) |
| 10 | `mag2d-control` | `mag2d` | 5575/5576 | 2-axis vector electromagnet on an NI DAQ: field + angle, PI in mT, water-cooling interlock |
| 11 | `mag2dcal-control` | `mag2dcal` | 5577/5578 | the same magnet, controlled the way the 1-axis one is: measured B(V) calibration, PI trim, freeze, long-term stabilizer |
| 12 | `ppms-control` | `ppms` | 5579/5580 | Quantum Design DynaCool through MultiVu (MultiPyVu): field, temperature, chamber (untested on the instrument) |
| 13 | `kepco-control` | `kepco` | 5581/5582 | Kepco BOP 20-10 bipolar power supply (GPIB), its own module -- not a field loop (simulation; untested on the instrument) |
| 14 | `windfreak-control` | `windfreak` | 5583/5584 | Windfreak SynthHD PRO v2, two-channel RF synthesizer (USB serial) (simulation; untested on the instrument) |
| 15 | `gsp818-control` | `gsp818` | 5585/5586 | GW Instek GSP-818 spectrum analyzer with tracking generator (simulation; untested on the instrument) |
| 16 | `signalhound-control` | `signalhound` | 5587/5588 | Signal Hound SA44B / SA124B with USB-TG44A tracking generator (sa_api.dll) (simulation; untested on the instrument) |
| 17 | `dsphase-control` | `dsphase` | 5589/5590 | DS Instruments 6 GHz digital RF phase shifter (USB) (simulation; untested on the instrument) |
| 18 | `dssg-control` | `dssg` | 5591/5592 | DS Instruments SG12000L 12 GHz signal generator (USB or Ethernet) (simulation; untested on the instrument) |
| 19 | `dsamp-control` | `dsamp` | 5593/5594 | DS Instruments 6 GHz variable-gain RF amplifier (USB) (simulation; untested on the instrument) |
| 20 | `agilis-control` | `agilis` | 5595/5596 | Newport Agilis 2-axis piezo stage on an AG-UC2 (simulation; untested on the instrument) |
| 21 | `smaract-control` | `smaract` | 5597/5598 | SmarAct CLL42 linear positioner on an SCU controller (simulation; untested on the instrument) |
| 22 | `sr830-control` | `sr830` | 5599/5600 | Stanford Research SR830 DSP lock-in (GPIB) (simulation; untested on the instrument) |
| 23 | `cs260-control` | `cs260` | 5601/5602 | Newport / Oriel Cornerstone 260 monochromator (GPIB) (simulation; untested on the instrument) |
| 24 | `ccs200-control` | `ccs200` | 5603/5604 | Thorlabs CCS200/M CCD spectrometer (TLCCS) (simulation; untested on the instrument) |
| 25 | `ddr25-control` | `ddr25` | 5605/5606 | Thorlabs DDR25/M direct-drive rotation stage on a K-Cube (simulation; untested on the instrument) |
| 26 | `elliptec-control` | `elliptec` | 5607/5608 | Thorlabs ELL14K Elliptec rotation mount (simulation; untested on the instrument) |
| 27 | `chopper-control` | `chopper` | 5609/5610 | Thorlabs MC2000B-EC optical chopper (MC1F10HP, MC1F60 blades) (simulation; untested on the instrument) |
| 28 | `superk-control` | `superk` | 5611/5612 | NKT SuperK EXTREME EXW-12 + SELECT / SELECT2 AOTFs on one RF driver (simulation; untested on the instrument) |
| 29 | `tc200-control` | `tc200` | 5613/5614 | Thorlabs TC200 heater controller with a PT100 (simulation; untested on the instrument) |
| 30 | `ls455-control` | `ls455` | 5615/5616 | Lake Shore 455 DSP gaussmeter, axial Hall probe (simulation; untested on the instrument) |
| 31 | `pm400-control` | `pm400` | 5617/5618 | Thorlabs PM400 power/energy meter console (TLPMX) (simulation; untested on the instrument) |
| 32 | `hp8648-control` | `hp8648` | 5619/5620 | HP / Agilent 8648D RF generator (GPIB) (simulation; untested on the instrument) |
| 33 | `sr7230-control` | `sr7230` | 5621/5622 | Ametek Signal Recovery 7230 DSP lock-in (simulation; untested on the instrument) |
| 34 | `k2450-control` | `k2450` | 5623/5624 | Keithley 2450 SourceMeter (simulation; untested on the instrument) |

Each project folder lives in `modules/<category>/` (the links above go there).
Instrument *n* gets `cmd = 5555 + 2n` and `pub = cmd + 1` by default, declared in
its `module.toml`; the launcher can change a module's ports on one PC.

**Updating a checkout from before 2026-09-27** (when the modules moved into
`modules/<category>/`): if you changed tracked lab files on that PC
(`camera.ini`, `objectives.ini`, `px_calibration.json`, clMag's `Calibrations`),
commit them or `git stash` them before `git pull` (and `git stash pop` after).
Then run `python tools/migrate_layout.py` (a dry run) and
`python tools/migrate_layout.py --apply`: it carries the files git does not
track (tuned `.ini` files, calibrations, notes, data) from each old
`<name>-control` folder into the new one, and removes the old folder. Finally
re-sync each module's environment.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11. Everything runs in
simulation, so you need no hardware and no vendor drivers.

```powershell
# the whole suite from one dashboard
cd mission-control
uv run python mission_control.py
```

Or drive one instrument on its own:

```powershell
cd modules\field\clMag-control
uv sync --extra gui
uv run scripts/run_service.py                      # add --real for hardware
uv run scripts/run_gui.py --connect localhost      # add --theme light
uv run scripts/magnet_console.py --connect localhost
```

A GUI started **without** `--connect` runs its own private local simulation. To
see the state shared with the service, start the service and connect to it.

And the scan engine:

```powershell
cd scan-core
uv sync --extra gui
uv run python run_demo.py            # 2-D, 3-D and XY-raster scans -> out/*.nc
uv run python apps/scan_builder.py
```

## Installing on a lab PC

The quick start above assumes a developer's machine — git, `uv`, a terminal. For
a PC that only has to *run* the experiment there is a `Setup.exe`, built from
[`installer/`](installer/README.md):

```powershell
powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
```

Out comes `installer\dist\AaltoFlow-Setup-<date>-<commit>.exe`. Whoever runs it
ticks the modules that PC needs; each lands as a folder with its own `.venv`,
built from that module's committed `uv.lock`, plus a private `uv.exe` — so the
target machine needs neither Python nor uv beforehand. It installs per user, so
no admin rights.

Two things keep the installer from drifting away from the suite:

- **The checkbox list is generated from the `module.toml` files**, not written by
  hand — the same manifests the launcher discovers modules from. Adding a module
  needs no change in `installer/`.
- **It builds from `git archive HEAD`**, not the working tree, so a Setup.exe
  names the commit it carries. Commit first; the script warns if you did not.

Copying files works offline, but building the environments does not (`uv sync`
fetches the interpreter and the packages). On a PC where that is blocked, untick
*"Build the Python environments now"* and run **Start menu ▸ AaltoFlow ▸ Rebuild
Python environments** later from a network that works.

## Tests

All 875 tests run offline — no hardware, no network beyond localhost, GUI tests
rendered offscreen.

```powershell
cd <project>
uv run pytest -q
```

| project | tests | | project | tests |
|---|---|---|---|---|
| clMag-control | 22 | | hf2-control | 52 |
| smb-control | 29 | | pm16-control | 44 |
| stage-control | 50 | | vna-control | 110 |
| piezo-control | 37 | | scan-core | 265 |
| camera-control | 119 | | mission-control | 12 |
| zpiezo-control | 14 | | suite-common | 45 |
| kim-control | 87 | | mag2d-control | 46 |
| ppms-control | 43 | | mag2dcal-control | 96 |
| kepco-control | 50 | | windfreak-control | 58 |
| k2450-control | 65 | | gsp818-control | 60 |
| signalhound-control | 77 | | dsphase-control | 96 |
| dssg-control | 62 | | dsamp-control | 56 |
| agilis-control | 76 | | smaract-control | 55 |
| sr830-control | 92 | | sr7230-control | 95 |
| cs260-control | 60 | | ccs200-control | 56 |
| ddr25-control | 64 | | elliptec-control | 66 |
| chopper-control | 55 | | superk-control | 54 |
| tc200-control | 66 | | ls455-control | 71 |
| pm400-control | 77 | | hp8648-control | 54 |
| | | | **total** | **2536** |

Beyond unit tests, `python tools/check_modules.py --live` starts every module's
service on scratch ports and checks it against the module contract. The data
viewer's 42 tests live in its own repository.

`scan-core` is tested against a fake service that speaks the wire contract, so it
needs no instrument package installed.

## Hardware passes

Per module, on the lab PC: uncomment the
vendor dependency in `pyproject.toml`, `uv sync`, run the service with `--real`,
exercise it from the console before the GUI, and work through every `# VERIFY`.
Keep the simulation path working. The per-module checklists are in
[`docs/DEVELOPER_NOTES.md`](docs/DEVELOPER_NOTES.md) section 11.
After a pass, record it in [`docs/VERIFIED_INSTRUMENTS.md`](docs/VERIFIED_INSTRUMENTS.md):
what was checked, the commit, the caveats.

## Repository layout

```
docs/DEVELOPER_NOTES.md      architecture, wire contract, conventions, gotchas
docs/flyers/                 one-page flyers (PDF + PNG)
docs/video/                  demo videos (mp4)
INSTRUMENT_MODULE_GUIDE.md   the blueprint for building a new instrument module
<instrument>-control/        eight instrument modules, one uv project each (each with module.toml)
suite-common/                module discovery, shared by the launcher, scan-core and the tools
scan-core/                   the N-D scan engine, Scan Builder and measurement suite
                             (the data viewer comes from the aaltoview repo)
mission-control/             the launcher
installer/                   Setup.exe: the wizard, the component generator, the post-install step
front-panels/                reference renders of each GUI
spikes/                      earlier QCoDeS experiments, kept as reference
tools/                       new_module, check_modules, panel renderer, lab deploy script
suite_local.json             THIS PC's ports / real flags / remote services (not in git)
```

Each project's own README describes its verbs, configuration and hardware status.

## Note on virtual environments

`uv` and OneDrive fight over `.venv`, which shows up as
`Access is denied (os error 5)`. Use `dev.ps1` (in `clMag-control`, copyable
anywhere) to put the environment in `%LOCALAPPDATA%\uv-venvs\<project>` for that
shell only. Never set `UV_PROJECT_ENVIRONMENT` globally to a single path — every
project then shares and corrupts one environment.

## How to cite

If you publish scientific work with data measured or processed using AaltoFlow,
we would be grateful for a citation. The repository's [`CITATION.cff`](CITATION.cff)
has the details (GitHub's "Cite this repository" button gives it as BibTeX or
APA); in short:

> L. Flajšman, *AaltoFlow: lab automation built from independent instrument
> modules*, NanoSpin group, Aalto University, https://github.com/FlashLukas/AaltoFlow,
> doi:10.5281/zenodo.22959230

DOI: [10.5281/zenodo.22959230](https://doi.org/10.5281/zenodo.22959230) (always the latest version; Zenodo lists the DOI of each
release too). We are also happy to
hear what AaltoFlow was used for -- open an issue and tell us.

## Credits and license

AaltoFlow was developed by Lukáš Flajšman in the NanoSpin group of
Prof. Sebastiaan van Dijken, Aalto University. The copyright is held by Aalto
University.

MIT — see [LICENSE](LICENSE). One optional dependency is GPL-3.0: kim-control's
hardware driver uses [pylablib](https://github.com/AlexShkarin/pyLabLib), so
kim-control as distributed together with pylablib falls under the GPL-3.0; the
rest of the suite does not depend on it.
