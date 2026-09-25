"""VNA-FMR with the 2-axis magnet: the old RotSampleInVNA measurement, as a recipe.

The LabVIEW program did this by hand-written sequence:
    go to the reference field -> wait stable -> measure the reference trace
    for every point: set the field -> wait stable -> measure
    field -> 0
and computed u = (S - S_ref) / S_ref afterwards.

Here the same thing is DATA: one recipe with a before-scan routine (set the
reference field, then run the VNA's `take_reference` action), one axis, and an
after-scan routine (field -> 0). The VNA module keeps the reference and serves
`u` itself, so the file holds u and the raw S for every point, and the recipe
inside the file says exactly how it was taken.

Start both services first (Mission Control, or two terminals):

    cd ..\\mag2d-control ; uv run scripts/run_service.py
    cd ..\\vna-control   ; uv run scripts/run_service.py

then, here:

    uv run python run_mag2d_vna_demo.py                              # field scan at 45 deg
    uv run python run_mag2d_vna_demo.py --mode angle --field 40      # angular scan at 40 mT
    uv run python run_mag2d_vna_demo.py --mode angle --hk 8          # simulated anisotropy

Writes out/mag2d_vna_<mode>.nc and a figure of |S/S_ref| with the simulator's
Kittel line on top (the line is only known when the VNA is simulated).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np

from scan_core.data import as_complex, load
from scan_core.engine import run
from scan_core.instrument import InstrumentError
from scan_core.lab import build_lab_registry
from scan_core.recipe import Recipe

OUT = Path(__file__).parent / "out"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--mode", choices=["field", "angle"], default="field")
    ap.add_argument("--angle", type=float, default=45.0, help="field scan: angle (deg)")
    ap.add_argument("--field", type=float, default=40.0, help="angle scan: field (mT)")
    ap.add_argument("--from", dest="lo", type=float, default=None,
                    help="scan start (mT or deg; default 70 mT / 0 deg)")
    ap.add_argument("--to", dest="hi", type=float, default=None,
                    help="scan stop (default 10 mT / 180 deg)")
    ap.add_argument("--n", type=int, default=13, help="number of scan points")
    ap.add_argument("--ref-field", type=float, default=150.0, help="reference field (mT)")
    ap.add_argument("--ref-angle", type=float, default=45.0, help="reference angle (deg)")
    ap.add_argument("--points", type=int, default=401, help="VNA points per sweep")
    ap.add_argument("--hk", type=float, default=None,
                    help="SIMULATED VNA only: uniaxial anisotropy field (mT)")
    ap.add_argument("--mag2d-port", type=int, default=None,
                    help="mag2d command port if not the launcher's (status = port + 1)")
    ap.add_argument("--vna-port", type=int, default=None,
                    help="vna command port if not the launcher's (status = port + 1)")
    args = ap.parse_args()

    endpoints = {name: (args.host, port, port + 1)
                 for name, port in (("mag2d", args.mag2d_port), ("vna", args.vna_port)) if port}
    print(f"connecting to mag2d and vna on {args.host} ...")
    try:
        reg, lab = build_lab_registry(host=args.host, include=("mag2d", "vna"), prefix=True,
                                      endpoints=endpoints or None, on_warn=lambda m: None)
    except InstrumentError as exc:
        print(f"\nFAILED: {exc}\n\nStart both services first (see the top of this file).")
        return 1

    try:
        vna, mag = lab["vna"], lab["mag2d"]
        # The magnet must be energised: after a fault the output stays off on
        # purpose, and a scan would then wait 30 s per point for a field that
        # can never come.
        if not mag.status().get("energized"):
            print("mag2d output is off -- switching it on")
            reg.get("mag2d.output").set(1)

        # The VNA files the field it HEARS with every trace. Make sure it is
        # listening to this magnet, and actually hearing it, before measuring.
        vna.command("set_field_source", source="mag2d")
        vna.wait_until(lambda st: st.get("field_source_set") == "mag2d" and st.get("field_ok"),
                       timeout_s=10.0, what="the VNA to hear mag2d's field")
        if args.hk is not None:
            vna.command("set_sample", name="hk_mT", value=args.hk)

        # Fix the sweep BEFORE the scan (a mid-scan change makes ragged traces,
        # and would also invalidate the reference). Continuous sweeping off, so
        # the VNA sweeps only when the scan asks.
        reg.get("vna.points").set(args.points)
        reg.get("vna.continuous").set(0)

        if args.mode == "field":
            lo = 70.0 if args.lo is None else args.lo
            hi = 10.0 if args.hi is None else args.hi
            fixed = {"mag2d.angle": args.angle}
            axis = {"type": "linear", "param": "mag2d.field", "start": lo, "stop": hi,
                    "num": args.n}
        else:
            lo = 0.0 if args.lo is None else args.lo
            hi = 180.0 if args.hi is None else args.hi
            fixed = {"mag2d.field": args.field}
            axis = {"type": "linear", "param": "mag2d.angle", "start": lo, "stop": hi,
                    "num": args.n}

        detectors = ["vna.u", "vna.s", "vna.sweep_field", "vna.sweep_angle",
                     "vna.sweep_field_ok", "mag2d.measured_magnitude", "mag2d.measured_angle"]
        simulated = reg.get("vna.f_res_model") is not None
        if simulated:
            detectors.append("vna.f_res_model")

        recipe = Recipe(
            name=f"mag2d_vna_{args.mode}",
            comment="VNA-FMR, reference taken before the scan (RotSampleInVNA successor)",
            fixed=fixed, axes=[axis], detectors=detectors,
            hooks=[
                {"when": "before_scan", "action": "call",
                 "args": {"set": {"mag2d.field": args.ref_field,
                                  "mag2d.angle": args.ref_angle},
                          "action": "vna.take_reference"}},
                {"when": "after_scan", "action": "call",
                 "args": {"set": {"mag2d.field": 0.0}}},
            ])
        errs = recipe.validate(reg)
        if errs:
            print("recipe is not valid:\n  " + "\n  ".join(errs))
            return 1

        t0 = time.monotonic()
        ds = run(recipe, reg,
                 on_log=lambda msg: print(f"\n  [routine] {msg}"),
                 on_progress=lambda d, n, eta: print(
                     f"\r  {d}/{n} points, {eta:5.0f} s left", end="", flush=True))
        print(f"\ndone in {time.monotonic() - t0:.0f} s")
        ref = vna.status().get("reference") or {}
        print(f"reference: acq #{ref.get('acq_id')} at {ref.get('field_mT'):.2f} mT, "
              f"{ref.get('angle_deg'):.1f} deg")
    except (InstrumentError, TimeoutError) as exc:
        print(f"\nFAILED: {exc}")
        return 1
    finally:
        lab.close()

    OUT.mkdir(exist_ok=True)
    nc = OUT / f"mag2d_vna_{args.mode}.nc"
    ds.to_netcdf(nc, engine="h5netcdf")
    print(f"wrote {nc}")

    # ---- read the file back, as anyone analysing it later would -------------
    back = load(str(nc))
    u = as_complex(back, "vna.u").values
    f = back.coords["vna.freq"].values                       # GHz
    x_name = "mag2d.field" if args.mode == "field" else "mag2d.angle"
    x = back.coords[x_name].values
    ratio_dB = 20 * np.log10(np.abs(1 + u))                  # |S / S_ref|
    if not np.all(back["vna.sweep_field_ok"].values == 1):
        print("WARNING: some sweeps did not hear the magnet (vna.sweep_field_ok = 0)")

    print(f"\n  {'set':>8} {'|B| heard':>10} {'angle':>7} {'dip |S/Sref|':>13} {'Kittel':>8}")
    worst = 0.0
    for i in range(len(x)):
        dip = f[np.argmin(ratio_dB[i])]
        # already in GHz: the manifest's `scale` converts the wire's Hz on read
        kit = back["vna.f_res_model"].values[i] if simulated else np.nan
        # only compare where the line is inside the band and deep enough to see
        seen = ratio_dB[i].min() < -0.5 and np.isfinite(kit) and f[0] < kit < f[-1]
        if seen:
            worst = max(worst, abs(dip - kit))
        print(f"  {x[i]:8.2f} {back['vna.sweep_field'].values[i]:10.2f} "
              f"{back['vna.sweep_angle'].values[i]:7.1f} {dip:13.4f} {kit:8.4f}"
              f"{'' if seen else '   (no line in band)'}")
    if simulated:
        print(f"worst |dip - Kittel| where a line is visible: {worst * 1e3:.1f} MHz")

    from matplotlib.figure import Figure
    fig = Figure(figsize=(7.5, 4.6), dpi=110)
    ax = fig.subplots()
    m = ax.pcolormesh(x, f, ratio_dB.T, shading="nearest", cmap="magma", vmin=-4, vmax=0.5)
    fig.colorbar(m, ax=ax, label="|S / S_ref| (dB)")
    if simulated:
        ax.plot(x, back["vna.f_res_model"].values, "c--", lw=1, label="Kittel (model)")
        ax.legend(loc="upper right")
    ax.set_ylim(f[0], f[-1])
    ax.set_xlabel("field (mT)" if args.mode == "field" else "angle (deg)")
    ax.set_ylabel("frequency (GHz)")
    ax.set_title(f"reference {args.ref_field:g} mT at {args.ref_angle:g} deg")
    fig.tight_layout()
    png = OUT / f"mag2d_vna_{args.mode}.png"
    fig.savefig(png)
    print(f"wrote {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
