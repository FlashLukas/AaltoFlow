# mag2d-control -- 2D vector magnet

A two-axis electromagnet on an NI DAQ: set the field as **magnitude + angle** or
as **Bx, By**, and a PI loop per axis holds it in **calibrated millitesla**, all
the time the output is on. Cooling water is interlocked. Successor of the
LabVIEW program *RotSampleInVNA*.

![mag2d front panel](../../../front-panels/mag2d.png)

*Simulated magnet: 80 mT along +X, then 150 mT at 45 deg. The dial shows the
setpoint (dim), the measured field (amber) and the tolerance ring (green =
stable); the chart shows both axes slewing at 2 V/s and settling.*

Ports **5575** (commands) / **5576** (status). Everything runs on a **simulator**
by default; the NI backend is written but has **not run on the magnet yet**.

## Run it

```powershell
cd modules\field\mag2d-control
uv sync --extra gui
uv run pytest -q                                  # 51 tests, ~15 s
uv run scripts/smoke_test.py                      # the sim magnet end to end, in simulated time

uv run scripts/run_service.py                     # the service (simulated magnet)
uv run scripts/run_gui.py --connect localhost     # a GUI on that service
uv run scripts/mag2d_console.py field 150 45      # or poke it by hand
```

`run_gui.py` WITHOUT `--connect` runs its own private simulated magnet, not the
service's.

### At start: the magnet is ADOPTED, not reset

The service READS the magnet and changes nothing (Lukas's rule, 2026-09-27):
enable line, drive voltages (read back through the card's internal
`_aoN_vs_aognd` channels where it has them), Hall probes, temperatures, water.
A magnet found **off** stays off (switch it on with `set_output` / Energize); a
magnet found **energized** -- e.g. after a crashed run -- is held where it is:
the setpoint becomes the field measured at that moment and the PI starts
bumpless from the drive on the wire. The output is never switched on or off by
the service itself at start.

### The water interlock at start

If the cooling water is off (and not bypassed) the service prints why and exits
with **code 3**, before it opens a socket. Start the water, or -- knowing the
coils are then unprotected -- run with `--bypass-water`.

### Stopping

Ctrl+C, the launcher's Stop, or `mag2d_console.py shutdown` all **ramp the output
to 0 V at the slew rate** and release the enable line. A hard kill cannot: the
DAQ keeps its last output. Do not taskkill a running magnet.
`shutdown{keep_outputs: true}` is a RESTART for a code update: no ramp, the
coils keep their drive (open loop, nothing watches the water) until the next
start adopts it.

## Commands (REQ/REP, JSON; fire-and-forget)

| verb | args | |
|---|---|---|
| `set_field` | `field_mT`, `angle_deg` (optional) | signed magnitude; no angle = keep it |
| `set_angle` | `angle_deg` | rotate, keep the magnitude |
| `set_vector` | `bx_mT`, `by_mT` | |
| `set_bx` / `set_by` | `bx_mT` / `by_mT` | set one component, keep the other |
| `zero` | | field 0 mT, angle kept (safety verb) |
| `set_output` | `enabled` | on = regulate; off = ramp to 0 V, then disable |
| `output_off` | | = `set_output` off (safety verb) |
| `set_water_bypass` | `enabled` | DANGER |
| `clear_fault` | | refused while the cause is still there |

plus `status`, `info`, `get_config`, `set_config`, `describe`, `shutdown{keep_outputs?}`.
A refused command (a setpoint during a FAULT) replies `{"ok": false, "error": "..."}`.

Control (one controller, many viewers -- docs/DEVELOPER_NOTES.md section 4):
the first GUI gets control, later ones are viewers; a viewer (or a script
without control) may still send the safety verbs `zero` and `output_off`.
`kind: machine` clients (scan-core) bypass the lock.

A setpoint is stored **exactly as sent**; `field_stable` goes False in the same
instant, and True again once every axis has been within `control.tolerance_mT`
for `control.stable_time_s`. Wait for both "my setpoint is in the status" and
`field_stable` (the client's `set_field_blocking` does).

## Sweep (fly scans)

The field magnitude and the field **angle** can be swept continuously at a set
pace, so scan-core's fly axis can fly them: the magnet sweeps over each row,
the detectors stream, and every sample is binned by the **measured** field.
An angle sweep is the angular FMR scan as one continuous rotation.

| verb | args | |
|---|---|---|
| `ramp_field` | `field_mT`, `rate_mT_per_s` | sweep the signed magnitude, angle kept; reply `{"ramp_id": n}` |
| `ramp_angle` | `angle_deg`, `rate_deg_per_s` | rotate, magnitude kept; the angle is never wrapped |
| `ramp_stop` | | end it where it is (safety verb: a viewer may send it) |
| `stream_start` / `stream_read` / `stream_stop` | | every Hall reading with its time: `field` (along the setpoint direction), `angle` (measured direction), `bx`, `by`, the setpoint |

How it works: the control loop moves the **setpoint** along a straight line in
time (computed from the elapsed time, so a late tick does not slow the sweep)
and the continuous PI makes the field follow; at the target the ordinary
`field_stable` takes over. The status shows `ramping`, `ramp_id` (the newest
sweep started), `ramp_knob` (`field` / `angle`), `ramp_target`, `ramp_rate`;
a sweep is over when `ramp_id` is yours and `ramping` is false. Any ordinary
set (`set_field`, `set_angle`, `set_vector`, `zero`, output off) stops a
running sweep first; a new sweep replaces a running one from where it got to.
Targets and rates are clamped to `limits` with a warning
(`field_rate_*_mT_per_s`, `angle_rate_*_deg_per_s`; the maxima are what the
sim's PI follows -- VERIFY on the magnet). A FAULT (water lost, temperature,
a failed read) ends the sweep and zeroes the setpoint as always; during a
FAULT a sweep is refused.

The measured angle is reported in the setpoint's own convention: a negative
field reads the direction of -B, the angle is unwrapped to the setpoint's turn
(350 deg reads 350, not -10), and below `limits.angle_min_field_mT` (1 mT) --
where the direction is only probe noise -- it shows the setpoint angle.

The GUI's **Sweep** card sweeps to the magnitude / angle typed above at the
rate beside each button; **Stop** ends it.

## Files

```
src/mag2d/
  config.py            every tunable number (+ .ini save/load)
  pid.py               one axis: feed-forward + PI, clamp, slew, anti-windup
  controller.py        the brain: setpoints, states, interlocks, sweeps, the loop thread
  stream.py            the record of every Hall reading, for fly scans
  sim_system.py        a controller on the simulated magnet
  backends/base.py     the hardware Protocol (volts in, volts out)
  backends/sim.py      the simulated magnet (+ FakeClock for tests)
  backends/nidaq.py    the NI DAQ (nidaqmx, lazy import) -- untested on hardware
  net/                 protocol, service, client, describe
  apps/                GUI (VectorDial), settings dialog, theme
scripts/               run_service, run_gui, mag2d_console (pyzmq only), smoke_test
```

## Hardware pass (not done yet)

1. Install NI-DAQmx, uncomment `nidaqmx` in `pyproject.toml`, `uv sync`.
2. Check the channel names in NI MAX against `config.Hardware`.
3. Start with a low `limits.field_max_mT`. Check the sign first: set a small field
   along +X and watch that `measured_bx_mT` moves the same way (if it runs away,
   `output` off at once and flip `hardware.ao_sign_x`; same for Y). Once it
   regulates, read `output_V` and the measured field from `status`: mT per volt
   = B / V at a steady point; put that in `control.ff_mT_per_V`.
4. Retune `kp`/`ki` (they are sim-tuned). Check the temperature scale (the VI's
   10e3 C/V is suspicious).
5. Every `# VERIFY` in `backends/nidaq.py`.

## Troubleshooting

**"Access is denied (os error 5)" from uv** in a OneDrive folder: keep the venv
off OneDrive -- copy `dev.ps1` usage from clMag-control (`.\dev.ps1 sync --extra gui`).
