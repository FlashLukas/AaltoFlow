# shsna-control -- Scalar network analyser (Signal Hound)

Transmission |S21| in dB with the Signal Hound kit: the **USB-TG44A tracking
generator** sweeps a tone through a device under test (DUT) into the
**SA44B / SA124B** analyser, and the power that comes through is divided by a
stored **thru reference** (the same sweep with the DUT replaced by a thru).
Scalar: power only, no phase.

![front panel](../../../front-panels/shsna.png)

Ports 5627 (commands) / 5628 (status).

## Three modules, one analyser

The SA API allows ONE process per analyser, and the TG can only be driven
through the analyser's handle. So (decision 2026-09-28):

| module | what it does |
|---|---|
| `signalhound` | the spectrum analyser; the **only owner** of the USB devices |
| `shsg` | the TG as a CW signal generator (through `signalhound`) |
| `shsna` | this module: TG sweeps through a DUT (through `signalhound`) |

With `--real`, shsna is a **client of the signalhound service** (raw ZeroMQ,
no package import). It opens no USB device and claims no hardware lock. A TG
sweep is exclusive on the analyser: signalhound pauses its spectrum sweeping
and any CW output while it runs, and restores them afterwards. Start
signalhound first (`start_after` in module.toml does that in the launcher).

## Units

The TG44A reports a TG sweep in **dB relative to its calibrated output**, not
in dBm, and its output level cannot be set in sweep mode (measured on the lab
PC, 2026-09-28: -19.4 dB flat through a 20 dB pad; -30 and -20 dBm gave the
same trace). So there is no level knob, `raw` is in dB (rel. TG output), and
`transmission = raw - thru` in dB. At most 1001 points per sweep; a sweep
takes about 0.2 s + 1.3 ms per point.

## Measuring

1. Replace the DUT by a thru, press **Take reference** (or `take_reference`).
2. Put the DUT back, press **Acquire** (or `acquire`).
3. Transmission appears; peak, its frequency, band-averaged transmission and
   the -3 dB width are shown and are scan detectors.

Transmission is REFUSED (with the reason) without a reference, or when the
reference was taken on another frequency grid. A failed acquisition (owner
down, no TG, sweep aborted) is latched as failed: `acq_error` says why, and
reading its trace raises instead of returning the previous one.

## In a scan

Detectors (one acquisition feeds all of them): `transmission` and `raw`
(arrays over the analyser's frequency grid, in MHz), `peak_transmission`,
`peak_freq`, `mean_transmission`, `bw3`, `raw_peak`. Actions for routines:
`take_reference` (waits, then checks `acq_error == ""`), `clear_reference`.
The frequency coordinate is the analyser's grid, known after a reference (or
one acquisition) of the band -- take the reference before the scan.

## Running

```powershell
cd modules\detector\shsna-control
uv sync --extra gui
uv run pytest -q
uv run scripts/run_service.py              # simulator (standalone)
uv run scripts/run_service.py --real       # via the signalhound service (5587/5588)
uv run scripts/run_gui.py --connect localhost
uv run scripts/run_gui.py                  # in-process simulator
uv run scripts/shsna_console.py            # raw-protocol console
uv run scripts/smoke_test.py
```

The simulator models the bench: TG ripple, cable loss rising as sqrt(f), a
20 dB pad, and a Butterworth band-pass DUT that can be removed (`set_sim
dut_inserted false`) to take the thru.
