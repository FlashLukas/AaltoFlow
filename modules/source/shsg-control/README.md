# shsg-control -- Signal generator (Signal Hound TG)

The Signal Hound **USB-TG44A tracking generator used as a plain CW source**:
output on/off, frequency (10 Hz - 4.4 GHz) and level (-30 to -10 dBm). No
phase, no modulation -- the TG has neither. Ports 5625 / 5626.

**The TG44A cannot be switched off.** It keeps emitting its last frequency and
level even after every program has exited. So "RF off" here means **parked**:
moved to a park frequency (the signalhound service's setting, default 10 kHz)
at the minimum level (-30 dBm). The GUI says so ("RF off - TG parked at 10 kHz,
-30 dBm (the TG44A cannot be silenced)") and status carries `parked`,
`park_Hz`, `park_dBm`; `rf_on` is false while parked.

![shsg front panel](../../../front-panels/shsg.png)

## How it relates to the other Signal Hound modules

The Signal Hound API drives the TG **through the spectrum analyser's USB
handle**, and a USB device can be opened by one process only. So the kit is
split into three modules (decision 2026-09-28):

| module | what it is | touches USB? |
|---|---|---|
| `signalhound` | spectrum analyser; the **owner** of both USB devices | yes, the only one |
| `shsg` (this) | the TG as a signal generator (CW) | no -- a client of `signalhound` |
| `shsna` | scalar network analyser (TG sweep + analyser) | no -- a client of `signalhound` |

With `--real`, shsg sends `tg_cw {on?, freq_hz?, level_dbm?}` to the
signalhound service and shows what that service **publishes as applied**
(`tg_cw_on`, `tg_cw_freq_hz`, `tg_cw_level_dbm`) -- never its own request, so a
scan waiting for the frequency cannot be fooled by a value that was only asked
for. The launcher starts `signalhound` first (`start_after` in module.toml) and
passes its address in `AALTOFLOW_ENDPOINTS`; started by hand, shsg uses
`[hardware] owner_host / owner_cmd_port / owner_pub_port` (default
127.0.0.1:5587/5588).

The CW output may stay on while the analyser sweeps a spectrum. A **network
analyser sweep takes the TG** for its duration: shsg then shows `tg_busy`
(amber "TG BUSY" in the GUI) and refuses commands with a clear message; the
signalhound service restores the CW afterwards.

## Behaviour worth knowing

- **Adopt on start:** the service only READS the TG's state at start; nothing is
  written. The analyser usually cannot read the TG (`tg_mode` "unknown" -- it may
  still be emitting whatever another program left it at): then nothing is
  adopted and the GUI says "TG state unknown -- it may be emitting"; set
  frequency, level and CW (or park) explicitly.
- **Stop:** a clean stop (launcher Stop, `shutdown` verb, closing the local
  GUI) **parks** the TG (`[hardware] off_on_shutdown`, default on). A killed
  service leaves the TG as it is; the signalhound service parks it when it
  stops itself.
- **signalhound not running:** shsg still starts; status `hw_error` says
  "signalhound service not reachable (...)" (red in the GUI) and commands are
  refused at once. When the owner comes up, shsg picks it up by itself.
- **Status keys** besides `rf_on`, `frequency_Hz`, `power_dBm`: `parked`,
  `park_Hz`, `park_dBm`, `tg_busy`,
  `tg_unknown`, `tg_ready` (connected, no error, not busy, state known),
  `hw_error`, `connected`, `idn`.
- **Scans:** `frequency`, `power` and `rf_on` settle when the echo matches the
  requested value (within `echo_tol_Hz` / `echo_tol_dB`) **and** `tg_ready` is
  true -- so no point is measured while a network-analyser sweep holds the TG.

## Run

```powershell
cd modules\source\shsg-control
uv sync --extra gui
uv run pytest -q
uv run scripts/smoke_test.py                   # offline sanity check
uv run scripts/run_service.py                  # standalone SIMULATED TG
uv run scripts/run_service.py --real           # the real TG, via the signalhound service
uv run scripts/run_gui.py                      # GUI with its own private simulator
uv run scripts/run_gui.py --connect localhost  # GUI of a running service
uv run scripts/shsg_console.py status          # raw-protocol console
```
