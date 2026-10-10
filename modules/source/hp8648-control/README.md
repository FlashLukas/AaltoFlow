# hp8648-control

Control for an **HP / Agilent 8648D** RF signal generator (9 kHz - 4 GHz):
switch the RF output on/off and set the CW **frequency** and **output level** --
over GPIB (SCPI), or fully simulated with no hardware. An AaltoFlow module on
ports **5619 / 5620**.

![hp8648 front panel](../../../front-panels/hp8648.png)

*RF on at 3200 MHz / +4 dBm. The spectrum screen shows the carrier as one line;
the red staircase is the power ceiling, which drops from +13 to +10 dBm above
2500 MHz, so +12 dBm is allowed at 2 GHz but not at 3 GHz.*

It is a **set-and-forget** instrument: no control loop, no state machine. The
brain (`SignalSource`) holds the desired signal, clamps it to the safety
limits, and lets one worker thread write it to the box, wait for the
synthesiser to switch, and read everything back. The one thing it does over
time is a **sweep** (section "Sweeps" below): walking frequency or level at a
set pace, for fly scans. A few things make it more than a setter:

- **The power ceiling depends on frequency.** The 8648D is specified to
  +13 dBm up to 2500 MHz and +10 dBm above (option 1EA raises it). The brain
  clamps to the tighter of that and your own envelope, and `describe` publishes
  the ceiling as the LIVE maximum of `power`, so its revision changes when the
  frequency crosses 2500 MHz. Moving to a higher band with a level that no
  longer fits lowers the level FIRST, then moves the frequency.
- **Reverse-power protection (RPP).** If power arrives at the RF output the box
  trips and switches its RF off. The module notices (status `rpp_tripped`, an
  error event, a red lamp) and makes "off" the desired state too, so nothing
  switches it back on behind your back. Remove the source, then switch RF on:
  that is also how the instrument re-arms.
- **Adopt at connect, change nothing.** When the service starts it READS the
  generator -- RF on/off, frequency, level, modulation, reverse-power state,
  reference and attenuator modes -- and shows exactly that; it writes nothing
  but `*CLS` (clears the error queue). A generator left running keeps running.
  RF found ON, a modulation found ON, a level outside your envelope or a
  reference mode found ON are each reported as a `warn` event and left as they
  are (reference modes are converted in software). The `signal` values in the
  config are defaults, sent only when you change them in Settings.
- **Pure CW is assumed, not forced.** Status reports `modulation_off` and the
  AM/FM/PM state; the module never switches a modulation. There is no phase
  control on the 8648 (and so no phase sweep).

The RF output is switched **only by an explicit command** (there is no config
switch for it) and is switched **off at shutdown** (Ctrl-C, the `shutdown`
verb, the launcher's Stop, or closing a local GUI).

## Layout

```
src/hp8648/
  config.py              Signal / Limits / Hardware / UI + .ini save/load
  spec.py                the 8648D's ranges, resolution and freq-dependent max level
  backends/
    base.py              SigGenBackend Protocol -- the interface everything depends on
    sim.py               SimulatedHP8648 -- resolution, RPP, unspecified-level flag, power-on state
    visa_8648.py         Visa8648 -- the real box over GPIB (SCPI, lazy pyvisa import)
  source.py              SignalSource -- clamps, worker thread, safe write order, RPP, sweeps
  softramp.py            the suite's software ramp (byte-identical copy of suite-common's)
  sim_system.py          build_sim_system(cfg)
  net/
    protocol.py          wire shapes + default ports (5619/5620)
    describe.py          the self-description (live power ceiling)
    service.py           Hp8648Service -- owns the brain, serves it over ZeroMQ
    client.py            Hp8648Client -- SignalSource-compatible facade over the socket
  apps/                  gui.py (SpectrumIndicator), settings_dialog.py, theme.py
scripts/
  run_service.py         start the service (simulated by default, --real for GPIB)
  run_gui.py             the GUI (local simulator, or --connect HOST)
  hp8648_console.py      standalone raw-protocol console (only needs pyzmq)
  smoke_test.py          quick offline check
tests/                   pytest: config, brain, describe, network, GUI, sweeps (all offline)
```

## Verbs

| verb | arguments | effect |
|---|---|---|
| `set_rf` | `on` (bool) | RF output on/off; on also re-arms a tripped RPP |
| `set_frequency` | `frequency_Hz` | CW frequency, clamped to the envelope |
| `set_power` | `power_dBm` | level, clamped to the live ceiling |
| `status` `info` `get_config` `set_config` `describe` `shutdown` | | the universal verbs |
| `shutdown` | `keep_outputs?` (bool) | plain: RF off; `true` = a restart for a code update, RF left as it is (the next start adopts it) |
| `ramp_frequency` `ramp_power` `ramp_stop` `stream_start` `stream_read` `stream_stop` | | the sweeps (section "Sweeps" below) |

A reply means **accepted**, not done. Status carries the read-back values
(`rf_on`, `frequency_Hz`, `power_dBm`), the setpoints (`*_set`),
`power_ceiling_dBm`, `rpp_tripped`, `level_unspecified`, `modulation_off`,
`hw_error` and `describe_rev`. A scan waits with the `echoes` policy; the
tolerances are the instrument's resolution (10 Hz, 0.1 dB), so a request for
-12.34 dBm counts as settled at -12.3.

## Sweeps (fly scans over frequency or level)

A fly scan (scan-core, `type: fly` axis) records the detectors while a knob
moves CONTINUOUSLY and bins every sample by the value the knob had at that
moment. The 8648D jumps to the value it is told, so the **service walks the
knob** in small steps (`softramp.py`, the suite's software ramp, copied byte
for byte from suite-common): one `FREQ:CW` / `POW:AMPL` every
`hardware.ramp_dt_s` (0.1 s), each value computed from the elapsed time, so a
late step does not slow the sweep down. There is no phase sweep: the 8648D
has no phase control.

| verb | arguments | pace limits (config `[limits]`) |
|---|---|---|
| `ramp_frequency` | `frequency_Hz`, `rate_Hz_per_s` | `ramp_rate_min/max_Hz_per_s` (1 kHz/s .. 4 GHz/s) |
| `ramp_power` | `power_dBm`, `rate_dB_per_s` | `ramp_rate_min/max_dB_per_s` (0.01 .. 100 dB/s) |
| `ramp_stop` | `knob` (optional; none = every sweep) | a stop: a viewer may send it |

- The reply carries the sweep's number (`ramp_id`); status shows
  `<knob>_ramping`, `<knob>_ramp_id`, the target and the pace
  (`frequency_ramp_target_Hz`, `power_ramp_rate_dB_per_s`, ...) and `ramping`
  (any knob). The sweep is over when `<knob>_ramp_id` is yours and
  `<knob>_ramping` is false.
- A target or pace outside the limits is clamped, with a warning. The limits
  are the same EFFECTIVE ones the setters use: the frequency never wider than
  the 8648D, the level never above the ceiling at the current frequency.
- **The level ceiling over a frequency sweep:** if the level does not fit the
  ceiling ANYWHERE on the way (e.g. +12 dBm on a sweep from 2 to 3 GHz), it is
  lowered once, to the lowest ceiling on the path, BEFORE the sweep starts --
  so the whole sweep runs at one level -- with a warning.
- An ordinary `set_frequency` / `set_power` takes that knob over (stops its
  sweep); a set of the other knob does not. A reverse-power trip stops every
  sweep.
- **The RF output is never switched by a sweep.**
- The record: the stream verbs hand out every value each sweep SENT, one
  channel per knob (`frequency`, `power`, each with its own time stamps in
  `t_ch`). describe declares a `ramp` block on each knob with
  `readback.measured: false` -- **binned by command**. Why not read back:
  `FREQ:CW?` / `POW:AMPL?` return the setting the box holds (the "programmed"
  value), not a measurement, so they would only echo the number just sent and
  cost a GPIB round trip per step. Sweep steps skip the worker's
  `switch_settle_s` wait, which exists for a read-back right after a set.
- **What the command does not capture (an old synthesiser):** every frequency
  step is a synthesiser SWITCH, specified < 75 ms below 1001 MHz and < 100 ms
  above. The output reaches each value up to one switching time after its time
  stamp, and may blank or glitch while it relocks. At a pace R that is an
  offset of about R x t_switch in the binned frequency (10 MHz/s x 0.1 s =
  1 MHz); a slower pace, or zig-zag rows (the two directions shift
  oppositely), keeps it small. That is also why `ramp_dt_s` is 0.1 s and not
  smaller. On a level sweep the step attenuator switches at fixed levels; if
  it is mechanical it clicks at every switch point and the level may jump
  briefly there. The level resolution is 0.1 dB.
- **VERIFY on the unit:** how long one write takes over GPIB; the real
  switching time per step and whether the output blanks while it relocks
  (both bound `ramp_dt_s`); which step attenuator the unit has, and the size
  of the level glitch at a switch point, before sweeping across tens of dB.

The GUI's **Sweep** card does the same by hand: pick the knob, set the pace,
"Sweep to" walks it to the value in that knob's box on the left, "Stop" ends
it. While a knob sweeps, its box keeps the target instead of following the
moving setpoint.

## SCPI commands used (real backend)

| function | set | query |
|---|---|---|
| RF output | `OUTP:STAT ON` / `OFF` | `OUTP:STAT?` |
| level | `POW:AMPL <v> DBM` | `POW:AMPL?` |
| frequency | `FREQ:CW <v> MHZ` | `FREQ:CW?` |
| modulation off | `AM:STAT OFF` `FM:STAT OFF` `PM:STAT OFF` `PULM:STAT OFF` | `AM:STAT?` ... |
| absolute units | `POW:REF:STAT OFF` `FREQ:REF:STAT OFF` `POW:ATT:AUTO ON` | |
| RPP / spec | | `STAT:QUES:POW:COND?` (bit 0 RPP, bit 1 unspecified level) |
| errors | | `SYST:ERR?` |

Source: *HP 8648A/B/C/D Operation and Service Guide*, chapter 2 (HP-IB
programming). The default address is `GPIB0::19::INSTR` (the factory HP-IB
address). The rear-panel language switch must be on **SCPI**. Every call not
yet confirmed on the instrument is marked `# VERIFY` in `backends/visa_8648.py`.

## Run

```powershell
cd modules\source\hp8648-control
.\dev.ps1 sync --extra gui                     # venv outside OneDrive (gotcha #8)
.\dev.ps1 run pytest -q
.\dev.ps1 run python scripts/smoke_test.py
.\dev.ps1 run python scripts/run_service.py    # simulated
.\dev.ps1 run python scripts/run_gui.py --connect localhost
.\dev.ps1 run python scripts/hp8648_console.py
    rf> freq 1.5 GHz
    rf> power -10
    rf> rf on
    rf> status
```

Real hardware (lab PC, NI-VISA installed):

```powershell
.\dev.ps1 sync --extra gui --extra real        # name BOTH extras (gotcha #29)
.\dev.ps1 run python scripts/run_service.py --real
.\dev.ps1 run python scripts/run_service.py --real --visa GPIB0::19::INSTR
```

## Using it from Python

```python
from hp8648.net.client import Hp8648Client

rf = Hp8648Client(host="localhost")
rf.start()
rf.set_frequency(2.45e9)
rf.set_power(-10.0)
rf.set_rf(True)
print(rf.status().rf_on, rf.status().power_ceiling_dBm)
rf.shutdown()          # closes the client; the service keeps running
```
