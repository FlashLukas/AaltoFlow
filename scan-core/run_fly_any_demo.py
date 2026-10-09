"""FLY SCAN over ANY knob that can sweep: a field x frequency FMR map, flown.

Lukas (2026-10-09): "not just XY scanning ... magnetic field, RF frequency".
A module that can sweep a knob continuously declares a `ramp` block in
describe; the fly engine then asks the MODULE to sweep that knob over each
row and bins every detector sample by the ramp's readback (scan_core/ramp.py).

This demo makes the same simulated map three ways, on the in-process
simulator (its field and RF frequency both have a software ramp):

  1. STEPPED  -- the reference: set, measure, next point.
  2. FIELD FLOWN -- the frequency steps, the FIELD sweeps each row; binned by
     the MEASURED field (the simulator streams a Hall-probe reading, as clMag
     does): attribute fly_binned_by = "measurement".
  3. FREQUENCY FLOWN -- the field steps, the FREQUENCY sweeps each row; binned
     by the COMMANDED frequency and its time stamp (a generator cannot report
     its frequency while sweeping, as dssg cannot): fly_binned_by = "command".

    uv run python run_fly_any_demo.py
    uv run python run_fly_any_demo.py --rate-field 40 --rate-freq 400 --tc 5

Writes out/fly_any_stepped.nc, out/fly_any_field.nc, out/fly_any_freq.nc and
out/fly_any.png. Every printed line is ASCII (gotcha #14).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np

from scan_core.engine import run
from scan_core.recipe import Recipe
from scan_core.registry import build_sim_registry

OUT = Path(__file__).parent / "out"

# off the islands of the simulated sample: the plain film's Kittel line
WHERE = {"pos_x": 30.0, "pos_y": 50.0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fields", type=int, default=41, help="field pixels / points")
    ap.add_argument("--freqs", type=int, default=21, help="frequency pixels / points")
    ap.add_argument("--rate-field", type=float, default=30.0, help="mT/s")
    ap.add_argument("--rate-freq", type=float, default=300.0, help="MHz/s")
    ap.add_argument("--tc", type=float, default=5.0,
                    help="lock-in time constant, ms (order 2)")
    args = ap.parse_args()

    reg = build_sim_registry()
    reg._state.lockin_tc_s = args.tc / 1e3
    field = {"param": "field", "start": 10.0, "stop": 90.0, "num": args.fields}
    freq = {"param": "rf_freq", "start": 700.0, "stop": 1300.0, "num": args.freqs}
    dets = ["lockin_r"]
    recipes = {
        "stepped": Recipe(name="fly_any_stepped", fixed=WHERE, detectors=dets,
                          axes=[{"type": "linear", **freq},
                                {"type": "linear", **field}]),
        "field": Recipe(name="fly_any_field", fixed=WHERE, detectors=dets, zigzag=True,
                        axes=[{"type": "linear", **freq},
                              {"type": "fly", **field, "speed": args.rate_field}]),
        "freq": Recipe(name="fly_any_freq", fixed=WHERE, detectors=dets, zigzag=True,
                       axes=[{"type": "linear", **field},
                             {"type": "fly", **freq, "speed": args.rate_freq}]),
    }
    results = {}
    OUT.mkdir(exist_ok=True)
    for key, r in recipes.items():
        errs = r.validate(reg)
        if errs:
            print(f"{r.name}: recipe is not valid:\n  " + "\n  ".join(errs))
            return 1
        t0 = time.monotonic()
        ds = run(r, reg, on_log=lambda m: print("  " + m))
        dt = time.monotonic() - t0
        extra = ""
        if "lockin_r_n" in ds:
            n = ds["lockin_r_n"].values
            fly_dim = r.axes[-1].get("param")
            extra = (f", {np.nanmedian(n):.0f} samples per pixel, binned by "
                     f"{ds[fly_dim].attrs.get('fly_binned_by')}")
        print(f"{r.name}: {dt:.1f} s{extra}")
        ds.to_netcdf(OUT / f"{r.name}.nc", engine="h5netcdf")
        results[key] = ds

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(1, 3, figsize=(15, 4.6), sharey=True)
    titles = {"stepped": "stepped (reference)",
              "field": f"FIELD flown at {args.rate_field:g} mT/s\n(binned by measurement)",
              "freq": f"FREQUENCY flown at {args.rate_freq:g} MHz/s\n(binned by command)"}
    for ax, key in zip(axs, ("stepped", "field", "freq")):
        da = results[key]["lockin_r"].transpose("rf_freq", "field")
        ax.pcolormesh(da["field"], da["rf_freq"], da.values, shading="nearest")
        ax.set_title(titles[key])
        ax.set_xlabel("field (mT)")
    axs[0].set_ylabel("RF frequency (MHz)")
    fig.suptitle("Fly scans over any knob: the same FMR map, three ways (zig-zag rows)")
    fig.tight_layout()
    fig.savefig(OUT / "fly_any.png", dpi=110)
    print(f"wrote {OUT / 'fly_any.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
