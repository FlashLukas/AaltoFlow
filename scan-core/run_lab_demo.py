"""Run a real scan against real instrument services (in simulation or not).

`run_demo.py` scans the toy physics in `build_sim_registry()`. This scans the
actual ZeroMQ services -- the same code path you will use on the bench. On the
lab PC the only difference is that the services were started with `--real`.

Start the magnet service first, in another terminal:

    cd ..\\clMag-control
    uv run scripts/run_service.py

then, here:

    uv run python run_lab_demo.py                 # 1-D field sweep
    uv run python run_lab_demo.py --points 9 --from -60 --to 60

The recipe below is an ordinary Recipe object, so everything that works in the
Scan Builder works here: more axes, other detectors, hooks. Nothing about the
engine changes between the simulated registry and this one -- that is the whole
argument for putting every knob behind a Parameter.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from scan_core.engine import run
from scan_core.instrument import InstrumentError
from scan_core.lab import build_lab_registry
from scan_core.recipe import Recipe

OUT = Path(__file__).parent / "out"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--from", dest="lo", type=float, default=-40.0, help="mT")
    ap.add_argument("--to", dest="hi", type=float, default=40.0, help="mT")
    ap.add_argument("--points", type=int, default=9)
    ap.add_argument("--detector", default="aux_ai1",
                    help="registry id to record at every point")
    args = ap.parse_args()

    print(f"connecting to clMag on {args.host} ...")
    try:
        reg, lab = build_lab_registry(host=args.host, include=("clMag",))
    except InstrumentError as exc:
        print(f"\nFAILED: {exc}\n\nStart it with:  "
              r"cd ..\clMag-control && uv run scripts/run_service.py")
        return 1

    try:
        print("registry built from the live service:")
        for p in reg.settables():
            print(f"  settable  {p.id:16s} {p.label:20s} "
                  f"[{p.limits[0]:.1f}, {p.limits[1]:.1f}] {p.unit}")
        for p in reg.gettables():
            print(f"  detector  {p.id:16s} {p.label:20s} {p.unit}")

        if reg.get(args.detector) is None:
            print(f"\nno such detector {args.detector!r}")
            return 2

        recipe = Recipe(
            name="lab_field_sweep",
            comment="1-D field sweep against the live clMag service",
            axes=[{"type": "linear", "param": "field",
                   "start": args.lo, "stop": args.hi, "num": args.points}],
            detectors=[args.detector, "measured_field", "current"],
            output={"dir": str(OUT), "basename": "lab_field_sweep",
                    "format": "netcdf"},
        )
        # Re-read any manifest whose revision moved before trusting the limits
        # a recipe is validated against. The bounds a registry was built with
        # are a SNAPSHOT, and they move: piezo's ceiling drops 200 -> 160 um on
        # closed loop, kim's armed leash replaces the clamp, clMag's field range
        # IS the calibration. Validating against a stale envelope passes a sweep
        # the instrument will silently clamp, and the dataset then claims
        # coordinates that were never visited.
        moved = lab.refresh_stale(reg, on_warn=lambda m: print(f"  note: {m}"))
        if moved:
            print(f"  limits refreshed for: {', '.join(moved)}")

        errs = recipe.validate(reg)   # catches bad ids and out-of-limit sweeps
        if errs:
            print()
            print("invalid recipe:")
            for e in errs:
                print(f"  - {e}")
            return 3

        print(f"\nsweeping field {args.lo:+g} -> {args.hi:+g} mT "
              f"in {args.points} points, recording {args.detector}\n")
        t0 = time.monotonic()

        def progress(done, total, eta_s):
            print(f"  point {done:3d}/{total}  eta {eta_s:5.1f} s", end="\r")

        ds = run(recipe, reg, on_progress=progress)
        print(f"\n\ndone in {time.monotonic()-t0:.1f} s")

        # The setpoint is the coordinate; the measured field is a recorded
        # channel. Comparing them is the honest check that the magnet really
        # went where the scan said it did.
        for sp, meas in zip(ds.coords["field"].values,
                            ds["measured_field"].values):
            print(f"  set {sp:+8.2f} mT   measured {meas:+8.3f} mT   "
                  f"error {meas - sp:+.3f}")

        OUT.mkdir(exist_ok=True)
        path = OUT / "lab_field_sweep.nc"
        ds.to_netcdf(path)
        print(f"\nwrote {path}")
    finally:
        lab.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
