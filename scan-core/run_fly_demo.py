"""FLY SCAN demo: move without stopping, bin by the measured position.

Two modes.

  --sim (default): the in-process simulator. Its stage travels at a real
  speed and its lock-in stream lags behind a real low-pass filter, so the one
  thing a fly scan must get right can be SEEN: the same zig-zag image with
  and without the lag correction. Without it, every other row is shifted the
  other way and edges look combed; with it they line up. Writes
  out/fly_sim_corrected.nc, out/fly_sim_raw.nc and out/fly_sim.png.

      uv run python run_fly_demo.py
      uv run python run_fly_demo.py --tc 30 --speed 30

  --lab: the running kim and hf2 services (Mission Control, or two terminals):

      cd ..\\kim-control ; uv run scripts/run_service.py
      cd ..\\hf2-control ; uv run scripts/run_service.py

      uv run python run_fly_demo.py --lab --from 0 --to 20 --pixels 41 --speed 4

  flies kim X at --speed um/s over --rows rows of kim Y, recording hf2 X1/R1
  continuously, and prints how many samples landed in each pixel. Writes
  out/fly_lab.nc. Ports: --kim-port / --hf2-port if not the launcher's.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np

from scan_core.engine import run
from scan_core.recipe import Recipe

OUT = Path(__file__).parent / "out"


def sim(args) -> int:
    from scan_core.registry import build_sim_registry
    reg = build_sim_registry()
    reg._state.lockin_tc_s = args.tc / 1e3
    results = {}
    for lag in (True, False):
        recipe = Recipe(
            name=f"fly_sim_{'corrected' if lag else 'raw'}",
            comment="fly scan over the simulated patterned sample",
            fixed={"field": 40.0, "rf_freq": 890.0},
            axes=[{"type": "linear", "param": "pos_y", "start": -20, "stop": 20,
                   "num": args.rows},
                  {"type": "fly", "param": "pos_x", "start": -40, "stop": 40,
                   "num": args.pixels, "speed": args.speed,
                   "speed_param": "stage_speed", "lag_correction": lag}],
            detectors=["lockin_x"], zigzag=True)
        errs = recipe.validate(reg)
        if errs:
            print("recipe is not valid:\n  " + "\n  ".join(errs))
            return 1
        t0 = time.monotonic()
        ds = run(recipe, reg, on_log=lambda m: print("  " + m))
        dt = time.monotonic() - t0
        n = ds["lockin_x_n"].values
        print(f"{recipe.name}: {args.rows} x {args.pixels} pixels in {dt:.1f} s, "
              f"{np.nanmedian(n):.0f} samples per pixel (min {np.nanmin(n):.0f})")
        OUT.mkdir(exist_ok=True)
        ds.to_netcdf(OUT / f"{recipe.name}.nc", engine="h5netcdf")
        results[lag] = ds

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.6), sharey=True)
    lag_um = args.speed * 2 * args.tc / 1e3
    for ax, lag in zip(axs, (False, True)):
        ds = results[lag]
        ax.pcolormesh(ds["pos_x"], ds["pos_y"], ds["lockin_x"], shading="nearest")
        ax.set_title(("lag corrected" if lag else "NOT corrected")
                     + f"  (lag = {lag_um:.2g} um at {args.speed:g} um/s)")
        ax.set_xlabel("x (um)")
        ax.set_aspect("equal")
    axs[0].set_ylabel("y (um)")
    fig.suptitle("Fly scan, zig-zag rows: every other row is flown backwards")
    fig.tight_layout()
    fig.savefig(OUT / "fly_sim.png", dpi=120)
    print(f"wrote {OUT / 'fly_sim.png'}")
    return 0


def lab(args) -> int:
    from scan_core.instrument import InstrumentError
    from scan_core.lab import build_lab_registry
    endpoints = {name: (args.host, port, port + 1)
                 for name, port in (("kim", args.kim_port), ("hf2", args.hf2_port)) if port}
    print(f"connecting to kim and hf2 on {args.host} ...")
    try:
        reg, labs = build_lab_registry(host=args.host, include=("kim", "hf2"), prefix=True,
                                       endpoints=endpoints or None, on_warn=lambda m: None)
    except InstrumentError as exc:
        print(f"\nFAILED: {exc}\n\nStart both services first (see the top of this file).")
        return 1
    try:
        y0 = float(reg.get("kim.position_y").get())
        recipe = Recipe(
            name="fly_lab",
            comment="fly scan: kim X continuous, hf2 streamed",
            axes=[{"type": "linear", "param": "kim.position_y", "start": y0,
                   "stop": y0 + args.row_pitch * (args.rows - 1), "num": args.rows},
                  {"type": "fly", "param": "kim.position_x", "start": args.lo,
                   "stop": args.hi, "num": args.pixels, "speed": args.speed,
                   "speed_param": "kim.velocity_x"}],
            detectors=["hf2.x1", "hf2.r1"], zigzag=args.zigzag)
        errs = recipe.validate(reg)
        if errs:
            print("recipe is not valid:\n  " + "\n  ".join(errs))
            return 1
        t0 = time.monotonic()
        ds = run(recipe, reg, on_log=lambda m: print("  " + m),
                 on_progress=lambda d, t, eta: None)
        dt = time.monotonic() - t0
        n = ds["hf2.x1_n"].values
        print(f"fly_lab: {args.rows} x {args.pixels} pixels in {dt:.1f} s")
        print(f"  samples per pixel: median {np.nanmedian(n):.0f}, min {np.nanmin(n):.0f}, "
              f"max {np.nanmax(n):.0f}; empty pixels: {int(np.sum(n == 0))}")
        # a fresh status by command, not the 8 Hz cache (which can be a frame old)
        st = labs["kim"].command("status")["status"]
        print(f"  kim X after the scan: {st['position_um'][0]:.3f} um, "
              f"velocity {st['velocity_um'][0]:.3f} um/s")
        OUT.mkdir(exist_ok=True)
        ds.to_netcdf(OUT / "fly_lab.nc", engine="h5netcdf")
        print(f"wrote {OUT / 'fly_lab.nc'}")
    finally:
        labs.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lab", action="store_true", help="use the kim + hf2 services")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--rows", type=int, default=None)
    ap.add_argument("--pixels", type=int, default=None)
    ap.add_argument("--speed", type=float, default=None, help="um/s")
    ap.add_argument("--tc", type=float, default=15.0,
                    help="sim: lock-in time constant, ms (order 2)")
    ap.add_argument("--from", dest="lo", type=float, default=0.0, help="lab: X start, um")
    ap.add_argument("--to", dest="hi", type=float, default=10.0, help="lab: X stop, um")
    ap.add_argument("--row-pitch", type=float, default=1.0, help="lab: Y step, um")
    ap.add_argument("--zigzag", action="store_true", help="lab: fly every other row back")
    ap.add_argument("--kim-port", type=int, default=None)
    ap.add_argument("--hf2-port", type=int, default=None)
    args = ap.parse_args()
    if args.lab:
        args.rows = args.rows or 3
        args.pixels = args.pixels or 21
        args.speed = args.speed or 2.0
        return lab(args)
    # 81 pixels of 1 um at 50 um/s with a 200 Hz stream = 4 samples per pixel;
    # a 15 ms order-2 filter then lags 1.5 um, which the uncorrected image shows
    args.rows = args.rows or 15
    args.pixels = args.pixels or 81
    args.speed = args.speed or 50.0
    return sim(args)


if __name__ == "__main__":
    raise SystemExit(main())
