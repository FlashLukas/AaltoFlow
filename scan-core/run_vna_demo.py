"""VNA-FMR map from two live services: the magnet and the (simulated) VNA.

The VNA module simulates S21 through a waveguide with a YIG film, and its film
sits in the field the MAGNET SERVICE measures. So this is a real two-instrument
scan: scan-core sets the field, waits for clMag to settle, triggers a fresh VNA
sweep, waits for that sweep, and files the complex trace under that field.

Start both services first (Mission Control, or two terminals):

    cd ..\\clMag-control ; uv run scripts/run_service.py
    cd ..\\vna-control   ; uv run scripts/run_service.py

then, here:

    uv run python run_vna_demo.py                         # -90 .. 90 mT, 37 fields
    uv run python run_vna_demo.py --from 0 --to 90 --fields 19 --points 1601

Writes out/vna_fmr_map.nc (open it in the Data tab / viewer: pick vna.s,
|z|, X = clMag.field, Y = vna.freq) and out/vna_fmr_map.png, which shows the
raw |S21| next to |S21| divided by the trace at the field nearest zero -- the
usual way to strip the cables' loss and ripple from VNA-FMR data.

The VNA's field source defaults to the 2-axis magnet (mag2d) since 2026-09-16;
this demo drives clMag, so it switches the VNA to clMag first and checks the
VNA actually hears it before sweeping.
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
    ap.add_argument("--from", dest="lo", type=float, default=-90.0, help="mT")
    ap.add_argument("--to", dest="hi", type=float, default=90.0, help="mT")
    ap.add_argument("--fields", type=int, default=37, help="number of field points")
    ap.add_argument("--points", type=int, default=801, help="VNA points per sweep")
    ap.add_argument("--clmag-port", type=int, default=None,
                    help="clMag command port if not the launcher's (status = port + 1)")
    ap.add_argument("--vna-port", type=int, default=None,
                    help="vna command port if not the launcher's (status = port + 1)")
    args = ap.parse_args()

    # Without port flags the ports come from module discovery, i.e. what the
    # launcher uses on this PC.
    endpoints = {name: (args.host, port, port + 1)
                 for name, port in (("clMag", args.clmag_port), ("vna", args.vna_port)) if port}
    print(f"connecting to clMag and vna on {args.host} ...")
    try:
        reg, lab = build_lab_registry(host=args.host, include=("clMag", "vna"), prefix=True,
                                      endpoints=endpoints or None, on_warn=lambda m: None)
    except InstrumentError as exc:
        print(f"\nFAILED: {exc}\n\nStart both services first (see the top of this file).")
        return 1

    try:
        # The VNA must read the field of the magnet THIS scan drives. The source
        # is an enum, which the registry keeps read-only (a Settable is
        # numeric), so it is set with a plain command -- then wait until the
        # VNA reports it is listening to clMag AND hearing it: a map filed
        # against a field the VNA never saw would look fine and be wrong.
        vna = lab["vna"]
        vna.command("set_field_source", source="clMag")
        vna.wait_until(lambda st: st.get("field_source_set") == "clMag" and st.get("field_ok"),
                       timeout_s=10.0, what="the VNA to hear clMag's field")
        # Fix the sweep BEFORE the scan: changing it mid-scan would make the
        # traces ragged, which the engine refuses. Continuous sweeping off, so
        # the VNA only sweeps when the scan asks.
        reg.get("vna.points").set(args.points)
        reg.get("vna.continuous").set(0)
        fields = np.linspace(args.lo, args.hi, args.fields)
        recipe = Recipe(name="vna_fmr_map",
                        axes=[{"type": "array", "param": "clMag.field",
                               "values": fields.tolist()}],
                        detectors=["vna.s", "vna.dip_freq", "vna.dip_depth", "vna.sweep_field",
                                   "vna.sweep_field_ok", "clMag.measured_field"])
        recipe.validate(reg)
        t0 = time.monotonic()
        ds = run(recipe, reg, on_progress=lambda d, n, eta: print(
            f"\r  {d}/{n} fields, {eta:5.0f} s left", end="", flush=True))
        print(f"\ndone in {time.monotonic() - t0:.0f} s")
    except (InstrumentError, TimeoutError) as exc:
        print(f"\nFAILED: {exc}")
        return 1
    finally:
        lab.close()

    OUT.mkdir(exist_ok=True)
    nc = OUT / "vna_fmr_map.nc"
    ds.to_netcdf(nc, engine="h5netcdf")
    print(f"wrote {nc}")

    back = load(str(nc))
    z = as_complex(back, "vna.s").values
    f = back.coords["vna.freq"].values
    H = back["vna.sweep_field"].values
    if not np.all(back["vna.sweep_field_ok"].values == 1):
        # the VNA could not hear the magnet: those traces used a fallback field
        print("WARNING: some sweeps did not see a live field (vna.sweep_field_ok = 0)")
    # a dip of a few tenths of a dB is ripple or noise: no line in the band
    for h, dip, depth in zip(H, back["vna.dip_freq"].values, back["vna.dip_depth"].values):
        note = "" if depth < -0.5 else "   (no line in the band)"
        print(f"  {h:8.2f} mT   dip {dip:.4f} GHz  {depth:6.2f} dB{note}")

    from matplotlib.figure import Figure
    ref = z[np.argmin(np.abs(H))]
    fig = Figure(figsize=(11, 4.4), dpi=110)
    ax1, ax2 = fig.subplots(1, 2)
    for ax, data, title, lim in (
            (ax1, z, "raw |S21| (dB)", None),
            (ax2, z / ref, f"|S21 / S21({H[np.argmin(np.abs(H))]:.1f} mT)| (dB)", (-4, 0.3))):
        m = ax.pcolormesh(H, f, 20 * np.log10(np.abs(data)).T, shading="nearest", cmap="magma",
                          **({"vmin": lim[0], "vmax": lim[1]} if lim else {}))
        fig.colorbar(m, ax=ax)
        ax.set_title(title)
        ax.set_xlabel("field (mT)")
        ax.set_ylabel("frequency (GHz)")
    fig.tight_layout()
    png = OUT / "vna_fmr_map.png"
    fig.savefig(png)
    print(f"wrote {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
