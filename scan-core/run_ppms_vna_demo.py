"""VNA-FMR in the DynaCool: the old QD_VNA_Integration measurement, as a recipe.

The LabVIEW program (VNA_GUI.vi + QDInstrument_ControlField.vi, talking through
global variables) did, by hand-written sequence:
    go to "Field for reference" -> wait until reached -> measure the reference
    for every field in the array: set it -> wait until reached (+ a fixed 3 s)
                                  -> measure one spectrum
    "Switch off field after sweep?" -> field 0
and showed (S - S_ref)/S_ref, S - S_ref (polar) or the raw S.

Here the same thing is DATA: one recipe with a before-scan routine (set the
reference field, then run the VNA's `take_reference` action), one field axis,
an optional fixed temperature, and an after-scan routine (field -> 0). The
DynaCool module decides "reached" (within tolerance, MultiVu holding, for the
hold time -- the old 3 s), the VNA files the field it HEARD with every trace,
and the file holds u and the raw S for every point.

Start both services first (Mission Control, or two terminals):

    cd ..\\ppms-control ; uv run scripts/run_service.py          # add --real for MultiVu
    cd ..\\vna-control  ; uv run scripts/run_service.py --field ppms
                                                  # add --real --driver cmt for the C1209

then, here:

    uv run python run_ppms_vna_demo.py                       # 70 -> 10 mT, reference at 150 mT
    uv run python run_ppms_vna_demo.py --from -50 --to 50 --n 51 --ref-field -50
                                                             # the old panel's defaults
    uv run python run_ppms_vna_demo.py --temperature 300     # also hold T during the scan

Writes out/ppms_vna.nc and a figure of |S/S_ref| (with the simulator's Kittel
line on top when the VNA is simulated).
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
    ap.add_argument("--from", dest="lo", type=float, default=70.0, help="first field (mT)")
    ap.add_argument("--to", dest="hi", type=float, default=10.0, help="last field (mT)")
    ap.add_argument("--n", type=int, default=13, help="number of field points")
    ap.add_argument("--ref-field", type=float, default=150.0, help="reference field (mT)")
    ap.add_argument("--temperature", type=float, default=None,
                    help="hold this temperature (K) during the scan (default: leave it)")
    ap.add_argument("--keep-field", action="store_true",
                    help="do NOT return the field to 0 after the scan")
    ap.add_argument("--points", type=int, default=401, help="VNA points per sweep")
    ap.add_argument("--ppms-port", type=int, default=None,
                    help="ppms command port if not the launcher's (status = port + 1)")
    ap.add_argument("--vna-port", type=int, default=None,
                    help="vna command port if not the launcher's (status = port + 1)")
    args = ap.parse_args()

    endpoints = {name: (args.host, port, port + 1)
                 for name, port in (("ppms", args.ppms_port), ("vna", args.vna_port)) if port}
    print(f"connecting to ppms and vna on {args.host} ...")
    try:
        reg, lab = build_lab_registry(host=args.host, include=("ppms", "vna"), prefix=True,
                                      endpoints=endpoints or None, on_warn=lambda m: None)
    except InstrumentError as exc:
        print(f"\nFAILED: {exc}\n\nStart both services first (see the top of this file).")
        return 1

    try:
        vna = lab["vna"]
        # The VNA files the field it HEARS with every trace: make sure it is
        # listening to the DynaCool, and actually hearing it, before measuring.
        vna.command("set_field_source", source="ppms")
        vna.wait_until(lambda st: st.get("field_source_set") == "ppms" and st.get("field_ok"),
                       timeout_s=10.0, what="the VNA to hear the DynaCool's field")

        # Fix the sweep BEFORE the scan (a mid-scan change makes ragged traces
        # and invalidates the reference); sweep only when the scan asks.
        reg.get("vna.points").set(args.points)
        reg.get("vna.continuous").set(0)

        fixed = {}
        if args.temperature is not None:
            fixed["ppms.temperature"] = args.temperature
        axis = {"type": "linear", "param": "ppms.field", "start": args.lo, "stop": args.hi,
                "num": args.n}
        detectors = ["vna.u", "vna.s", "vna.sweep_field", "vna.sweep_field_ok",
                     "ppms.measured_field", "ppms.measured_temperature"]
        simulated = reg.get("vna.f_res_model") is not None
        if simulated:
            detectors.append("vna.f_res_model")
        hooks = [{"when": "before_scan", "action": "call",
                  "args": {"set": {"ppms.field": args.ref_field},
                           "action": "vna.take_reference"}}]
        if not args.keep_field:
            hooks.append({"when": "after_scan", "action": "call",
                          "args": {"set": {"ppms.field": 0.0}}})

        recipe = Recipe(
            name="ppms_vna",
            comment="VNA-FMR in the DynaCool, reference before the scan "
                    "(QD_VNA_Integration successor)",
            fixed=fixed, axes=[axis], detectors=detectors, hooks=hooks)
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
        print(f"reference: acq #{ref.get('acq_id')} at {ref.get('field_mT'):.2f} mT")
    except (InstrumentError, TimeoutError) as exc:
        print(f"\nFAILED: {exc}")
        return 1
    finally:
        lab.close()

    OUT.mkdir(exist_ok=True)
    nc = OUT / "ppms_vna.nc"
    ds.to_netcdf(nc, engine="h5netcdf")
    print(f"wrote {nc}")

    # ---- read the file back, as anyone analysing it later would -------------
    back = load(str(nc))
    u = as_complex(back, "vna.u").values
    f = back.coords["vna.freq"].values                       # GHz
    x = back.coords["ppms.field"].values
    ratio_dB = 20 * np.log10(np.abs(1 + u))                  # |S / S_ref|
    if not np.all(back["vna.sweep_field_ok"].values == 1):
        print("WARNING: some sweeps did not hear the DynaCool (vna.sweep_field_ok = 0)")

    print(f"\n  {'set':>8} {'measured':>9} {'VNA heard':>10} {'T (K)':>8} "
          f"{'dip |S/Sref|':>13} {'Kittel':>8}")
    worst_f = worst_heard = 0.0
    for i in range(len(x)):
        dip = f[np.argmin(ratio_dB[i])]
        kit = back["vna.f_res_model"].values[i] if simulated else np.nan
        seen = ratio_dB[i].min() < -0.5 and np.isfinite(kit) and f[0] < kit < f[-1]
        if seen:
            worst_f = max(worst_f, abs(dip - kit))
        heard = back["vna.sweep_field"].values[i]
        worst_heard = max(worst_heard, abs(heard - x[i]))
        print(f"  {x[i]:8.2f} {back['ppms.measured_field'].values[i]:9.3f} {heard:10.3f} "
              f"{back['ppms.measured_temperature'].values[i]:8.3f} {dip:13.4f} {kit:8.4f}"
              f"{'' if seen else '   (no line in band)'}")
    print(f"worst |field the VNA filed - setpoint|: {worst_heard:.3f} mT")
    if simulated:
        print(f"worst |dip - Kittel| where a line is visible: {worst_f * 1e3:.1f} MHz")

    from matplotlib.figure import Figure
    fig = Figure(figsize=(7.5, 4.6), dpi=110)
    ax = fig.subplots()
    m = ax.pcolormesh(x, f, ratio_dB.T, shading="nearest", cmap="magma", vmin=-4, vmax=0.5)
    fig.colorbar(m, ax=ax, label="|S / S_ref| (dB)")
    if simulated:
        ax.plot(x, back["vna.f_res_model"].values, "c--", lw=1, label="Kittel (model)")
        ax.legend(loc="upper right")
    ax.set_ylim(f[0], f[-1])
    ax.set_xlabel("field (mT)")
    ax.set_ylabel("frequency (GHz)")
    ax.set_title(f"DynaCool, reference at {args.ref_field:g} mT")
    fig.tight_layout()
    png = OUT / "ppms_vna.png"
    fig.savefig(png)
    print(f"wrote {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
