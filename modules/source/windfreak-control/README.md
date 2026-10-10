# windfreak-control

Control for a **Windfreak Technologies SynthHD PRO v2**, a two-channel RF
synthesizer (10 MHz - 24 GHz, up to +20 dBm): switch each output (RFoutA,
RFoutB) on and off, set its **frequency**, **power** and **phase**, and pick the
**reference** both PLLs lock to -- over the instrument's USB virtual COM port,
or fully simulated with no hardware. Frequency, power and phase of either
channel can also be **swept** continuously at a set pace, for fly scans
(section "Sweeps").

![windfreak front panel](../../../front-panels/windfreak.png)

*The start state of the simulator: the pretend SynthHD was left with A radiating
2.45 GHz and B off, and the service ADOPTED that (it reads the instrument at
start and changes nothing). With RF on, the
Outputs pane draws each channel as a wave (more cycles = higher frequency,
taller = more power) and the dial on the right shows the two phases; with both
channels at the same frequency it reads B - A directly (a quadrature pair is a
90 deg dial). An unlocked PLL draws a ragged red trace.*

Light theme: [`windfreak-light.png`](../../../front-panels/windfreak-light.png).

It is an AaltoFlow module like every other: a thin backend behind a `Protocol`
interface, a small brain, a ZeroMQ service + client on the suite's wire
contract, a `describe` manifest so scan-core can sweep it without knowing
anything about it, and a Qt front panel.

## Ports

Commands 5583 (REQ/REP), status 5584 (PUB) -- declared in `module.toml`, and
overridable per PC in the launcher.

## Layout

```
windfreak-control/
  module.toml            how the suite finds this module
  src/windfreak/
    config.py            Channel (x2) / Reference / Limits / Hardware / UI + .ini save/load
    synthesizer.py       the brain: desired state, clamps, ONE worker thread, status snapshot, sweeps
    softramp.py          the suite's software ramp (byte-identical copy from suite-common)
    sim_system.py        build_sim_system(): brain + simulated SynthHD
    backends/base.py     the DualSynth interface (a typing.Protocol)
    backends/sim.py      a simulated SynthHD PRO v2 (grid, lock, leveling, temperature)
    backends/synthhd.py  the real one over USB serial -- the ONLY file importing pyserial
    net/protocol.py      ports, topics, config <-> dict
    net/service.py       WindfreakService (REP + PUB threads)
    net/client.py        WindfreakClient (same methods as the brain)
    net/describe.py      the parameter manifest
    apps/gui.py          front panel + the DualToneIndicator
    apps/settings_dialog.py, apps/theme.py
  scripts/  run_service.py  run_gui.py  windfreak_console.py  smoke_test.py
  tests/    config, synthesizer, backend command strings, net, describe, GUI smoke, theme
```

## Commands used (real backend)

Windfreak's own one-character protocol (API guide v1.0b), **no line
terminator** on what we send, `\n` on every reply:

| what | command | notes |
|---|---|---|
| select channel | `C0` / `C1` | A / B; re-sent before every per-channel command and query |
| channel spacing | `i<Hz>` | sent only when you change `hardware.channel_spacing_Hz`, never at start |
| frequency | `f<MHz>`, `f?` | 0.1 Hz steps, snapped to the channel spacing |
| power | `W<dBm>` | the unit levels it; `V` = 1 if it managed |
| phase | `~<deg>` | a phase **step** (relative); the backend turns absolute into steps |
| output on | `E1r1h1` | PLL on, amplifier on, unmuted |
| output off | `h0r0` (or `h0r0E0`) | muted + amplifier off (+ PLL off in the "quiet" mode) |
| lock | `p` | 1 = locked |
| read at start | `f?` `W?` `h?` `r?` `E?` per channel, `x?` `*?` | queries only -- nothing is written at start |
| reference | `x0/x1/x2`, `*<MHz>` | external / internal 27 MHz / internal 10 MHz |
| temperature | `z` | degC |

Every call not yet confirmed on the v2 hardware is marked `# VERIFY` in
`backends/synthhd.py`.

## Setup

```powershell
cd modules\source\windfreak-control
.\dev.ps1 sync --extra gui                 # simulator + GUI
.\dev.ps1 sync --extra gui --extra real    # + pyserial for the instrument
```

`dev.ps1` keeps the virtual environment in `%LOCALAPPDATA%\uv-venvs\windfreak-control`,
outside OneDrive (which locks files inside a `.venv` and makes `uv` fail with
"Access is denied"). Name every extra you want: `uv sync` removes the ones you
leave out.

## Run

```powershell
.\dev.ps1 run scripts/run_service.py                  # simulated
.\dev.ps1 run scripts/run_service.py --real --port COM4
.\dev.ps1 run scripts/run_gui.py --connect localhost  # the panel, on the service
.\dev.ps1 run scripts/run_gui.py                      # a private local simulation
.\dev.ps1 run scripts/windfreak_console.py            # raw-protocol console
    wf> freq a 2.5 GHz
    wf> freq b 2.5 GHz
    wf> phase b 90
    wf> rf a on
    wf> rf b on
    wf> status
    wf> alloff
```

**Start is read-only** (rule of 2026-09-27): the service READS what the
SynthHD is doing -- RF on/off, frequency, power, PLL, reference -- and shows
exactly that; a running output keeps running. The config's channel values are
not pushed; they are overwritten with what was read, and a value goes to the
instrument only when someone sets it. (The phase has no readback: "0 deg" is
the phase at service start.) On `shutdown`, Ctrl-C and when the GUI of a local
simulation closes, both outputs are switched **off**. Verb
`shutdown{keep_outputs?}`: with `keep_outputs: true` (a restart for a code
update) the service closes but leaves both outputs as they are, and the next
start adopts them.

## Sweeps (fly scans over frequency, power or phase)

A fly scan (scan-core, `type: fly` axis) records the detectors while a knob
moves CONTINUOUSLY and bins every sample by the value the knob had at that
moment. The SynthHD jumps to the value it is told, so the **service walks the
knob** in small steps (`softramp.py`, the suite's software ramp, copied byte
for byte from suite-common): one `f` / `W` / `~` write every
`hardware.ramp_dt_s` (20 ms), each value computed from the elapsed time, so a
late step does not slow the sweep down.

Each channel's knob has its own sweep, named like its describe control:
`a_frequency`, `a_power`, `a_phase`, `b_frequency`, `b_power`, `b_phase`. The
channel is an argument of the ramp verbs, exactly as of `set_frequency`.

| verb | arguments | pace limits (config `[limits]`) |
|---|---|---|
| `ramp_frequency` | `channel`, `frequency_Hz`, `rate_Hz_per_s` | `ramp_rate_min/max_Hz_per_s` (1 kHz/s .. 10 GHz/s) |
| `ramp_power` | `channel`, `power_dBm`, `rate_dB_per_s` | `ramp_rate_min/max_dB_per_s` (0.01 .. 100 dB/s) |
| `ramp_phase` | `channel`, `phase_deg`, `rate_deg_per_s` | `ramp_rate_min/max_deg_per_s` (0.01 .. 3600 deg/s) |
| `ramp_stop` | `knob` (optional, e.g. `a_frequency`; none = every sweep) | a stop: a viewer may send it |

- The reply carries the sweep's number (`ramp_id`); status shows
  `<sweep>_ramping`, `<sweep>_ramp_id`, the target and the pace
  (`a_frequency_ramp_target_Hz`, `b_power_ramp_rate_dB_per_s`, ...) and
  `ramping` (any knob of either channel). The sweep is over when
  `<sweep>_ramp_id` is yours and `<sweep>_ramping` is false. While a knob
  sweeps, its status value (`a_frequency_Hz` ...) is the live one.
- A target or pace outside the limits is clamped, with a warning; a zero,
  negative or non-finite pace is refused.
- An ordinary `set_frequency` / `set_power` / `set_phase` takes that channel's
  knob over (stops its sweep); a set of another knob or of the other channel
  does not. Settings sent back (OK in the Settings dialog) while a knob sweeps
  do not jump it back: its stale value is ignored. Shutdown stops every sweep
  before it switches the outputs off.
- **The RF output is never switched by a sweep.**
- The record: the stream verbs (`stream_start` / `stream_read` /
  `stream_stop`, group `ramp`) hand out every value each sweep SENT, one
  channel per sweep, each with its own time stamps in `t_ch`. describe
  declares a `ramp` block on each of the six controls with
  `readback.measured: false` -- **binned by command**. Why not read back: the
  SynthHD has no measurement of its output to read -- `f?` / `W?` return the
  setting (the frequency snapped to the channel grid, the requested power),
  and there is no phase readback at all -- so a query would only echo the
  number just sent, and waiting for its reply line on the serial port roughly
  halves the step rate. The PLL settles in ~100 us (datasheet), far inside
  one step; `a_locked` / `b_locked` still show if it ever does not keep up.
- A sweep step writes to the serial port from its own thread; every backend
  call (the worker's polls and the steps) goes through one hardware lock, so
  bytes never interleave on the line.
- **VERIFY on the unit:** how fast the SynthHD takes back-to-back writes (it
  re-levels the power after every frequency / power write) -- that bounds
  `ramp_dt_s`; how long one query takes; whether a large power sweep glitches
  the output at internal switch points; the instrument's phase resolution
  (a long phase sweep is many small relative `~` steps).

The GUI's **Sweep** card does the same by hand: pick the channel and the knob,
set the pace, "Sweep to" walks it to the value in that channel's box, "Stop"
ends every sweep.

## What a scan sees (`describe`)

Per channel, flat ids: `a_rf_on`, `a_frequency` (MHz), `a_power` (dBm),
`a_phase` (deg) as controls; `a_locked`, `a_leveled`, `a_frequency_actual` as
indicators -- and the same with `b_`. Plus `reference` (enum),
`ext_ref` (a control only while the external reference is selected),
`ref_settled`, `rf_all_off`, `ramping`, `temperature`, and the action `all_rf_off`
(usable in a scan routine). Frequency, power and phase of each channel carry
a `ramp` block (section "Sweeps"), so a fly scan can fly them.

**Settle rule:** every channel control is `adopt_then_flag`: wait until the
service echoes the value it pushed (`a_frequency_Hz`), then until
`a_settled` -- that request is the latest one sent AND, while the output is
on, the PLL reports lock. So a scan never records a point on a synthesizer
that has not been programmed yet or is not locked; with the external
reference missing it times out with a message instead of measuring. Switching
an output OFF settles as soon as it is done, lock or not (the safe action
never hangs). The action `all_rf_off` waits for the status flag `rf_all_off`,
i.e. until both outputs really are off in the instrument.

## Tests

```powershell
.\dev.ps1 run pytest -q            # 116 tests, offline, ports 17020-17039
.\dev.ps1 run python scripts/smoke_test.py
python ..\..\..\tools\check_modules.py windfreak --live
```

## Using it from Python

```python
from windfreak.net.client import WindfreakClient
wf = WindfreakClient("localhost")
wf.set_frequency("a", 2.5e9); wf.set_frequency("b", 2.5e9)
wf.set_phase("b", 90.0)
wf.set_rf("a", True); wf.set_rf("b", True)
st = wf.status()          # {"a_locked": True, "b_phase_deg": 90.0, ...}
```

A reply means *accepted*, not done: poll `status()` until the echo matches and
`a_settled` is true (scan-core does exactly that from the manifest).
