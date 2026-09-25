# meqi + QCoDeS — real-client spike

> **Naming note (2026-09-10):** the magnet module was renamed `meqi` -> `clMag`
> (folder `clMag-control`, package `clMag`). This spike is kept EXACTLY as it ran
> in 2026-08-11, old names and all, because rewriting a record of a past experiment
> would misrepresent it. It does not run as unpacked in any case: `run_meqi_service.py`
> expects a copy of the package beside it that was never included in the zip.

Proof that QCoDeS drives your **actual** meqi service (sim backend, unchanged
code) as the synchronization layer. A 1-D field sweep where QCoDeS sets the
field, waits for your controller's real `field_stable` signal, then reads
`measured_field` and `current` into a dataset. Verified end-to-end (qcodes 0.58,
Python 3.11): 17-point sweep −40→+40 mT in ~21 s, measured field tracks setpoint
1:1 (see `meqi_sweep.png`).

## What each file is

- `meqi_qcodes.py` — **the adapter, and the only new code.** Wraps your
  `meqi.net.client.MeqiClient` as a QCoDeS `Instrument`:
  - `field` (settable) — set is fire-and-forget `set_field`, then it BLOCKS in
    `_wait_stable()` until the service reports the new setpoint adopted **and**
    `field_stable` True (same poll your `scripts/client_demo.py` does by hand).
  - `measured_field`, `current` (gettable) — read off the status frame.
- `run_meqi_sweep.py` — starts the service, wraps it, runs `dond`, exports
  netCDF + plot. You write no sweep loop and no settle logic here.
- `run_meqi_service.py` — sandbox convenience that boots the sim service. In your
  repo you already have this: `uv run scripts/run_service.py`.

## Run it in your repo

```bash
# terminal 1 — your existing service
uv run scripts/run_service.py

# terminal 2 — drop meqi_qcodes.py + run_meqi_sweep.py in the repo, then:
uv run python run_meqi_sweep.py     # needs: pip install qcodes matplotlib h5netcdf
```

## What this proves for the coordinator

Nothing in meqi changed. QCoDeS never sees PID, seeking, calibration, or the
state machine — those stay in your controller. The adapter exposes only "set a
field (blocks until stable)" and "read a number", and QCoDeS's `dond` turns that
into set → wait → grab → store, into a labelled dataset (SQLite + netCDF/xarray).

The one real improvement to make in meqi itself: add the blocking set to
`MeqiClient` (e.g. `set_field_blocking` / `wait_stable`) so it's reusable outside
QCoDeS too; the adapter then just calls it. After that, each of your other
modules (RF, stages, piezo, zpiezo, kim) gets the same ~40-line adapter, and the
coordinator = a QCoDeS `Station` holding them all. `dond(field_sweep, freq_sweep,
..., detector)` becomes a multidimensional measurement with no new loop code.
```
