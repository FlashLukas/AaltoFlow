# agilis-control

Control module for a **Newport Agilis** 2-axis stage — two slip-stick
(stick-slip) piezo actuators driven by one **AG-UC2** controller over USB —
built to the AaltoFlow instrument blueprint (`INSTRUMENT_MODULE_GUIDE.md`). It
is a *set-and-forget* module: you ask for N steps and the AG-UC2 makes them;
there is no control loop.

It uses **command port 5595** and **status port 5596** (declared in
`module.toml`; the launcher can override them per PC).

![agilis front panel](../../../front-panels/agilis.png)

*The simulator at its start state. Left: the position estimate, the XY step map
and the two drive waveforms (tooth height = step amplitude). Right: the step
amplitude per direction, the measured step size with the amplitude it was
measured at, the leash and the position list.*

## The one idea to understand: steps are counted, micrometres are measured

An Agilis actuator moves in discrete **steps**: the AG-UC2 sends a sawtooth,
the slow ramp drags the stage along by friction, the fast edge lets the piezo
snap back while the stage stays. There is no encoder, so the only position the
controller knows is its **step counter** (forward minus backward steps).

How far one step goes is set by the **step amplitude** (1–50, power-up default
16), and the manual is explicit that the relation is **not linear**, that too
small an amplitude does not move at all, and that **forward and backward steps
differ**. So this module:

- keeps a **measured step size per axis and per direction**, and records the
  amplitude it was measured at. Change the amplitude and status says
  `cal_valid: false` (the front panel turns the tag amber) until you measure
  again — the µm readings are then approximate, not silently wrong;
- books every counter change as forward or backward steps, so the position
  estimate is `forward steps × forward size − backward steps × backward size`.
  1000 steps out and 1000 back is counter 0, but not position 0.
- counts the steps made at an amplitude the step size was NOT measured at --
  after an amplitude change, or during a jog at speed 2 or 3, which always
  steps at the maximum amplitude 50. Those errors stay in the estimate, so
  status says `estimate_ok: false` (and `uncal_steps`) until the next Datum.

Default step size: 0.05 µm (the AG-LS25 datasheet's minimum incremental
motion) at amplitude 16. **Measure your own**: on the AG-LS25 the
**Measure step size** routine does it limit to limit, in both directions (on a
vertical mount up and down differ -- that is why there are two numbers); on a
stage without a limit switch move N steps, measure the distance, divide by N.

**The stage on the rig: Newport AG-LS25** (datasheet, manual A824E): 12 mm
travel, hard stops + an electrical limit switch, minimum incremental motion
0.05 µm, absolute accuracy (MA/PA) 100 µm, > 0.5 mm/s unloaded / > 0.2 mm/s at
1.7 N axial load, axial load capacity 2 N. "AG-LS25V6" is the 1e-6 Torr vacuum
version.

## What it does

- Drive 2 axes (X, Y) = the AG-UC2's axis 1 and 2 (swappable in Settings).
- Move in **steps** (`move_to_step`, `move_steps`) or **micrometres**
  (`move_to_um`, `move_relative_um`), each direction with its own step size.
- **Continuous jog** at one of the controller's four speeds (5 / 100 / 666 /
  1700 steps/s; 100 and 1700 at maximum amplitude). Hold the button: the jog
  is a **dead-man** — it ends by itself 1.5 s after the last jog command, so a
  crashed client cannot leave the stage driving into its end stop.
- **Step amplitude** per axis and direction, and a **Steps: Large / Small**
  preset (all to 50 / all to 16).
- Two zeros: **Datum** resets the controller's step counter (`ZP`); **Zero**
  sets a display origin for the relative read-out only.
- A **travel leash** (± steps around the datum) that replaces the travel limits
  when armed; the brain also **stops a jog** that reaches it.
- **Limit switch** (AG-LS25): indicator (PH), **move to a limit** (MV),
  **measure position** (MA) and **absolute move** (PA) -- the controller counts
  steps between the limits and cuts the USB link while it does (up to 2 min),
  so these run as numbered routines -- and **Measure step size** (the manual's
  MV-3 / ZP / PR100 / MV4 / TP procedure, both directions; ends at the
  negative limit with the datum there).
- A 20-slot **position list** in step coordinates, save/load JSON.
- **Fly-scan stream** of both positions for scan-core (`stream_*` verbs).
- **Reads, never changes, at start** (suite rule of 2026-09-27): the step
  counters and amplitudes are READ from the controller and adopted; the .ini
  amplitudes are applied only when you apply them (and only if they differ).
  The one write is `MR` (remote mode) -- the manual refuses TP/SU?/PH in local
  mode -- sent once the axes are at rest (a running move is waited for). The
  one exception is a safety interlock: a **jog** found running (left by a
  crashed session, so without its dead-man) is stopped after 2 s. `info` and
  status list what start-up wrote (`startup_writes`). On shutdown both axes
  stop and the push buttons are handed back (`ML`).
  `shutdown{keep_outputs?}` -- `keep_outputs: true` is a restart for a code update: motion is still stopped, nothing is moved back or parked, the next start adopts the position.

## Architecture (same as every module in the suite)

```
backends/  base.py (Protocol) · sim.py (default, no hardware) · ag_uc2.py (real, lazy pyserial)
agilis.py  the "brain": poll thread, step tallies -> um estimate, clamps, jog dead-man
net/       protocol.py · service.py · client.py · describe.py
apps/      theme.py · gui.py (MainWindow + StickSlipIndicator) · settings_dialog.py
scripts/   run_service.py · run_gui.py · agilis_console.py · smoke_test.py
```

The brain's **poll thread** reads the counters, axis states and limit switches
(20 Hz) and rebuilds the status snapshot; `status()` never touches the
hardware. Every serial exchange happens under one lock.

## Install and run

From `agilis-control\` in PowerShell (`dev.ps1` keeps the venv outside OneDrive):

```powershell
.\dev.ps1 sync --extra gui --extra real      # `real` = pyserial, for the AG-UC2
.\dev.ps1 run pytest -q
.\dev.ps1 run python scripts\smoke_test.py

.\dev.ps1 run python scripts\run_service.py                 # simulator
.\dev.ps1 run python scripts\run_service.py --real --com COM5   # the AG-UC2
.\dev.ps1 run python scripts\run_gui.py --connect localhost
.\dev.ps1 run python scripts\agilis_console.py
```

## Remote-control quick reference

JSON over REQ/REP on 5595; every reply is `{"ok": true, ...}` or
`{"ok": false, "error": ...}` and means *accepted*, not *done* — watch
`moving` in status. Axes are `"X"/"Y"` or `0/1`.

| intent | command |
|---|---|
| read all state | `{"cmd":"status"}` → `position_steps`, `position_um`, `target_um`, `moving`, `amplitude_fwd`, `cal_valid`, `estimate_ok`, … |
| absolute step target | `{"cmd":"move_to_step","axis":"X","position":5000}` |
| step by N from here | `{"cmd":"move_steps","axis":"X","delta":200}` |
| absolute µm (estimate) | `{"cmd":"move_to_um","axis":"X","position":120.0}` |
| relative µm | `{"cmd":"move_relative_um","axis":"X","delta":10.0}` |
| continuous jog (repeat to keep alive) | `{"cmd":"jog","axis":"Y","speed":-1}` (−4…4, 0 = stop) |
| step amplitude | `{"cmd":"set_amplitude","axis":"X","value":30,"direction":-1}` (0 = both) |
| amplitude preset | `{"cmd":"set_step_size","large":true}` |
| store a measured step size | `{"cmd":"set_calibration","axis":"X","value":0.048,"direction":1}` |
| leash | `{"cmd":"set_leash","enabled":true,"leash_steps":20000}` |
| datum / display zero | `{"cmd":"zero_counter","axis":"X"}` · `set_zero` · `clear_zero` |
| to a limit switch (MV) | `{"cmd":"move_to_limit","axis":"X","direction":-1,"speed":3}` |
| measure position (MA, routine) | `{"cmd":"measure_position","axis":"X"}` → `routine_id`; result in `measured_um` |
| absolute move (PA, routine) | `{"cmd":"move_absolute","axis":"X","position":6000}` (µm from the − limit) |
| measure step size (routine) | `{"cmd":"measure_step_size","axis":"Y"}` → `routine_id`; wait for `routine_running` false, `routine_error` "OK" |
| stop | `{"cmd":"stop"}` (all) or `{"axis":"Y"}` |
| position list | `store_position` / `goto_position` / `save_positions` / `load_positions` |
| fly-scan stream | `stream_start` / `stream_read` / `stream_stop` → `{t, values: {x, y}}` |

`describe` offers `position_x/y` (µm, settle `adopt_then_flag` on `target_um`),
the four amplitudes, the Large/Small preset, step-size and limit indicators, and
the actions `datum_x`, `datum_y`, `stop`, and (limit-switch stages)
`measure_step_size_x/y` and `measure_position_x/y` with a numbered-routine wait
block -- all usable in scan routines.

## Hardware notes (before the first real run)

`backends/ag_uc2.py` is the **only** file that talks to the controller
(pyserial, imported inside `open()`), written from the Newport Agilis manual
A824E. Every command is marked `# VERIFY`. On the lab PC:

1. Install Newport's AG-UC2 software (it brings the USB driver), find the COM
   port in Device Manager, set `hardware.port` (Settings) or pass `--com`.
2. Check the reply formats of `TP`, `TS`, `TE`, `PH`, `SU+?` in a terminal
   (921600 baud, CR LF) — the driver parses the last integer in each line.
3. Time a 2000-step `PR` to learn the stepping rate (the simulator assumes 666
   steps/s) and check `TS` reports 1 right after `PR`.
4. Measure the step size of each axis and direction at the amplitude you will
   use: the LIMIT SWITCH card's **Measure step size** (AG-LS25), or by hand and
   store it (STEP SIZE card or `set_calibration`).
5. AG-LS25: check the reply of `1MA` / `1PA500` (format, which limit MA counts
   from), that the controller answers once the USB link is back, and the
   switch-to-switch distance (`hardware.travel_um`, nominal 12000).
