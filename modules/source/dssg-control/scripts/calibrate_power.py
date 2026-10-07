"""Measure the POWER CALIBRATION of the SG12000L on the bench (once per unit).

WHY: the attenuator's 0.5 dB steps are exact at 1-2 GHz but not above ~4 GHz
(10 GHz: "-10.5 dBm" is only 0.32 dB below "-10.0", "-13.5" is 0.56 dB short of
its nominal 3.5 dB), and the vernier's dB per count depends on frequency AND
power. This script measures both with the Signal Hound spectrum analyser and
writes dssg_power_calibration.json, which the dssg service loads at start
(src/dssg/vernier_cal.py explains how it is used). After that, fine power
delivers the level asked for to ~0.05 dB at every frequency.

RUN IT FROM SCAN-CORE'S ENVIRONMENT (it uses scan-core's engine and registry;
the dssg package is not needed there, only its vernier_cal.py, imported from
this folder):

    cd scan-core
    uv run python ../modules/source/dssg-control/scripts/calibrate_power.py --quick
    uv run python ../modules/source/dssg-control/scripts/calibrate_power.py --quick --yes
    uv run python ../modules/source/dssg-control/scripts/calibrate_power.py --yes

Without --yes it only prints the plan (points, a rough duration) and changes
nothing. --quick measures 1, 4 and 10 GHz only, for a first try.

Before you start
  * the dssg AND signalhound services are running (Mission Control, or each
    module's scripts/run_service.py --real);
  * the generator's output goes into the analyser through a PAD (the bench
    had 30 dB): +5 dBm out must stay well below the analyser's limit. Set
    --ref-level to suit the pad (default -10 dBm at the analyser);
  * nothing else drives either instrument meanwhile.

What it does
  1. reads the generator's state (RF, frequency, power, vernier, fine power)
     and the analyser's sweep settings, to put them back at the end;
  2. switches the generator to STEP mode (hardware.fine_power = false), so a
     power set goes straight to the attenuator and the vernier is a control;
  3. scan A, attenuator linearity: frequency (generator and analyser centre
     together, a `zip` axis) x every 0.5 dB step, vernier 0, RF on;
  4. scan B, vernier slope: frequency x power (-20, -10, 0 dBm) x vernier
     (-8, -4, 0, +4, +8 counts);
  5. RF OFF (an after_scan routine of each scan, and again at the end
     whatever happens -- error or Ctrl+C included), then the start state
     back: fine power, frequency, power, vernier, the analyser's settings.
     The RF output is left OFF unless you pass --restore-rf (then it is
     switched back on if it was on at the start and the run succeeded);
  6. builds the calibration (vernier_cal.build_calibration) and writes it.
     The step deviations are RELATIVE to the -10 dBm step, so the pad, the
     cables and the analyser's flatness cancel; the absolute level at -10 dBm
     remains the generator's own factory calibration.

Output (default: dssg_power_calibration.json in the module folder, which the
service loads; *calibration*.json is gitignored in this module -- it is lab
data of ONE unit -- and the installer and Mission Control's settings export
keep it). Both raw scans are kept as .nc next to it (named in the JSON).
Restart the dssg service afterwards to load the new file.

Printed text is ASCII (gotcha #14).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import math
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_DIR = os.path.abspath(os.path.join(_HERE, ".."))
_SRC = os.path.join(MODULE_DIR, "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, _SRC)

from dssg import vernier_cal  # noqa: E402  (stdlib only: works in scan-core's env)

#: the frequencies measured (GHz); clipped to what BOTH instruments can do
FREQS_GHZ = (0.1, 0.25, 0.5, 1, 1.5, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)
QUICK_FREQS_GHZ = (1, 4, 10)
#: scan B: the powers and vernier counts the slope is fitted over. +-8 counts
#: covers the +-6 the fine power ever uses, in the vernier's linear part
SLOPE_POWERS = (-20.0, -10.0, 0.0)
SLOPE_COUNTS = (-8, -4, 0, 4, 8)
STEP_DB = 0.5
#: analyser settings: a 1 MHz window around the tone, 10 kHz RBW -- a clean
#: CW peak far above the floor, and a sweep of a few tens of ms
SPAN_MHZ, RBW_KHZ, VBW_KHZ = 1.0, 10.0, 10.0
#: a peak further than this from the generator's frequency is not our tone
#: (RF not on, a spur, no signal): that reading is dropped, not used
PEAK_TOL_HZ = 200e3
#: rough time per point (set, settle, one analyser sweep): only for the ETA
SEC_PER_POINT = 0.6

# registry ids (scan_core.lab.build_lab_registry(..., prefix=True))
D_FREQ, D_POWER, D_VERNIER, D_RF = ("dssg.frequency", "dssg.power",
                                    "dssg.vernier", "dssg.rf_on")
S_CENTER, S_SPAN, S_RBW, S_VBW, S_REF = ("signalhound.center", "signalhound.span",
                                         "signalhound.rbw", "signalhound.vbw",
                                         "signalhound.ref_level")
S_LEVEL, S_PEAKF, S_OVER = ("signalhound.peak_level", "signalhound.peak_freq",
                            "signalhound.overloaded")


# ---- pure planning (tested without any service) --------------------------------

def plan_freqs(freqs_GHz, dssg_MHz: tuple[float, float],
               sa_GHz: tuple[float, float]) -> list[float]:
    """The frequencies (GHz) inside BOTH instruments' ranges."""
    lo = max(dssg_MHz[0] / 1e3, sa_GHz[0])
    hi = min(dssg_MHz[1] / 1e3, sa_GHz[1])
    return [float(f) for f in freqs_GHz if lo - 1e-12 <= f <= hi + 1e-12]


def power_steps(lo: float, hi: float, max_power: float, step: float = STEP_DB) -> list[float]:
    """Every attenuator step from lo to min(hi, max_power), inclusive."""
    top = min(hi, max_power)
    k0, k1 = math.ceil(lo / step - 1e-9), math.floor(top / step + 1e-9)
    return [round(k * step, 6) for k in range(k0, k1 + 1)]


def slope_powers(lo: float, hi: float, max_power: float) -> list[float]:
    """SLOPE_POWERS that the generator may make (clipped, not moved)."""
    top = min(hi, max_power)
    return [p for p in SLOPE_POWERS if lo - 1e-9 <= p <= top + 1e-9]


def build_recipes(freqs_GHz, steps, spowers, counts=SLOPE_COUNTS, *,
                  ref_level: float = -10.0, name: str = "dssg_power_cal") -> tuple[dict, dict]:
    """The two scans as Recipe dicts (scan_core.recipe.Recipe.from_dict).

    The generator (MHz) and the analyser's centre (GHz) move TOGETHER on one
    `zip` axis: one dimension, two parameters in lockstep. RF on is a fixed
    condition -- set last, after the analyser, at the lowest power (the
    script parks the generator there first) -- and an after_scan routine
    switches it off again, also after an abort or an error.
    """
    freqs_GHz = [float(f) for f in freqs_GHz]
    fzip = {"type": "zip", "name": "frequency",
            "members": [{"param": D_FREQ, "values": [f * 1e3 for f in freqs_GHz]},
                        {"param": S_CENTER, "values": list(freqs_GHz)}]}
    sa_fixed = {S_SPAN: SPAN_MHZ, S_RBW: RBW_KHZ, S_VBW: VBW_KHZ, S_REF: float(ref_level)}
    dets = [S_LEVEL, S_PEAKF, S_OVER]
    rf_off = [{"when": "after_scan", "action": "call", "args": {"set": {D_RF: 0}}}]
    common = {"detectors": dets, "hooks": rf_off, "zigzag": False,
              "settle": {"default_timeout_s": 30.0},
              "output": {"dir": ".", "basename": name, "format": "netcdf"}}
    a = {"name": f"{name}_A_attenuator",
         "comment": "dssg power calibration, scan A: every attenuator step at "
                    "vernier 0 (step mode), level on the spectrum analyser",
         "fixed": {D_VERNIER: 0, **sa_fixed, D_RF: 1},
         "axes": [fzip, {"type": "array", "param": D_POWER,
                         "values": [float(p) for p in steps]}],
         **common}
    b = {"name": f"{name}_B_vernier",
         "comment": "dssg power calibration, scan B: vernier slope at a few "
                    "powers (step mode), level on the spectrum analyser",
         "fixed": {**sa_fixed, D_RF: 1},
         "axes": [dict(fzip, members=[dict(m) for m in fzip["members"]]),
                  {"type": "array", "param": D_POWER, "values": [float(p) for p in spowers]},
                  {"type": "array", "param": D_VERNIER, "values": [int(c) for c in counts]}],
         **common}
    return a, b


def n_points(recipe: dict) -> int:
    """Points of a recipe dict built by build_recipes (product of the axes)."""
    n = 1
    for ax in recipe["axes"]:
        n *= len(ax["members"][0]["values"]) if ax["type"] == "zip" else len(ax["values"])
    return n


def parse_idn(idn: str) -> tuple[str, str]:
    """(model, firmware) from *IDN? -- "maker,model,serial,firmware". The
    SERIAL is deliberately dropped: the file may travel (CLAUDE.md)."""
    parts = [p.strip() for p in str(idn or "").split(",")]
    model = parts[1] if len(parts) > 1 else (parts[0] if parts and parts[0] else "unknown")
    firmware = parts[3] if len(parts) > 3 else ""
    return model, firmware


def clean_levels(levels, peak_GHz, overloaded, expect_GHz) -> list:
    """Drop (NaN) readings that are not our tone: an overloaded analyser, or
    a peak further than PEAK_TOL_HZ from the generator's frequency. Works on
    nested lists of the same shape; returns nested lists of floats."""
    if isinstance(levels, (list, tuple)):
        return [clean_levels(l, p, o, expect_GHz) for l, p, o in
                zip(levels, peak_GHz, overloaded)]
    lv = float(levels)
    if (not math.isfinite(lv) or bool(overloaded)
            or not math.isfinite(float(peak_GHz))
            or abs(float(peak_GHz) - expect_GHz) * 1e9 > PEAK_TOL_HZ):
        return float("nan")
    return lv


# ---- the measurement -------------------------------------------------------------

def _endpoints(args) -> dict:
    out = {}
    for name, port in (("dssg", args.dssg_port), ("signalhound", args.sa_port)):
        if port:
            out[name] = (args.host, int(port), int(port) + 1)
    return out


def _connect_ctl(args):
    """Our OWN connections, for reading the start state and putting it back.
    Kept apart from the scans' registry, so a scan interrupted half-way (a
    REQ socket left mid-request) cannot stop the restore."""
    from scan_core.lab import Lab
    ctl = Lab()
    eps = _endpoints(args)
    for name in ("dssg", "signalhound"):
        if name in eps:
            h, c, p = eps[name]
            ctl.connect(name, host=h, cmd_port=c, pub_port=p)
        else:
            ctl.connect(name, host=args.host)
    return ctl


def _registry(args):
    from scan_core.lab import build_lab_registry
    return build_lab_registry(host=args.host, include=("dssg", "signalhound"),
                              prefix=True, endpoints=_endpoints(args) or None,
                              on_warn=lambda m: print(f"  note: {m}"))


def _limits(reg, pid):
    p = reg.get(pid)
    if p is None:
        raise RuntimeError(f"{pid} is not in the registry -- is the service the "
                           f"right version?")
    return tuple(float(v) for v in p.limits)


def _restore(ctl, start: dict, sa_start: dict, rf_back: bool) -> None:
    """RF off FIRST, then the start state back. Every step on its own: one
    failure must not stop the others (least of all the RF off)."""
    dssg, sa = ctl["dssg"], ctl["signalhound"]

    def step(what, fn):
        try:
            fn()
        except Exception as exc:          # report, carry on
            print(f"  RESTORE FAILED ({what}): {exc}")

    step("RF off", lambda: dssg.command("set_rf", on=False))
    step("fine power", lambda: dssg.command(
        "set_config", config={"hardware": {"fine_power": bool(start["fine_power_cfg"])}}))
    step("frequency", lambda: dssg.command("set_frequency",
                                           frequency_Hz=float(start["frequency_Hz"])))
    if start["fine_power_cfg"]:
        # the asked power, re-split by the service (with its calibration)
        step("power", lambda: dssg.command("set_power", power_dBm=float(start["power_dBm"])))
    else:
        step("power", lambda: dssg.command("set_power",
                                           power_dBm=float(start["attenuator_dBm"])))
        if start.get("has_vernier"):
            step("vernier", lambda: dssg.command("set_vernier",
                                                 vernier=int(start["vernier"])))
    # analyser: RBW before VBW (VBW may not exceed RBW), centre before span
    # (a centre change may narrow the span)
    for key, verb in (("rbw_Hz", "set_rbw"), ("vbw_Hz", "set_vbw"),
                      ("center_Hz", "set_center"), ("span_Hz", "set_span"),
                      ("ref_level_dBm", "set_ref_level")):
        if sa_start.get(key) is not None:
            step(f"analyser {key}", lambda k=key, v=verb: sa.command(v, **{k: sa_start[k]}))
    if rf_back:
        step("RF back on", lambda: dssg.command("set_rf", on=True))


def _levels(ds, expect_GHz_per_row):
    """Nested lists of cleaned levels, first index = frequency."""
    lv, pk, ov = (ds[S_LEVEL].values.tolist(), ds[S_PEAKF].values.tolist(),
                  ds[S_OVER].values.tolist())
    return [clean_levels(lv[i], pk[i], ov[i], f) for i, f in enumerate(expect_GHz_per_row)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Measure the SG12000L power calibration "
                                             "(attenuator steps + vernier slope)")
    ap.add_argument("--out", default=os.path.join(MODULE_DIR, vernier_cal.DEFAULT_FILE),
                    help="calibration JSON to write (default: the module folder's "
                         "dssg_power_calibration.json, which the service loads)")
    ap.add_argument("--quick", action="store_true",
                    help=f"only {', '.join(f'{f:g}' for f in QUICK_FREQS_GHZ)} GHz (a first try)")
    ap.add_argument("--yes", action="store_true",
                    help="really measure (without it: print the plan, change nothing)")
    ap.add_argument("--max-power", type=float, default=5.0,
                    help="highest generator power used, dBm (default +5)")
    ap.add_argument("--ref-level", type=float, default=-10.0,
                    help="analyser reference level, dBm (default -10: +5 dBm through "
                         "a 30 dB pad arrives at -25)")
    ap.add_argument("--restore-rf", action="store_true",
                    help="switch the RF back on at the end if it was on at the start "
                         "(default: leave it OFF)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--dssg-port", type=int, default=None,
                    help="dssg command port if not the launcher's (status = port + 1)")
    ap.add_argument("--sa-port", type=int, default=None,
                    help="signalhound command port if not the launcher's")
    args = ap.parse_args(argv)

    try:
        from scan_core.engine import run
        from scan_core.recipe import Recipe
    except ImportError:
        print("scan_core is not importable: run this from scan-core's environment "
              "(cd scan-core; uv run python ../modules/source/dssg-control/scripts/"
              "calibrate_power.py ...)")
        return 2

    # ---- plan (reads the live limits, changes nothing) --------------------
    print(f"connecting to dssg and signalhound on {args.host} ...")
    reg, lab = _registry(args)
    try:
        f_lim = _limits(reg, D_FREQ)                 # MHz
        c_lim = _limits(reg, S_CENTER)               # GHz
        p_lim = _limits(reg, D_POWER)                # dBm
    finally:
        lab.close()
    freqs = plan_freqs(QUICK_FREQS_GHZ if args.quick else FREQS_GHZ, f_lim, c_lim)
    steps = power_steps(p_lim[0], p_lim[1], args.max_power)
    spows = slope_powers(p_lim[0], p_lim[1], args.max_power)
    if not freqs or len(steps) < 2:
        print(f"nothing to measure: frequencies {freqs}, steps {steps}")
        return 1
    rec_a, rec_b = build_recipes(freqs, steps, spows, ref_level=args.ref_level)
    na, nb = n_points(rec_a), n_points(rec_b)
    print(f"plan: {len(freqs)} frequencies ({', '.join(f'{f:g}' for f in freqs)} GHz)")
    print(f"  scan A: {len(steps)} attenuator steps {steps[0]:g}..{steps[-1]:g} dBm "
          f"-> {na} points")
    print(f"  scan B: powers {', '.join(f'{p:g}' for p in spows)} dBm x vernier "
          f"{', '.join(f'{c:+d}' for c in SLOPE_COUNTS)} -> {nb} points")
    print(f"  {na + nb} points, roughly {(na + nb) * SEC_PER_POINT / 60:.0f} min; "
          f"max power {steps[-1]:g} dBm, analyser ref level {args.ref_level:g} dBm")
    print(f"  output: {args.out}")
    if not args.yes:
        print("dry run: nothing was changed. Add --yes to measure.")
        return 0

    # ---- measure ------------------------------------------------------------
    out = os.path.abspath(args.out)
    out_dir = os.path.dirname(out)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(out))[0]
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    nc_a = os.path.join(out_dir, f"{stem}_{stamp}_A_attenuator.nc")
    nc_b = os.path.join(out_dir, f"{stem}_{stamp}_B_vernier.nc")

    ctl = _connect_ctl(args)
    dssg, sa = ctl["dssg"], ctl["signalhound"]
    st = dssg.command("status")["status"]
    cfg = dssg.command("get_config")["config"]
    start = {"rf_on": bool(st.get("rf_on")), "frequency_Hz": st.get("frequency_Hz"),
             "power_dBm": st.get("power_dBm"), "attenuator_dBm": st.get("attenuator_dBm",
                                                                        st.get("power_dBm")),
             "vernier": int(st.get("vernier", 0)), "has_vernier": bool(st.get("has_vernier")),
             "fine_power_cfg": bool(cfg.get("hardware", {}).get("fine_power", False))}
    sst = sa.command("status")["status"]
    sa_start = {k: sst.get(k) for k in ("rbw_Hz", "vbw_Hz", "center_Hz", "span_Hz",
                                        "ref_level_dBm")}
    model, firmware = parse_idn(st.get("idn", ""))
    print(f"start state: RF {'ON' if start['rf_on'] else 'off'}, "
          f"{float(start['frequency_Hz']) / 1e6:.3f} MHz, {start['power_dBm']} dBm, "
          f"fine power {'on' if start['fine_power_cfg'] else 'off'} -- restored at the end")
    if not start["has_vernier"]:
        print("this unit has no vernier: cannot calibrate fine power")
        ctl.close()
        return 1

    ok = False
    ds_a = ds_b = None
    try:
        # STEP mode: the vernier becomes a control, a power set goes straight
        # to the attenuator, and the published power is the nominal step
        dssg.command("set_config", config={"hardware": {"fine_power": False}})
        dssg.wait_until(lambda s: s.get("fine_power") is False, timeout_s=10.0,
                        what="the generator to switch to step mode")
        # park at the LOWEST step before RF goes on (it is switched on as a
        # fixed condition of the scan), and set the analyser up -- RBW first,
        # or a VBW above the old RBW is refused by its limits
        dssg.command("set_power", power_dBm=float(steps[0]))
        sa.command("set_rbw", rbw_Hz=RBW_KHZ * 1e3)
        sa.command("set_vbw", vbw_Hz=VBW_KHZ * 1e3)
        sa.command("set_span", span_Hz=SPAN_MHZ * 1e6)
        sa.command("set_ref_level", ref_level_dBm=float(args.ref_level))
        time.sleep(0.5)                        # describe revisions settle

        for rec, nc, label in ((rec_a, nc_a, "A"), (rec_b, nc_b, "B")):
            # each scan switches RF on as a fixed condition: let that happen
            # at the lowest step, not where the last scan ended (+5 dBm)
            dssg.command("set_power", power_dBm=float(steps[0]))
            reg, lab = _registry(args)          # step mode: vernier is a control
            try:
                recipe = Recipe.from_dict(rec)
                errs = recipe.validate(reg)
                if errs:
                    raise RuntimeError("recipe not valid:\n  " + "\n  ".join(errs))
                print(f"scan {label}: {n_points(rec)} points")
                t0 = time.monotonic()
                ds = run(recipe, reg, data_path=nc,
                         on_log=lambda m: print(f"\n  [scan] {m}"),
                         on_progress=lambda d, n, eta: print(
                             f"\r  {d}/{n} points, {eta:5.0f} s left", end="", flush=True))
                print(f"\n  done in {time.monotonic() - t0:.0f} s")
            finally:
                lab.close()
            ds.to_netcdf(nc, engine="h5netcdf")
            print(f"  raw data: {nc}")
            if label == "A":
                ds_a = ds
            else:
                ds_b = ds
        ok = True
    except KeyboardInterrupt:
        print("\ninterrupted (Ctrl+C)")
    except Exception as exc:
        print(f"\nFAILED: {type(exc).__name__}: {exc}")
    finally:
        print("RF off, restoring the start state ...")
        _restore(ctl, start, sa_start, rf_back=ok and args.restore_rf and start["rf_on"])
        ctl.close()
        if start["rf_on"] and not (ok and args.restore_rf):
            print("note: the RF was ON at the start and is left OFF (--restore-rf "
                  "switches it back on after a successful run)")
    if not ok:
        return 1

    # ---- extract ------------------------------------------------------------
    levels_a = _levels(ds_a, freqs)
    lv_b = _levels(ds_b, freqs)
    rows = [(f * 1e9, p, list(SLOPE_COUNTS), lv_b[i][k])
            for i, f in enumerate(freqs) for k, p in enumerate(spows)]
    meta = {"model": model, "firmware": firmware,
            "date": _dt.datetime.now().isoformat(timespec="seconds"),
            "measured_with": "scripts/calibrate_power.py",
            "sources": [os.path.basename(nc_a), os.path.basename(nc_b)],
            "analyser": {"model": sst.get("model", ""), "span_MHz": SPAN_MHZ,
                         "rbw_kHz": RBW_KHZ, "vbw_kHz": VBW_KHZ,
                         "ref_level_dBm": float(args.ref_level)},
            "vernier_counts": list(SLOPE_COUNTS)}
    d = vernier_cal.build_calibration([f * 1e9 for f in freqs], steps, levels_a, rows,
                                      meta=meta)
    vernier_cal.Calibration.from_dict(d)       # refuse to write what cannot load
    vernier_cal.save_calibration(d, out)
    print(f"calibration written: {out}")
    for note in d["notes"]:
        print(f"  note: {note}")
    print("  freq (GHz)   worst step error (dB)   vernier dB/count at "
          + ", ".join(f"{p:g}" for p in d["slope_powers_dBm"]) + " dBm")
    for f, drow, srow in zip(d["freqs_Hz"], d["dev_dB"], d["slope_dB_per_count"]):
        worst = max(drow, key=abs)
        print(f"  {f / 1e9:9.3f}   {worst:+8.3f}                "
              + ", ".join(f"{s:.4f}" for s in srow))
    print("Restart the dssg service to load it (its log names the file).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
