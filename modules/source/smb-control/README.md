# smb-control

Control for a **Rohde & Schwarz SMB100A** RF signal generator: turn the RF
output on/off and set the output **power**, **frequency**, and **phase** — over
GPIB (SCPI), or fully simulated with no hardware.

![smb front panel](../../../front-panels/smb.png)

*RF on at 2.45 GHz / -3 dBm. The antenna indicator radiates while the output is live, and its brightness tracks the power.*

It is a sibling to `clMag-control` (the Kepco magnet controller) and copies its
shape on purpose: a thin hardware backend behind a `Protocol` interface, a
dataclass `Config` with plain-text save/load, and a ZeroMQ service + client so a
GUI, a console, or a coordinator can drive it over localhost or the lab network.

The one big difference: the SMB100A is a **set-and-forget** instrument. There is
no control loop, so there is no PID, no calibration, and no state machine
here — the `Generator` just holds the desired signal, clamps it to the safety
limits, and pushes it to the box. The one thing it does over time is a
**sweep** (below): walking frequency, power or phase at a set pace, for fly
scans.

## Ports (why they differ from the magnet)

| service        | commands (REP) | status (PUB) |
|----------------|----------------|--------------|
| magnet (clMag)  | 5555           | 5556         |
| **RF (smb)**   | **5557**       | **5558**     |

Different defaults mean **both services run on the same PC at once**, which is
exactly what the planned coordinator needs (“set field → wait stable → set RF →
measure”).

## Layout

```
src/smb/
  config.py              all tunable numbers (Signal / Limits / Hardware) + .ini save/load
  backends/
    base.py              RFSource Protocol — the interface everything depends on
    sim.py               SimulatedSMB100A — fake generator, runs offline
    visa_scpi.py         VisaSMB100A — the real box over GPIB (SCPI, lazy pyvisa import)
  generator.py           Generator — holds desired signal, clamps to limits, drives a backend
  sim_system.py          build_sim_system(cfg) — wire the simulator into a Generator
  net/
    protocol.py          wire shapes + default ports (5557/5558)
    service.py           SmbService — owns the generator, serves it over ZeroMQ
    client.py            SmbClient — Generator-compatible facade over the socket
scripts/
  run_service.py         start the service (simulated by default, --real for GPIB)
  rf_console.py          standalone raw-protocol console (only needs pyzmq)
  smoke_test.py          quick offline check (imports + clamp behaviour)
tests/                   pytest: config, generator, and a full network round-trip
```

## SCPI commands used (real backend)

| function   | set                       | query        |
|------------|---------------------------|--------------|
| RF output  | `OUTP:STAT ON` / `OFF`    | `OUTP:STAT?` |
| power      | `POW <dBm>`               | `POW?`       |
| frequency  | `FREQ <Hz>`               | `FREQ?`      |
| phase      | `PHAS <deg> DEG`          | `PHAS?`      |

On connect the backend sends `*CLS`, `UNIT:ANGL DEG` (so phase is always in
degrees), and `OUTP:STAT OFF`. The default GPIB address is `GPIB0::28::INSTR`
(the SMB100A’s factory address) — change it in the `.ini` or with `--visa`.

Verbs: `set_rf`, `rf_off`, `set_power`, `set_frequency`, `set_phase`, plus the
universal `status`, `info`, `get_config`, `set_config`, `describe`,
`shutdown{keep_outputs?}` — a plain shutdown switches the RF off; with
`keep_outputs: true` (a restart for a code update) the RF is left as it is and
the next start adopts it. The sweeps add `ramp_frequency`, `ramp_power`,
`ramp_phase`, `ramp_stop{knob?}` and `stream_start` / `stream_read` /
`stream_stop` (section "Sweeps" below).

## Setup

Uses [uv](https://docs.astral.sh/uv/). From this folder:

```powershell
uv sync                     # core (pyzmq)
uv sync --extra gui         # later, if/when a GUI is added
```

### ⚠ OneDrive + venv gotcha (same as clMag-control)

This project lives under OneDrive, which locks files in `.venv` as it syncs and
makes `uv` fail with *“Access is denied (os error 5)”*. Put the virtual
environment **off** OneDrive, once per machine:

```powershell
[Environment]::SetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', "$env:LOCALAPPDATA\uv-venvs\smb-control", 'User')
$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\uv-venvs\smb-control"
uv sync
```

## Run

Simulated (no hardware):

```powershell
uv run scripts/smoke_test.py                 # instant offline sanity check
uv run scripts/run_service.py                # start the service (simulated)
```

Then, in another terminal, drive it:

```powershell
uv run scripts/rf_console.py                 # interactive
    rf> freq 1.5 GHz
    rf> power -10
    rf> rf on
    rf> status
    rf> watch 5
    rf> quit

uv run scripts/rf_console.py freq 1e9        # one-shot, then exit
uv run scripts/rf_console.py rf on
```

Real hardware (lab PC): uncomment `pyvisa` in `pyproject.toml`, `uv sync`, then:

```powershell
uv run scripts/run_service.py --real                       # uses GPIB0::28::INSTR
uv run scripts/run_service.py --real --visa GPIB0::28::INSTR
```

Point a client (the console or the GUI) at another machine with `--connect <host>`.

## Sweeps (fly scans over frequency, power or phase)

A fly scan (scan-core, `type: fly` axis) records the detectors while a knob
moves CONTINUOUSLY and bins every sample by the value the knob had at that
moment. The SMB100A jumps to the value it is told, so the **service walks the
knob** in small steps (`softramp.py`, the suite's software ramp, copied byte
for byte from suite-common): one `FREQ` / `POW` / `PHAS` every
`hardware.ramp_dt_s` (50 ms), each value computed from the elapsed time, so a
late step does not slow the sweep down.

| verb | arguments | pace limits (config `[limits]`) |
|---|---|---|
| `ramp_frequency` | `frequency_Hz`, `rate_Hz_per_s` | `ramp_rate_min/max_Hz_per_s` (1 kHz/s .. 10 GHz/s) |
| `ramp_power` | `power_dBm`, `rate_dB_per_s` | `ramp_rate_min/max_dB_per_s` (0.01 .. 100 dB/s) |
| `ramp_phase` | `phase_deg`, `rate_deg_per_s` | `ramp_rate_min/max_deg_per_s` (0.01 .. 3600 deg/s) |
| `ramp_stop` | `knob` (optional; none = every sweep) | a stop: a viewer may send it |

- The reply carries the sweep's number (`ramp_id`); status shows
  `<knob>_ramping`, `<knob>_ramp_id`, the target and the pace
  (`frequency_ramp_target_Hz`, `power_ramp_rate_dB_per_s`, ...) and `ramping`
  (any knob). The sweep is over when `<knob>_ramp_id` is yours and
  `<knob>_ramping` is false.
- A target or pace outside the limits is clamped, with a warning.
- An ordinary `set_frequency` / `set_power` / `set_phase` takes that knob over
  (stops its sweep); a set of another knob does not.
- **The RF output is never switched by a sweep.**
- The record: the stream verbs hand out every value each sweep SENT, one
  channel per knob (`frequency`, `power`, `phase`, each with its own time
  stamps in `t_ch`). describe declares a `ramp` block on each knob with
  `readback.measured: false` -- **binned by command**. Why not read back:
  `FREQ?` / `POW?` / `PHAS?` return the setting the box holds, not a
  measurement, so they would only echo the number just sent and cost a GPIB
  round trip per step. Sweep steps skip the backend's 50 ms settle pause
  (`hardware.settle_s`), which exists for a read-back right after a set.
- **VERIFY on the unit:** how long one write takes over GPIB and the SMB100A's
  setting time (together they bound `ramp_dt_s`); which step attenuator the
  unit has -- a mechanical one clicks at every switch point of a large power
  sweep.

The GUI's **Sweep** card does the same by hand: pick the knob, set the pace,
"Sweep to" walks it to the value in that knob's box, "Stop" ends it.

## GUI

A dark, amber-on-dark window in the same style as the magnet GUI, with a
transmitter-tower indicator that **radiates animated waves whenever the RF is
on** (brighter the higher the power). Install the optional GUI extra and run it:

```powershell
uv sync --extra gui
uv run scripts/run_gui.py                     # local simulator
uv run scripts/run_gui.py --connect <host>    # drive a running service
```

The window sends `set_rf` / `set_power` / `set_frequency` / `set_phase` and reads
a status snapshot on a timer — driving a local `Generator` or a remote `SmbClient`
identically. Files live under `src/smb/apps/` (`theme.py`, `gui.py`,
`settings_dialog.py`); the antenna glyph is `AntennaIndicator` in `gui.py`.

## Tests

```powershell
uv run pytest            # or: uv run pytest -v
```

Covers the config round-trip (including the `rf_on` bool), the generator’s
set/read and safety clamping, a full service⇄client round-trip over ZeroMQ, and
the sweeps (`tests/test_sweep.py`: each knob to its end at the pace, stop, a
set taking over, RF untouched, the record, the verbs on ports 18102/18103).

## Using it from Python (e.g. a coordinator)

```python
from smb.net.client import SmbClient

rf = SmbClient(host="localhost")     # or the lab PC's address
rf.start()
rf.set_frequency(1.5e9)
rf.set_power(-10.0)
rf.set_phase(0.0)
rf.set_rf(True)
print(rf.status().rf_on)             # True
rf.shutdown()
```

`SmbClient` mirrors the `Generator` API, so the same code drives an in-process
generator or a remote one — only the address changes.
