# sync-spike — does an open library already do "set → settle → grab → store"?

**Yes.** This is a runnable proof that **QCoDeS** does exactly the synchronization
logic you described, and that it plugs onto your existing ZeroMQ + status-poll
architecture with a thin adapter — you don't have to write the sweep/settle/store
engine yourself.

## The logic you described, and where each piece lives

> "wait for all driven parameters to be set, then grab a data point or array of
> points → multidimensional arrays that need storing and processing"

| Your phrase | Who does it here |
|---|---|
| driven parameter | `qcodes_adapter.py` → the settable `field` parameter |
| "wait until it's set" | `magnet_client.py` → `wait_settled()` (polls `status` until `settled`) |
| "grab a data point" | the measured `kerr` parameter |
| the sweep loop over a grid | QCoDeS `dond` (in `run_sweep.py`) — **you write none of it** |
| multidimensional array | QCoDeS `DataSet` → `xarray` (`.nc` files) |
| storing / processing | SQLite (`spike.db`) + netCDF, both open formats |

## Files

- `mock_instrument.py` — a fake instrument server on your blueprint's wire
  protocol (ZeroMQ REQ/REP, fire-and-forget `set_field`, poll `status` for
  `settled`). It *ramps* at a finite slew rate so "wait for settle" is a real
  problem, not a no-op.
- `magnet_client.py` — the brain-compatible client facade. The key addition is
  `wait_settled()` / `set_field_blocking()`, which turn fire-and-forget+poll into
  a single blocking call.
- `qcodes_adapter.py` — wraps that client as a QCoDeS `Instrument` with a
  settable `field` and a gettable `kerr`. **~40 lines. This is the whole cost of
  adopting QCoDeS.**
- `run_sweep.py` — launches two mock instruments as separate processes, runs a
  1-D and a 2-D sweep, exports to netCDF, saves `sweeps.png`.

## Run it

```bash
pip install qcodes pyzmq matplotlib h5netcdf
python run_sweep.py
```

Outputs: `spike.db`, `field_sweep_1d.nc`, `field_sweep_2d.nc`, `sweeps.png`.

## How this maps to your REAL project

1. Delete `mock_instrument.py` — your actual meqi/magnet server replaces it.
2. In `magnet_client.py`, keep your real `client.py`; just make sure it has a
   blocking "set and wait until settled" method. You already have `status` with
   an effect-settled signal, so this is a few lines.
3. In `qcodes_adapter.py`, point each QCoDeS parameter at your client's real
   setters/getters. One adapter per instrument (magnet, RF, stage, piezo,
   camera…). Each of your ZeroMQ modules becomes one QCoDeS `Instrument`.
4. Your planned **coordinator** = a QCoDeS `Station` holding those instruments;
   `dond(sweepA, sweepB, ..., det1, det2, ...)` is the experiment. Nesting more
   sweeps just adds dimensions to the dataset — no new loop code.

## The honest trade-off

- **Adopt QCoDeS** (this spike): you inherit sweeps, settle-handling, N-dim
  datasets, storage, and xarray export for free; cost = one small adapter per
  instrument.
- **Build it yourself**: reuse your status-poll for "all set", write results into
  `xarray` (netCDF/HDF5) or NeXus. Pure stack, more to learn, but you
  re-implement nested sweeps and incremental saving that QCoDeS already ships.

The alternative heavyweight framework is **Bluesky/ophyd** (its Status-object
model maps even more literally to "wait for all driven parameters" — each `set`
returns a Status, the RunEngine waits on all of them), but it's a steeper first
framework. QCoDeS is the better starting point for you.
