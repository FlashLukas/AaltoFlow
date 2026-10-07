"""Measure the POWER CALIBRATION of the SG12000L on the bench (once per unit).

WHY: the attenuator's 0.5 dB steps are exact at 1-2 GHz but not above ~4 GHz
(10 GHz: "-10.5 dBm" is only 0.32 dB below "-10.0", "-13.5" is 0.56 dB short of
its nominal 3.5 dB; at 12 GHz the worst step is ~2 dB off), and the vernier's dB
per count depends on frequency AND power (resonant near 6 GHz). This script
measures both with the Signal Hound spectrum analyser and writes
dssg_power_calibration.json, which the dssg service loads at start
(src/dssg/vernier_cal.py explains how it is used).

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
  3. WARM-UP (--warmup-s, default 60 s): RF on at -10 dBm at the first
     frequency, the analyser reads the level every few seconds and the drift
     since RF on is printed (the bench drifted 0.1-0.15 dB in the minutes
     after RF on; what is left, the interleaved reference removes);
  4. --passes N (default 2) times, alternating the power direction (up, down,
     up ...) so a hysteresis or a thermal trend averages out instead of
     biasing the result:
       scan A, attenuator linearity: frequency (generator and analyser centre
       together, a `zip` axis) x every 0.5 dB step, vernier 0, with the -10 dBm
       REFERENCE step measured again every REF_EVERY points (and first and
       last). Each reading is taken relative to the reference interpolated to
       its moment, so a slow drift during the row cancels;
       scan B, vernier slope: frequency x power (-20, -10, 0 dBm) x vernier
       (-8, -4, 0, +4, +8 counts);
  5. RF OFF at the end whatever happens -- error or Ctrl+C included (it stays
     on BETWEEN the scans, so the unit does not cool down and re-drift), then
     the start state back: fine power, frequency, power, vernier, the
     analyser's settings. The RF output is left OFF unless you pass
     --restore-rf (then it is switched back on if it was on at the start and
     the run succeeded);
  6. builds one calibration per pass (vernier_cal.build_calibration), averages
     them (vernier_cal.average_passes) with the pass-to-pass SPREAD of every
     entry, writes it, and prints the worst spread per frequency: an entry
     whose spread is as large as the deviation itself is noise, and the
     module weights it down (vernier_cal.shrink).
     The step deviations are RELATIVE to the -10 dBm step, so the pad, the
     cables and the analyser's flatness cancel; the absolute level at -10 dBm
     remains the generator's own factory calibration.

Output (default: dssg_power_calibration.json in the module folder, which the
service loads; *calibration*.json is gitignored in this module -- it is lab
data of ONE unit -- and the installer and Mission Control's settings export
keep it). Every raw scan is kept as .nc next to it (named in the JSON).
Restart the dssg service afterwards to load the new file.

How good is it? The goal is SOUND, not heroic (Lukas: "dont try to get it to
0.1 dBm"): it removes the big step errors (0.5-2 dB above ~4 GHz) down to
~0.2-0.3 dB. The steps themselves repeat only to ~0.1-0.2 dB from one run to
the next, so no table can do much better.

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

#: the frequencies measured (GHz); clipped to what BOTH instruments can do.
#: Dense around 6 GHz, where the vernier slope is resonant (bench 2026-10-08:
#: 0.041 dB/count at 5 GHz, 0.111 at 6, 0.086 at 7) -- linear interpolation
#: between 5 and 7 GHz would miss the peak entirely.
FREQS_GHZ = (0.1, 0.25, 0.5, 1, 1.5, 2, 3, 4, 5, 5.25, 5.5, 5.75, 6, 6.25, 6.5,
             6.75, 7, 8, 9, 10, 11, 12)
QUICK_FREQS_GHZ = (1, 4, 10)
#: scan B: the powers and vernier counts the slope is fitted over. +-8 counts
#: covers the +-6 the fine power ever uses, in the vernier's linear part
SLOPE_POWERS = (-20.0, -10.0, 0.0)
SLOPE_COUNTS = (-8, -4, 0, 4, 8)
STEP_DB = 0.5
#: the reference step every reading is taken relative to, and how often it is
#: measured again inside a row (in points) to follow the drift
REF_POWER = -10.0
REF_EVERY = 10
#: analyser settings: a 1 MHz window around the tone, 10 kHz RBW -- a clean
#: CW peak far above the floor, and a sweep of a few tens of ms
SPAN_MHZ, RBW_KHZ, VBW_KHZ = 1.0, 10.0, 10.0
#: a peak further than this from the generator's frequency is not our tone
#: (RF not on, a spur, no signal): that reading is dropped, not used
PEAK_TOL_HZ = 200e3
#: time per point measured on the bench (2026-10-07, v2: 22 frequencies x
#: 2 passes x (61 + 15) points in ~31 min, scan A ~0.56 s, scan B ~0.53 s per
#: point): only for the ETA. (v1's "2.5 min" was a misreading -- 0.13 s
#: made the ETA 3.5x too short.)
SEC_PER_POINT = 0.55
#: warm-up: a level reading every WARMUP_EVERY_S, printed with the drift
WARMUP_EVERY_S = 5.0

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


def interleave(steps, ref: float = REF_POWER, every: int = REF_EVERY,
               descending: bool = False) -> list[float]:
    """The power sequence of one row: the reference first, then `every` steps,
    the reference again, ... and the reference last. The reference's own
    step is left out of the steps (it IS the reference, measured often)."""
    seq = sorted((float(s) for s in steps if abs(float(s) - ref) > 1e-6),
                 reverse=descending)
    out = [float(ref)]
    for k in range(0, len(seq), max(1, int(every))):
        out += seq[k:k + every] + [float(ref)]
    return out


def build_recipes(freqs_GHz, steps, spowers, counts=SLOPE_COUNTS, *,
                  ref_level: float = -10.0, descending: bool = False,
                  rf_off_after: bool = True, name: str = "dssg_power_cal",
                  tag: str = "") -> tuple[dict, dict]:
    """The two scans of ONE pass as Recipe dicts (scan_core.recipe.Recipe).

    The generator (MHz) and the analyser's centre (GHz) move TOGETHER on one
    `zip` axis: one dimension, two parameters in lockstep. Scan A's power axis
    is the interleaved sequence (reference every few points; scan-core is
    happy with repeated values on an array axis). `descending` sweeps the
    powers -- and scan B's vernier counts -- downwards (alternate passes).
    RF on is a fixed condition, set last after the analyser. `rf_off_after`
    adds an after_scan routine to scan B (the last scan of a pass) that
    switches it off (also after an abort or an error); pass it only for the
    LAST pass, so the unit stays warm between scans. The script's own finally
    switches the RF off whatever happens.
    """
    freqs_GHz = [float(f) for f in freqs_GHz]

    def fzip():
        return {"type": "zip", "name": "frequency",
                "members": [{"param": D_FREQ, "values": [f * 1e3 for f in freqs_GHz]},
                            {"param": S_CENTER, "values": list(freqs_GHz)}]}

    sa_fixed = {S_SPAN: SPAN_MHZ, S_RBW: RBW_KHZ, S_VBW: VBW_KHZ, S_REF: float(ref_level)}
    hooks = ([{"when": "after_scan", "action": "call", "args": {"set": {D_RF: 0}}}]
             if rf_off_after else [])
    common = {"detectors": [S_LEVEL, S_PEAKF, S_OVER], "zigzag": False,
              "settle": {"default_timeout_s": 30.0},
              "output": {"dir": ".", "basename": name, "format": "netcdf"}}
    way = "down" if descending else "up"
    pw = sorted((float(p) for p in spowers), reverse=descending)
    cn = sorted((int(c) for c in counts), reverse=descending)
    a = {"name": f"{name}{tag}_A_attenuator",
         "comment": f"dssg power calibration, scan A ({way}): every attenuator step "
                    f"at vernier 0 (step mode), the {REF_POWER:g} dBm reference every "
                    f"{REF_EVERY} points, level on the spectrum analyser",
         "fixed": {D_VERNIER: 0, **sa_fixed, D_RF: 1}, "hooks": [],
         "axes": [fzip(), {"type": "array", "param": D_POWER,
                           "values": interleave(steps, descending=descending)}],
         **common}
    b = {"name": f"{name}{tag}_B_vernier",
         "comment": f"dssg power calibration, scan B ({way}): vernier slope at a few "
                    f"powers (step mode), level on the spectrum analyser",
         "fixed": {**sa_fixed, D_RF: 1}, "hooks": hooks,
         "axes": [fzip(),
                  {"type": "array", "param": D_POWER, "values": pw},
                  {"type": "array", "param": D_VERNIER, "values": cn}],
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
    lv = float("nan") if levels is None else float(levels)
    pk = float("nan") if peak_GHz is None else float(peak_GHz)
    if (not math.isfinite(lv) or bool(overloaded) or not math.isfinite(pk)
            or abs(pk - expect_GHz) * 1e9 > PEAK_TOL_HZ):
        return float("nan")
    return lv


def row_levels(seq_powers, levels_rows, steps, ref: float = REF_POWER) -> list[list[float]]:
    """Scan A of one pass -> [f][step] levels RELATIVE to the drifting
    reference (vernier_cal.drift_corrected per row). The reference step
    itself comes out as 0, so build_calibration's dev formula is unchanged."""
    out = []
    for row in levels_rows:
        rel = vernier_cal.drift_corrected(seq_powers, row, ref)
        out.append([rel.get(round(float(s), 6), float("nan")) for s in steps])
    return out


# ---- the measurement -------------------------------------------------------------

def _endpoints(args) -> dict:
    out = {}
    for name, port in (("dssg", args.dssg_port), ("signalhound", args.sa_port)):
        if port:
            out[name] = (args.host, int(port), int(port) + 1)
    return out


def _connect_ctl(args):
    """Our OWN connections, for reading the start state, the warm-up and
    putting everything back. Kept apart from the scans' registry, so a scan
    interrupted half-way (a REQ socket left mid-request) cannot stop the
    restore."""
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
                              on_warn=lambda m: None)


def _limits(reg, pid):
    p = reg.get(pid)
    if p is None:
        raise RuntimeError(f"{pid} is not in the registry -- is the service the "
                           f"right version?")
    return tuple(float(v) for v in p.limits)


def _sa_level(sa, timeout_s: float = 15.0) -> float:
    """One fresh analyser acquisition (the module's acquire contract: trigger,
    wait for THAT acquisition number to finish), its peak level in dBm."""
    aid = sa.command("acquire").get("acq_id")
    st = sa.wait_until(lambda s: s.get("acq_id") == aid and not s.get("acquiring"),
                       timeout_s=timeout_s, what="an analyser sweep")
    v = (st.get("sample") or {}).get("peak_dBm")
    return float("nan") if v is None else float(v)


def _warmup(dssg, sa, f_GHz: float, seconds: float) -> None:
    """RF on at the reference power at the first frequency for `seconds`,
    printing the level and its drift since RF on every few seconds. Simple on
    purpose (Lukas: a sound correction of the big step errors is the goal,
    not 0.1 dB): what is left of the drift after it, the interleaved
    reference removes."""
    dssg.command("set_frequency", frequency_Hz=f_GHz * 1e9)
    dssg.command("set_power", power_dBm=REF_POWER)
    sa.command("set_center", center_Hz=f_GHz * 1e9)
    dssg.command("set_rf", on=True)
    print(f"warm-up: RF on at {REF_POWER:g} dBm, {f_GHz:g} GHz, for {seconds:.0f} s")
    t0 = time.monotonic()
    first = None
    while True:
        t = time.monotonic() - t0
        lv = _sa_level(sa)
        if first is None and math.isfinite(lv):
            first = lv
        drift = lv - first if first is not None and math.isfinite(lv) else float("nan")
        print(f"  {t:5.0f} s  {lv:8.3f} dBm  drift since RF on {drift:+.3f} dB")
        if t >= seconds:
            print(f"warm-up done: {drift:+.3f} dB drift over {t:.0f} s")
            return
        time.sleep(max(0.0, WARMUP_EVERY_S - (time.monotonic() - t0 - t)))


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
    ap.add_argument("--passes", type=int, default=2,
                    help="repeat the measurement N times, alternating the power "
                         "direction; averaged, with the spread (default 2)")
    ap.add_argument("--warmup-s", type=float, default=60.0,
                    help="warm-up with RF on before measuring, s (0 = none; default 60)")
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
    passes = max(1, int(args.passes))

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
    if not freqs or len(steps) < 2 or not any(abs(s - REF_POWER) < 1e-6 for s in steps):
        print(f"nothing to measure: frequencies {freqs}, steps {steps} "
              f"(the {REF_POWER:g} dBm reference step must be among them)")
        return 1
    rec_a, rec_b = build_recipes(freqs, steps, spows, ref_level=args.ref_level)
    na, nb = n_points(rec_a), n_points(rec_b)
    per_pass = na + nb
    print(f"plan: {len(freqs)} frequencies ({', '.join(f'{f:g}' for f in freqs)} GHz), "
          f"{passes} pass{'es' if passes != 1 else ''} (power up / down alternately)")
    print(f"  scan A: {len(steps)} attenuator steps {steps[0]:g}..{steps[-1]:g} dBm + the "
          f"{REF_POWER:g} dBm reference every {REF_EVERY} -> {na} points")
    print(f"  scan B: powers {', '.join(f'{p:g}' for p in spows)} dBm x vernier "
          f"{', '.join(f'{c:+d}' for c in SLOPE_COUNTS)} -> {nb} points")
    eta = passes * per_pass * SEC_PER_POINT + max(0.0, args.warmup_s)
    print(f"  {passes * per_pass} points + {args.warmup_s:.0f} s warm-up: roughly "
          f"{eta / 60:.0f} min; max power {steps[-1]:g} dBm, analyser ref level "
          f"{args.ref_level:g} dBm")
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
    results = []                      # per pass: (seq, ds_a, ds_b, nc_a, nc_b, recipe_b)
    try:
        # STEP mode: the vernier becomes a control, a power set goes straight
        # to the attenuator, and the published power is the nominal step
        dssg.command("set_config", config={"hardware": {"fine_power": False}})
        dssg.wait_until(lambda s: s.get("fine_power") is False, timeout_s=10.0,
                        what="the generator to switch to step mode")
        dssg.command("set_vernier", vernier=0)
        # the analyser -- RBW first, or a VBW above the old RBW is refused
        sa.command("set_rbw", rbw_Hz=RBW_KHZ * 1e3)
        sa.command("set_vbw", vbw_Hz=VBW_KHZ * 1e3)
        sa.command("set_span", span_Hz=SPAN_MHZ * 1e6)
        sa.command("set_ref_level", ref_level_dBm=float(args.ref_level))
        if args.warmup_s > 0:
            _warmup(dssg, sa, freqs[0], args.warmup_s)
        time.sleep(0.5)                        # describe revisions settle

        for k in range(passes):
            down = bool(k % 2)
            tag = f"_p{k + 1}"
            ra, rb = build_recipes(freqs, steps, spows, ref_level=args.ref_level,
                                   descending=down, rf_off_after=(k == passes - 1),
                                   tag=tag)
            got = []
            for rec, label in ((ra, "A"), (rb, "B")):
                nc = os.path.join(out_dir, f"{stem}_{stamp}{tag}_{label}_"
                                           f"{'attenuator' if label == 'A' else 'vernier'}.nc")
                reg, lab = _registry(args)          # step mode: vernier is a control
                try:
                    recipe = Recipe.from_dict(rec)
                    errs = recipe.validate(reg)
                    if errs:
                        raise RuntimeError("recipe not valid:\n  " + "\n  ".join(errs))
                    print(f"pass {k + 1}/{passes} ({'down' if down else 'up'}), scan {label}: "
                          f"{n_points(rec)} points")
                    t0 = time.monotonic()
                    ds = run(recipe, reg, data_path=nc,
                             on_log=lambda m: print(f"\n  [scan] {m}"),
                             on_progress=lambda d, n, eta: print(
                                 f"\r  {d}/{n} points, {eta:5.0f} s left", end="",
                                 flush=True))
                    print(f"\n  done in {time.monotonic() - t0:.0f} s")
                finally:
                    lab.close()
                ds.to_netcdf(nc, engine="h5netcdf")
                print(f"  raw data: {os.path.basename(nc)}")
                got.append((ds, nc, rec))
            results.append(got)
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

    # ---- extract: one calibration per pass, then the average + spread -------
    meta = {"model": model, "firmware": firmware,
            "date": _dt.datetime.now().isoformat(timespec="seconds"),
            "measured_with": "scripts/calibrate_power.py",
            "sources": [os.path.basename(nc) for got in results for _, nc, _ in got],
            "analyser": {"model": sst.get("model", ""), "span_MHz": SPAN_MHZ,
                         "rbw_kHz": RBW_KHZ, "vbw_kHz": VBW_KHZ,
                         "ref_level_dBm": float(args.ref_level)},
            "vernier_counts": list(SLOPE_COUNTS), "reference_every_points": REF_EVERY,
            "warmup_s": float(args.warmup_s)}
    per_pass_cals = []
    for (ds_a, _, rec_a_k), (ds_b, _, rec_b_k) in results:
        seq = rec_a_k["axes"][1]["values"]
        levels_a = row_levels(seq, _levels(ds_a, freqs), steps)
        lv_b = _levels(ds_b, freqs)
        pw_k = rec_b_k["axes"][1]["values"]
        cn_k = rec_b_k["axes"][2]["values"]
        rows = [(f * 1e9, p, list(cn_k), lv_b[i][j])
                for i, f in enumerate(freqs) for j, p in enumerate(pw_k)]
        per_pass_cals.append(vernier_cal.build_calibration(
            [f * 1e9 for f in freqs], steps, levels_a, rows, ref_power=REF_POWER, meta=meta))
    d = vernier_cal.average_passes(per_pass_cals)
    cal = vernier_cal.Calibration.from_dict(d)   # refuse to write what cannot load
    vernier_cal.save_calibration(d, out)
    print(f"calibration written: {out}")
    for note in d["notes"]:
        print(f"  note: {note}")
    print("  freq (GHz)  worst step dev (dB)  worst spread (dB)  vernier dB/count at "
          + ", ".join(f"{p:g}" for p in d["slope_powers_dBm"]) + " dBm")
    for i, f in enumerate(d["freqs_Hz"]):
        drow = d["dev_dB"][i]
        worst = max(drow, key=abs)
        spread = max(cal.dev_std_dB[i]) if cal.dev_std_dB is not None else float("nan")
        print(f"  {f / 1e9:9.3f}   {worst:+8.3f}            {spread:7.3f}           "
              + ", ".join(f"{s:.4f}" for s in d["slope_dB_per_count"][i]))
    print("A spread close to the deviation itself means that entry is mostly noise;")
    print("the module weights such entries down (vernier_cal.shrink).")
    print("Restart the dssg service to load it (it prints the file it loaded).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
