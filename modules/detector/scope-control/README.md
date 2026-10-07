# scope-control -- Oscilloscope (Siglent SDS1000CML+ / RS PRO RSDS1102CML+)

A two-channel oscilloscope as an AaltoFlow detector: averaged, triggered
traces of both channels in physical units, with per-channel numbers, so a scan
can record a waveform (or two, and their XY relation) at every point --
against position, temperature, a drive amplitude, anything else the suite
sweeps.

It is a SCOPE, deliberately nothing more: what the recorded signals mean for
an experiment (a hysteresis loop and its coercive field, a resonance, ...)
is analysed in the AaltoView processing module, from the traces this module
records (Lukas, 2026-10-07).

![front panel](../../../front-panels/scope.png)

**First contact with the instrument on 2026-10-06** (lab PC, firmware
6.01.01.25): the replies and three bugs it showed are written up at the top of
`backends/siglent.py` and fixed (a 0x0A byte inside the binary waveform cut the
read and put every later reply out of step; SI prefixes such as "500.0KSa" and
"0.00us" were dropped). Not yet re-run on the instrument after the fix; the
other unconfirmed commands are `# VERIFY`, and
[First run on the instrument](#first-run-on-the-instrument) is the checklist.
The Analog Discovery 3 backend (with its two generator outputs) comes later;
the module is generic over the backend's capabilities.

Ports 5633 / 5634.

## What it does with every trace

1. The scope triggers; the module notices the new record (it never changes
   the scope's run mode) and reads both channels.
2. Volts -> the channel's **physical quantity** (`quantity = scale x V +
   offset`, e.g. 10 A/V for a current probe that gives 0.1 V/A), recorded
   with the data.
3. The record is reduced to `points` samples (neighbours averaged).
4. **Running average** of the last N traces ("312 / 500"; Restart average
   empties it). A change of any setting that shapes a trace empties it too.
5. **Zero-phase filter** (low/high-pass, Butterworth magnitude squared) --
   identical on every channel, so the channels stay time-aligned with each
   other (an ordinary filter would shift one against the other).
6. Numbers: per channel mean, rms, peak-to-peak, amplitude, frequency; and
   the phase of CH2 against CH1 (`src/scope/analysis.py`).

## The window

Two tabs, each the full height: **X(t), Y(t)** (the channels against time,
CH2 on its own axis) and **XY / YX** (the scope's XY mode: CH2 against
CH1; "YX" swaps them). A cursor readout under each plot, in that plot's axis units. The
columns sit in a splitter -- drag the borders. Tab, XY/YX and the column
widths are remembered on this PC (QSettings).

The module's own settings -- the QUANTITY of each channel (label, unit, scale
per volt, value at 0 V: e.g. a probe's calibration), averaging, points,
filter -- are saved to `scope.ini` next to the project at every change
(atomically), so a restart keeps them. The scope's own settings are read from
the scope at start, as always.

![XY tab](../../../front-panels/scope-xy.png)

## Scan use: `acquire`

`acquire` restarts the average and completes when N FRESH triggered traces
are in -- the first trace after the trigger is skipped, because it may have
been recorded before the thing the scan just changed. 500 averages at 30 Hz
are ~17 s per point; the scan's wait and its timeout know that (the timeout
grows with the averages AND the record length). One acquisition feeds every
detector: the two traces (arrays with their own `time` dimension), the
per-channel numbers and the phase. A trace that hit the screen edge is reported as CLIPPED.

Refused, with the reason, when the scope cannot deliver: trigger mode STOP,
SINGLE with more than one trace to average, or AUTO at a slow time base
(>= `hardware.roll_tdiv_s`, 50 ms/div): there the scope free-runs / rolls and
makes no triggered records (lab PC: one in 120 s). In NORMAL mode triggered
records come at any time/div, one per record length (measured: ~8 s at
0.1 - 0.5 s/div) -- an acquisition there just takes long, and its timeout
knows it. The scope snaps some time/div values (the lab's: 20 ms -> 10 ms,
200 ms -> 100 ms); a warn event says when it did.

The trace length, the time base and the units must not change during a scan
(scan-core refuses ragged data); sweep them only as an outer axis, if at all.

## Rules it keeps

- **Start changes nothing.** The scope's settings (V/div, offset, coupling,
  probe, time/div, delay, trigger) are READ and shown; they are written only
  when someone changes one. The scope snaps V/div and time/div to its steps:
  status shows what was asked (`*_set`) next to what it holds.
- A change at the scope's front panel is noticed (settings re-read every
  3 s), shown in the log and adopted.
- The one write the module makes on its own is `WFSU` -- how the next
  waveform TRANSFER is thinned (a 1.4 Mpts record would take seconds to move).
  It changes nothing about the acquisition or the screen.
- Safety verbs `abort` / `stop` (cancel an acquisition) work for a viewer too.
  A scope drives nothing; the AD3 backend will add `generator_off`.
- `shutdown{keep_outputs: true}` (Mission Control's Restart) closes exactly as
  a plain stop does -- a scope's shutdown writes nothing either way -- and
  replies `kept_outputs`.

## Verbs

Scope settings: `set_channel_enabled`, `set_vdiv`, `set_offset`,
`set_coupling`, `set_probe` (each `{channel, ...}`), `set_tdiv`, `set_delay`,
`set_trigger_source|level|slope|mode`. Module: `set_points`, `set_averages`,
`set_keep_raw`, `set_filter {lowpass_Hz?, highpass_Hz?, order?}`,
`set_physical {channel, scale?, offset?, unit?, label?}`, `restart_average`, `set_sim` (simulator). Measuring: `acquire -> {acq_id}`,
`abort`/`stop`, `get_trace {which: live|sample}`, `get_time`, `get_sample`.
Plus `status`, `info`, `get_config`, `set_config`, `describe`, `shutdown`.

## Setup (lab PC)

1. NI-VISA. The scope is USB-TMC (`USB0::0xF4EC::0xEE3A::<serial>::INSTR`,
   Siglent's vendor id) -- or GPIB through Siglent's USB-GPIB adapter at the
   address in the scope's I/O menu (it showed 18): `GPIB0::18::INSTR`.
2. `cd modules\detector\scope-control` then `uv sync --extra gui --extra real`.
3. Mission Control > Instruments on this PC offers the address (`--visa`).

## Run

```powershell
uv run scripts/run_service.py                      # the simulated bench
uv run scripts/run_service.py --real --visa "USB0::0xF4EC::0xEE3A::SDS...::INSTR"
uv run scripts/run_gui.py --connect localhost
uv run scripts/scope_console.py acquire
uv run scripts/smoke_test.py
```

## First run on the instrument

The bench of 2026-10-06: AFG CH1 -> scope CH1, AFG CH2 -> scope CH2 and EXT
TRIG. Let the AFG module drive (afg-control): CH1 sine 30 Hz, CH2 square
following CH1 (`follow on 0`), both at high-Z.

1. **Identity and start.** Start `--real`. The log must say what the scope
   shows (V/div, time/div, trigger) and NOTHING may change on its screen.
   Any "could not read ..." names a query to fix.
2. **Reply headers / units** (`_num`): V/div, offset, time/div read right?
   Coupling words (D1M/A1M/GND) and probe (ATTN) right?
3. **Trigger readback** (`TRSE?` format, `EX:TRLV?`, `EX:TRSL?`): source EXT,
   level, slope as on the screen.
4. **New-trace flag** (`INR?` bit 0): the trigger rate in the header should be
   the AFG's 30 Hz, or whatever the scope manages (each record takes its own
   length plus dead time). 0 Hz = INR does not work that way: tell Claude.
5. **Waveform** (`WFSU`, `C1:WF? DAT2`, 25 codes/div): CH1 in the Y-t plot
   must have the AFG's amplitude; CH2 a 0..V square.
6. **Time axis and the trigger point** -- MEASURED 2026-10-07: the axis is
   built from the points the scope actually SENDS (at 1 ms/div the block held
   20480 points = 41 ms of memory; SANU? said 8000, the screen shows 14 ms),
   centred on the trigger (edge at +0.06 ms with delay 0), and a POSITIVE
   delay moves the window later (t = +TRDL - span/2 + ...; the first build had
   the sign inverted). Re-check: with a delay set, the edge stays at t = 0.
7. **Writing** each setting from the GUI (V/div, offset, coupling, probe,
   time/div, delay, source, level, slope, mode): the screen follows, the GUI
   shows the snapped value.
8. **Acquire** 50 averages: the time it takes ~ 51 / trigger rate.
9. Close the service: the scope's front panel works again (Go To Local).

Then record the result in `docs/VERIFIED_INSTRUMENTS.md`.

## Tests

```powershell
uv run pytest -q        # 71 tests, offline: the simulated bench + a fake SDS1000CML+
python ..\..\..\tools\check_modules.py scope --live
```
