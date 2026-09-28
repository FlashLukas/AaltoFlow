"""RESONANCE WINDOW demo: the same FMR field sweep, full band vs windowed.

Lukas, 2026-09-28: "some devices are terribly slow and if I want to see e.g.
FMR in field I scan most of the time in the dark. Can we cheat?"

The simulator's `fmr` detector stands in for a spectrum analyser with a
tracking generator: a 2..20 GHz transmission trace (721 bins) with an FMR dip
that follows the in-plane Kittel line of a film whose TRUE mu0 Meff is
1650 mT. Each sweep costs time per bin swept (--ms-per-bin), as on the real
analyser. The windowed run ASSUMES 1750 mT (--assumed-meff): its first point is
a full sweep, the dip found there corrects the model, and every later point
sweeps only +- --margin MHz around the predicted line (a full sweep every
--full-every points refreshes the baseline). Outside the window the trace is
the baseline, and `fmr_measured` says which bins were measured.

    uv run python run_window_demo.py
    uv run python run_window_demo.py --points 81 --margin 200 --ms-per-bin 0.2

Prints both run times, how much of the band was measured, the largest
difference between the two datasets INSIDE the resonance region (where the
answer matters), and the final Meff estimate. Writes out/window_full.nc,
out/window_windowed.nc (and out/window_demo.png if matplotlib is there).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np

from scan_core import resonance
from scan_core.engine import run
from scan_core.recipe import Recipe
from scan_core.registry import build_sim_registry

OUT = Path(__file__).parent / "out"


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--points", type=int, default=41, help="field points")
    ap.add_argument("--from", dest="start", type=float, default=20.0, help="mT")
    ap.add_argument("--to", dest="stop", type=float, default=200.0, help="mT")
    ap.add_argument("--angle", type=float, default=0.0, help="in-plane field angle, deg")
    ap.add_argument("--margin", type=float, default=300.0, help="window half width, MHz")
    ap.add_argument("--full-every", type=int, default=20)
    ap.add_argument("--assumed-meff", type=float, default=1750.0, help="mT")
    ap.add_argument("--ms-per-bin", type=float, default=0.1,
                    help="sweep cost per bin (the real analyser is slower)")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args(argv)

    axes = [{"type": "linear", "param": "field", "start": args.start,
             "stop": args.stop, "num": args.points}]
    fixed = {"field_angle": args.angle}
    window = {"detector": "fmr", "field": "field", "angle": "field_angle",
              "model": "inplane",
              "params": {"g": 2.0, "meff_mT": args.assumed_meff, "hk_mT": 5.0,
                         "easy_axis_deg": 0.0},
              "margin_MHz": args.margin, "dip": "min", "track": True,
              "full_every": args.full_every}

    results = {}
    for label, win in (("full", None), ("windowed", window)):
        reg = build_sim_registry()
        reg._state.fmr_s_per_bin = args.ms_per_bin / 1000.0
        recipe = Recipe(name=f"window_demo_{label}", fixed=fixed, axes=axes,
                        detectors=["fmr"], window=win)
        errs = recipe.validate(reg)
        if errs:
            raise SystemExit("invalid recipe:\n  " + "\n  ".join(errs))
        t0 = time.monotonic()
        ds = run(recipe, reg, created_iso=time.strftime("%Y-%m-%dT%H:%M:%S"))
        results[label] = (ds, time.monotonic() - t0, reg._state)
        if not args.no_save:
            OUT.mkdir(exist_ok=True)
            ds.to_netcdf(OUT / f"window_{label}.nc", engine="h5netcdf")

    ds_full, t_full, st = results["full"]
    ds_win, t_win, _ = results["windowed"]
    f = st.fmr_freqs_MHz * 1e6
    true = st.fmr_true
    B = ds_full["field"].values

    # "the resonance region": +- margin around the TRUE line at each field.
    # Split into the bins the windowed run MEASURED there (the two runs differ
    # only by sweep noise) and the bins it FILLED (the window sat a little off
    # the true line: the fill misses the line's far tail -- the price of the
    # window, and why `margin` should be several linewidths).
    d_meas, d_fill = [0.0], [0.0]
    mask = ds_win["fmr_measured"].values
    for k, b in enumerate(B):
        f0 = resonance.kittel_hz("inplane", b, args.angle, true)
        sel = np.abs(f - f0) <= args.margin * 1e6
        d = np.abs(ds_win["fmr"].values[k] - ds_full["fmr"].values[k])
        if (sel & mask[k]).any():
            d_meas.append(float(np.max(d[sel & mask[k]])))
        if (sel & ~mask[k]).any():
            d_fill.append(float(np.max(d[sel & ~mask[k]])))
    frac = float(ds_win["fmr_measured"].values.mean())
    meff = float(ds_win["fmr_meff_mT"].values[-1])
    fit = ds_win["fmr_fres_fit_Hz"].values
    pred_true = np.array([resonance.kittel_hz("inplane", b, args.angle, true) for b in B])
    fit_err = np.nanmax(np.abs(fit - pred_true)) / 1e6
    n_full = int(ds_win["fmr_full_sweep"].values.sum())

    print(f"field sweep {args.start:g} -> {args.stop:g} mT, {args.points} points, "
          f"{len(f)} bins of {(f[1] - f[0]) / 1e6:g} MHz, "
          f"{args.ms_per_bin:g} ms per bin")
    print(f"  full band : {t_full:6.2f} s")
    print(f"  windowed  : {t_win:6.2f} s   ({t_full / t_win:.1f}x faster; "
          f"{100 * frac:.1f} % of the bins measured, {n_full} full sweeps)")
    print(f"  max |windowed - full| within +-{args.margin:g} MHz of the true line: "
          f"{max(d_meas):.3f} dB on measured bins (the sim draws the same noise in both runs; "
          f"real sweeps differ by their noise), {max(d_fill):.3f} dB on filled ones (the line's tail)")
    print(f"  fitted line vs true Kittel: worst {fit_err:.1f} MHz")
    print(f"  Meff: assumed {args.assumed_meff:g} mT -> estimated {meff:.1f} mT "
          f"(true {true['meff_mT']:g})")

    if not args.no_save:
        _figure(ds_full, ds_win, B, f)
    return {"t_full": t_full, "t_win": t_win, "fraction": frac,
            "max_diff_measured": max(d_meas), "max_diff_filled": max(d_fill),
            "meff": meff, "fit_err_MHz": fit_err}


def _figure(ds_full, ds_win, B, f):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, ax = plt.subplots(1, 3, figsize=(15, 4.5), sharey=True)
    ext = [f[0] / 1e9, f[-1] / 1e9, B[0], B[-1]]
    for a, data, title in ((ax[0], ds_full["fmr"].values, "full band"),
                           (ax[1], ds_win["fmr"].values, "windowed (baseline filled)"),
                           (ax[2], ds_win["fmr_measured"].values.astype(float),
                            "measured mask")):
        a.imshow(data, aspect="auto", origin="lower", extent=ext)
        a.set_title(title)
        a.set_xlabel("frequency (GHz)")
    ax[0].set_ylabel("field (mT)")
    ax[1].plot(ds_win["fmr_fres_fit_Hz"].values / 1e9, B, "w.", ms=3)
    fig.tight_layout()
    fig.savefig(OUT / "window_demo.png", dpi=110)
    print(f"  wrote {OUT / 'window_demo.png'}")


if __name__ == "__main__":
    main()
