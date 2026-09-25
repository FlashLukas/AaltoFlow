# Calibrations

Measured `B(V)` curves for the 2-axis magnet live here, one JSON file per
measurement, named `mag2dcal_YYYYMMDD_HHMMSS.json`.

- The service **auto-saves** every calibration it measures here
  (`calibration.auto_save`) and **loads the newest one at start**
  (`calibration.load_newest_on_start`). Both can be turned off in Settings, and
  a specific file can be forced with `run_service.py --calibration FILE`.
- Each file holds, per axis, the **up** and **down** hysteresis legs as
  `[volts, millitesla]` pairs, plus the Hall constants it was recorded with. If
  those constants change, the old curve is no longer valid -- measure again.
- These are worth keeping in git: they are the record of what the magnet
  actually did on a given day, and a new one that disagrees with an old one is
  telling you something.

Measure one from the GUI's Calibration card, or:

```powershell
uv run scripts/mag2dcal_console.py --connect localhost calibrate 21 0.5 5
```

`dwell_s` (the middle number) must be several time constants of the magnet, or
every point is recorded while the coil is still arriving.
