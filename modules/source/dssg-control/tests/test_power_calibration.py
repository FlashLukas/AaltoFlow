"""POWER CALIBRATION (2026-10-07): the measured attenuator step errors and
vernier slopes of one unit, so fine power delivers the level asked for to
~0.05 dB at every frequency -- not only at 1-2 GHz, where the steps happen to
be exact. All offline: the "bench" is the simulator with an inexact attenuator
([sim] attenuator_error_dB)."""

import importlib.util
import json
import math
import os
import time

import pytest

from dssg import vernier_cal
from dssg.config import Config
from dssg.net.describe import build_manifest
from dssg.sim_system import build_sim_system
from dssg.backends.sim import SimulatedSG12000L

STEPS = [round(-21.5 + 0.5 * k, 1) for k in range(54)]     # -21.5 .. +5


def wait_for(synth, pred, timeout=3.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        s = synth.status()
        if pred(s):
            return s
        time.sleep(0.02)
    raise AssertionError(f"timed out; last status {synth.status()}")


def _cal(freqs=(1e9, 10e9), dev_rows=None, slopes=((0.04, 0.06), (0.05, 0.07)),
         spows=(-20.0, 0.0)):
    steps = [-11.0, -10.5, -10.0, -9.5]
    dev_rows = dev_rows or [[0.0, 0.0, 0.0, 0.0], [0.4, 0.2, 0.0, -0.1]]
    return vernier_cal.Calibration(list(freqs), steps, dev_rows, list(spows),
                                   [list(r) for r in slopes], meta={"date": "2026-10-07"})


# ---- extraction ----------------------------------------------------------------

def test_build_calibration_recovers_the_step_errors_and_slopes():
    freqs = [1e9, 4e9, 10e9]
    true_dev = lambda f, p: 0.0 if p >= -10 else 0.3 * (f / 10e9) * (-(p + 10) / 3.5)
    # what the analyser shows: the level, minus a pad and a frequency-dependent
    # cable/analyser loss -- both must cancel out of the deviations
    loss = {1e9: 30.2, 4e9: 30.9, 10e9: 32.4}
    levels = [[p + true_dev(f, p) - loss[f] for p in STEPS] for f in freqs]
    rows = [(f, p, [-8, -4, 0, 4, 8],
             [p - loss[f] + c * (0.045 + 0.001 * f / 1e9 - 0.0005 * p) for c in (-8, -4, 0, 4, 8)])
            for f in freqs for p in (-20.0, -10.0, 0.0)]
    d = vernier_cal.build_calibration(freqs, STEPS, levels, rows,
                                      meta={"model": "SG12000L", "date": "2026-10-07"})
    assert d["schema"] == 1 and d["model"] == "SG12000L"
    assert d["reference_power_dBm"] == -10.0
    for i, f in enumerate(freqs):
        for j, p in enumerate(STEPS):
            assert d["dev_dB"][i][j] == pytest.approx(true_dev(f, p), abs=1e-3)
    assert d["slope_powers_dBm"] == [-20.0, -10.0, 0.0]
    assert d["slope_dB_per_count"][2][0] == pytest.approx(0.045 + 0.010 + 0.010, abs=1e-5)
    assert d["notes"] == []
    vernier_cal.Calibration.from_dict(d)          # it loads


def test_build_calibration_fills_gaps_and_drops_a_frequency_without_reference():
    nan = float("nan")
    powers = [-11.0, -10.5, -10.0, -9.5]
    levels = [[-41.0, nan, -40.0, -39.5],          # a gap: filled
              [-41.0, -40.5, nan, -39.5]]          # no reference: dropped
    d = vernier_cal.build_calibration([1e9, 2e9], powers, levels, [])
    assert d["freqs_Hz"] == [1e9]
    assert d["dev_dB"][0][1] == pytest.approx(0.0)   # -40.5 interpolated
    assert any("dropped" in n for n in d["notes"])
    assert any("filled" in n for n in d["notes"])
    # no slope rows at all: the nominal table, and a note saying so
    assert d["slope_dB_per_count"][0][0] == pytest.approx(vernier_cal.slope_dB_per_count(1e9))


def test_reference_falls_back_to_the_nearest_step():
    d = vernier_cal.build_calibration([1e9], [-12.0, -11.5, -9.0], [[-12.0, -11.4, -9.0]], [])
    assert d["reference_power_dBm"] == -9.0
    assert d["dev_dB"][0] == pytest.approx([0.0, 0.1, 0.0])


# ---- interpolation -----------------------------------------------------------------

def test_dev_is_linear_in_frequency_with_end_values_outside():
    c = _cal()
    assert c.dev(10e9, -11.0) == pytest.approx(0.4)
    assert c.dev(5.5e9, -11.0) == pytest.approx(0.2)          # half way
    assert c.dev(20e9, -11.0) == pytest.approx(0.4)           # end value
    assert c.dev(0.1e9, -11.0) == pytest.approx(0.0)
    assert c.dev(10e9, -10.75) == pytest.approx(0.3)          # between steps
    assert not c.in_range(20e9) and c.in_range(5e9)


def test_slope_is_bilinear_and_clipped():
    c = _cal()
    assert c.slope(1e9, -20) == pytest.approx(0.04)
    assert c.slope(1e9, -10) == pytest.approx(0.05)           # half way in power
    assert c.slope(5.5e9, -10) == pytest.approx(0.055)        # and in frequency
    assert c.slope(12e9, 5) == pytest.approx(0.07)            # clipped both ways
    assert c.slope(0.1e9, -30) == pytest.approx(0.04)


# ---- the split -------------------------------------------------------------------

def test_split_picks_the_step_whose_real_level_is_nearest():
    # 10 GHz, like the lab unit: -13.5 dBm really delivers -12.94
    steps = [-15.0, -14.5, -14.0, -13.5, -13.0, -12.5, -12.0]
    dev = [0.0 if p >= -10 else 0.56 * (-(p + 10) / 3.5) for p in steps]
    c = vernier_cal.Calibration([10e9], steps, [dev], [-10.0], [[0.06]])
    att, n = vernier_cal.split(-13.5, 0.5, 10e9, -40, 5, cal=c)
    assert att == -14.0                     # NOT the nominally nearest -13.5
    got = vernier_cal.delivered(att, n, 10e9, c)
    assert abs(got - -13.5) <= 0.03
    # without the calibration: the nominal step, as before
    assert vernier_cal.split(-13.5, 0.5, 10e9, -40, 5) == (-13.5, 0)


def test_split_never_leaves_the_limits_with_a_calibration():
    c = vernier_cal.Calibration([1e9], [-21.5, -21.0], [[0.9, 0.8]], [-10.0], [[0.05]])
    att, n = vernier_cal.split(-21.5, 0.5, 1e9, -21.5, 5, cal=c)
    assert att == -21.5 and n == -vernier_cal.MAX_FILL_COUNTS     # capped, not further


def test_delivered_without_calibration_is_the_old_model():
    assert vernier_cal.delivered(-13.5, -5, 2e9) == pytest.approx(-13.5 - 5 * 0.0441)


# ---- the file ------------------------------------------------------------------------

def test_json_round_trip(tmp_path):
    c = _cal()
    p = tmp_path / "dssg_power_calibration.json"
    c.save(p)
    back = vernier_cal.Calibration.load(p)
    assert back.to_dict() == c.to_dict()
    assert back.meta["date"] == "2026-10-07"
    assert json.loads(p.read_text(encoding="utf-8"))["schema"] == 1
    assert "dssg_power_calibration.json" in back.describe()


@pytest.mark.parametrize("bad", [
    {"schema": 2, "freqs_Hz": [1e9]},
    {"schema": 1, "freqs_Hz": [1e9]},                              # tables missing
    {"schema": 1, "freqs_Hz": [2e9, 1e9], "steps_dBm": [-10], "dev_dB": [[0], [0]],
     "slope_powers_dBm": [-10], "slope_dB_per_count": [[0.05], [0.05]]},   # not ascending
    {"schema": 1, "freqs_Hz": [1e9], "steps_dBm": [-10], "dev_dB": [[0]],
     "slope_powers_dBm": [-10], "slope_dB_per_count": [[0.0]]},          # zero slope
    "not a dict",
])
def test_from_dict_refuses_what_it_does_not_understand(bad):
    with pytest.raises(ValueError):
        vernier_cal.Calibration.from_dict(bad)


def _synth(cfg, events=None):
    s, backend = build_sim_system(cfg)
    ev = events if events is not None else []
    s._on_event = lambda lvl, msg: ev.append((lvl, msg))
    s.events, s.sim = ev, backend
    return s


@pytest.mark.parametrize("content", ['{"schema": 99}', "this is not json"])
def test_a_bad_file_is_a_warning_and_no_calibration(tmp_path, content):
    p = tmp_path / "dssg_power_calibration.json"
    p.write_text(content, encoding="utf-8")
    cfg = Config()
    cfg.hardware.power_calibration = str(p)
    s = _synth(cfg)
    s.start()                                       # never a crash
    try:
        assert s.power_calibration() is None
        assert any(lvl == "warn" and "NOT used" in m for lvl, m in s.events)
        assert s.status().power_calibrated is False
    finally:
        s.shutdown()


def test_no_file_is_the_old_behaviour(tmp_path):
    cfg = Config()
    cfg.hardware.power_calibration = str(tmp_path / "missing.json")
    s = _synth(cfg)
    s.start()
    try:
        assert s.power_calibration() is None
        assert any("no power calibration" in m for _, m in s.events)
        s.set_frequency(2e9)
        s.set_power(-13.73)
        st = wait_for(s, lambda st: st.attenuator_dBm == -13.5)
        assert st.vernier == -5 and st.power_calibrated is False
    finally:
        s.shutdown()


def test_a_relative_path_needs_the_service_folder(tmp_path):
    """Tests and a GUI's private simulation never pick up a unit's file lying
    in the module folder; run_service.py sets calibration_dir."""
    _cal().save(tmp_path / vernier_cal.DEFAULT_FILE)
    s = _synth(Config())
    s.load_power_calibration()
    assert s.power_calibration() is None            # no calibration_dir: not looked for
    s.calibration_dir = str(tmp_path)
    s.load_power_calibration()
    assert s.power_calibration() is not None
    assert any("power calibration loaded" in m and "2026-10-07" in m
               and "1-10 GHz" in m for _, m in s.events)


# ---- the closed loop: simulated bench -> calibration -> the level asked for ----------

def _bench(err_dB, freqs):
    """Measure the simulated unit as calibrate_power.py measures the real one:
    a pad + cable loss in front of the 'analyser', step mode, vernier 0 for
    scan A, +-8 counts for scan B."""
    cfg = Config()
    cfg.sim.attenuator_error_dB = err_dB
    sim = SimulatedSG12000L(cfg.sim, cfg.hardware.power_step_dB)
    sim.open()
    pad = lambda f: 30.0 + 0.2 * f / 1e9            # loss grows with frequency
    levels, rows = [], []
    for f in freqs:
        sim.set_frequency(f)
        sim.set_vernier(0)
        row = []
        for p in STEPS:
            sim.set_power(p)
            row.append(sim.output_dBm() - pad(f))
        levels.append(row)
        for p in (-20.0, -10.0, 0.0):
            sim.set_power(p)
            lv = []
            for c in (-8, -4, 0, 4, 8):
                sim.set_vernier(c)
                lv.append(sim.output_dBm() - pad(f))
            rows.append((f, p, [-8, -4, 0, 4, 8], lv))
        sim.set_vernier(0)
    return vernier_cal.build_calibration(freqs, STEPS, levels, rows,
                                         meta={"date": "2026-10-07", "model": "SG12000L"})


TARGETS = (-18.3, -13.5, -11.27, -10.0, -4.62, 0.0, 3.9)
FREQS = (1e9, 4e9, 7.5e9, 10e9)


def _worst_error(synth):
    worst = 0.0
    for f in FREQS:
        synth.set_frequency(f)
        for t in TARGETS:
            synth.set_power(t)
            wait_for(synth, lambda st: st.frequency_Hz == f
                     and st.attenuator_dBm == synth._att and st.vernier == synth._vernier)
            worst = max(worst, abs(synth.sim.output_dBm() - t))
    return worst


def test_closed_loop_delivers_the_power_asked_for(tmp_path):
    err = 0.56                                      # the lab unit at 10 GHz, -13.5 dBm
    d = _bench(err, [1e9, 2e9, 4e9, 6e9, 8e9, 10e9, 12e9])
    p = tmp_path / "cal.json"
    vernier_cal.save_calibration(d, p)

    cfg = Config()
    cfg.hardware.poll_hz = 20.0
    cfg.sim.attenuator_error_dB = err

    plain = _synth(cfg)                             # no calibration: the problem
    plain.start()
    try:
        uncorrected = _worst_error(plain)
    finally:
        plain.shutdown()

    cfg.hardware.power_calibration = str(p)
    s = _synth(cfg)
    s.start()
    try:
        assert s.status().power_calibrated is True
        corrected = _worst_error(s)
        # the published level agrees with what comes out
        st = s.status()
        assert abs(st.power_dBm - s.sim.output_dBm()) <= 0.02
    finally:
        s.shutdown()
    assert uncorrected > 0.3, uncorrected           # the test can see the error
    assert corrected <= 0.05, corrected


def test_adopt_includes_the_step_error(tmp_path):
    """A unit left at -13.5 dBm, vernier 0, at 10 GHz really makes ~-12.94;
    with a calibration, that is the level adopted (and published)."""
    d = _bench(0.56, [1e9, 10e9])
    p = tmp_path / "cal.json"
    vernier_cal.save_calibration(d, p)
    cfg = Config()
    cfg.sim.attenuator_error_dB = 0.56
    cfg.sim.state_frequency_Hz = 10e9
    cfg.sim.state_power_dBm = -13.5
    cfg.hardware.power_calibration = str(p)
    s = _synth(cfg)
    s.start()
    try:
        assert s.status().power_dBm == pytest.approx(-12.94, abs=0.02)
        assert s.status().attenuator_dBm == -13.5
    finally:
        s.shutdown()


def test_outside_the_table_says_so_once(tmp_path):
    d = _bench(0.3, [1e9, 4e9])
    p = tmp_path / "cal.json"
    vernier_cal.save_calibration(d, p)
    cfg = Config()
    cfg.hardware.power_calibration = str(p)
    s = _synth(cfg)
    s.start()
    try:
        s.set_frequency(6e9)
        s.set_power(-12.0)
        s.set_frequency(8e9)
        out = [m for lvl, m in s.events if "outside the power calibration" in m]
        assert len(out) == 1
        s.set_frequency(2e9)                         # back inside, then out again
        s.set_frequency(9e9)
        out = [m for lvl, m in s.events if "outside the power calibration" in m]
        assert len(out) == 2
    finally:
        s.shutdown()


def test_status_and_describe_say_it_is_calibrated(tmp_path):
    p = tmp_path / "cal.json"
    _cal().save(p)
    cfg = Config()
    cfg.hardware.power_calibration = str(p)
    s = _synth(cfg)
    s.start()
    try:
        st = s.status()
        assert st.power_calibrated is True and "cal.json" in st.power_calibration
        ids = {q["id"]: q for q in build_manifest(s)["parameters"]}
        assert ids["power_calibrated"]["kind"] == "indicator"
        assert "power calibration" in ids["power"]["help"]
    finally:
        s.shutdown()


def test_a_new_path_over_set_config_loads_and_resplits(tmp_path):
    d = _bench(0.56, [1e9, 10e9])
    p = tmp_path / "cal.json"
    vernier_cal.save_calibration(d, p)
    cfg = Config()
    cfg.hardware.poll_hz = 20.0
    cfg.sim.attenuator_error_dB = 0.56
    cfg.hardware.power_calibration = ""
    s = _synth(cfg)
    s.start()
    try:
        s.set_frequency(10e9)
        s.set_power(-13.5)
        wait_for(s, lambda st: st.attenuator_dBm == -13.5)
        cfg.hardware.power_calibration = str(p)
        s.apply_config()
        wait_for(s, lambda st: st.attenuator_dBm == -14.0)
        assert abs(s.sim.output_dBm() - -13.5) <= 0.05
    finally:
        s.shutdown()


# ---- calibrate_power.py: the recipes, built without any service -------------------

def _script():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "calibrate_power.py")
    spec = importlib.util.spec_from_file_location("calibrate_power", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_calibrate_power_plans_and_builds_the_recipes():
    cp = _script()
    # clipped to BOTH instruments: an SA44B stops at 4.4 GHz
    assert cp.plan_freqs(cp.FREQS_GHZ, (25, 12000), (0.0001, 4.4)) == [
        0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0]
    # dense around the 6 GHz vernier resonance
    assert {5.25, 5.5, 5.75, 6.25, 6.5, 6.75} <= set(cp.FREQS_GHZ)
    steps = cp.power_steps(-21.5, 10.0, 5.0)
    assert steps[0] == -21.5 and steps[-1] == 5.0 and len(steps) == 54
    assert cp.power_steps(-21.5, 10.0, 0.2)[-1] == 0.0          # --max-power clips
    assert cp.slope_powers(-21.5, 10.0, -5.0) == [-20.0, -10.0]

    a, b = cp.build_recipes([1, 4, 10], steps, [-20.0, -10.0, 0.0], ref_level=-12)
    fz = a["axes"][0]
    assert fz["type"] == "zip"
    assert [m["param"] for m in fz["members"]] == ["dssg.frequency", "signalhound.center"]
    assert fz["members"][0]["values"] == [1000.0, 4000.0, 10000.0]        # MHz
    assert fz["members"][1]["values"] == [1.0, 4.0, 10.0]                 # GHz
    seq = a["axes"][1]["values"]
    assert a["axes"][1]["param"] == "dssg.power"
    assert seq == cp.interleave(steps)
    assert a["fixed"]["dssg.vernier"] == 0 and a["fixed"]["signalhound.ref_level"] == -12
    assert list(a["fixed"])[-1] == "dssg.rf_on"     # RF on LAST, after the analyser
    # RF off after the LAST scan only (B of the last pass): warm in between
    assert a["hooks"] == []
    assert b["hooks"] == [{"when": "after_scan", "action": "call",
                           "args": {"set": {"dssg.rf_on": 0}}}]
    assert "signalhound.peak_level" in a["detectors"]
    assert "dssg.vernier" not in b["fixed"]
    assert b["axes"][2]["values"] == [-8, -4, 0, 4, 8]
    assert cp.n_points(a) == 3 * len(seq) and cp.n_points(b) == 3 * 3 * 5
    # a DOWN pass, not the last: everything reversed, and the RF stays on
    a2, b2 = cp.build_recipes([1], steps, [-20.0, -10.0, 0.0], descending=True,
                              rf_off_after=False, tag="_p2")
    assert a2["axes"][1]["values"] == cp.interleave(steps, descending=True)
    assert b2["axes"][1]["values"] == [0.0, -10.0, -20.0]
    assert b2["axes"][2]["values"] == [8, 4, 0, -4, -8]
    assert a2["hooks"] == [] and b2["hooks"] == []
    assert a2["name"].endswith("_p2_A_attenuator")


def test_interleave_puts_the_reference_between_every_few_steps():
    cp = _script()
    seq = cp.interleave([-12.0, -11.5, -11.0, -10.5, -10.0, -9.5, -9.0], every=3)
    assert seq == [-10.0, -12.0, -11.5, -11.0, -10.0, -10.5, -9.5, -9.0, -10.0]
    down = cp.interleave([-12.0, -11.5, -10.0, -9.5], every=10, descending=True)
    assert down == [-10.0, -9.5, -11.5, -12.0, -10.0]
    # every step except the reference appears exactly once
    full = cp.interleave(cp.power_steps(-21.5, 10, 5))
    assert sorted(set(full)) == cp.power_steps(-21.5, 10, 5)
    assert full.count(-10.0) == 1 + math.ceil(53 / cp.REF_EVERY)


# ---- drift removal, passes, spread, shrink (bench 2026-10-08) --------------------

def test_a_linear_drift_is_removed():
    cp = _script()
    steps = [-12.0, -11.5, -11.0, -10.5, -10.0, -9.5, -9.0]
    true = {p: p + (0.3 if p < -10 else 0.0) for p in steps}     # steps below -10 short
    seq = cp.interleave(steps, every=2)
    drift = lambda i: 0.004 * i - 0.25                       # 0.004 dB per point
    loss = 30.7
    row = [true[p] - loss + drift(i) for i, p in enumerate(seq)]
    rel = vernier_cal.drift_corrected(seq, row, -10.0)
    for p in steps:
        assert rel[p] == pytest.approx(true[p] - true[-10.0], abs=1e-9)
    # without the correction the drift WOULD have shown up
    naive = {p: row[seq.index(p)] - row[0] for p in steps}
    assert max(abs(naive[p] - (true[p] - true[-10.0])) for p in steps) > 0.02
    # and through the script's helper, into build_calibration
    levels = cp.row_levels(seq, [row], steps)
    d = vernier_cal.build_calibration([4e9], steps, levels, [])
    assert d["dev_dB"][0] == pytest.approx([0.3, 0.3, 0.3, 0.3, 0.0, 0.0, 0.0], abs=1e-6)


def test_drift_correction_skips_bad_readings_and_needs_a_reference():
    nan = float("nan")
    rel = vernier_cal.drift_corrected([-10, -11, -10, -12, -10], [-40, nan, -40.2, -42.1, nan])
    assert math.isnan(rel[-11.0])
    assert rel[-12.0] == pytest.approx(-42.1 - -40.2)      # end value of the reference
    allbad = vernier_cal.drift_corrected([-10, -11], [nan, -41])
    assert all(math.isnan(v) for v in allbad.values())


def _one_pass(dev_row, slope):
    return {"schema": 1, "kind": vernier_cal.KIND, "notes": [], "date": "2026-10-08",
            "freqs_Hz": [4e9, 10e9], "steps_dBm": [-11.0, -10.5, -10.0],
            "dev_dB": [list(dev_row), [0.6, 0.3, 0.0]],
            "slope_powers_dBm": [-10.0], "slope_dB_per_count": [[slope], [0.06]]}


def test_passes_are_averaged_with_their_spread():
    passes = [_one_pass([0.10, -0.01, 0.0], 0.044), _one_pass([0.14, 0.15, 0.0], 0.046),
              _one_pass([0.12, 0.07, 0.0], 0.045)]
    d = vernier_cal.average_passes(passes)
    assert d["passes"] == 3
    assert d["dev_dB"][0] == pytest.approx([0.12, 0.07, 0.0], abs=1e-4)
    assert d["dev_std_dB"][0] == pytest.approx([0.02, 0.08, 0.0], abs=1e-4)
    assert d["dev_std_dB"][1] == pytest.approx([0.0, 0.0, 0.0])
    assert d["slope_dB_per_count"][0] == pytest.approx([0.045])
    assert d["slope_std_dB_per_count"][0][0] == pytest.approx(0.001, abs=1e-6)
    c = vernier_cal.Calibration.from_dict(d)
    assert c.passes() == 3 and c.worst_spread() == pytest.approx(0.08)
    assert "3 passes" in c.describe() and "worst spread 0.08 dB" in c.describe()
    # round trip keeps the spread
    assert vernier_cal.Calibration.from_dict(c.to_dict()).dev_std_dB == c.dev_std_dB


def test_a_frequency_missing_from_one_pass_is_dropped():
    p1, p2 = _one_pass([0.1, 0.0, 0.0], 0.04), _one_pass([0.1, 0.0, 0.0], 0.04)
    p2["freqs_Hz"], p2["dev_dB"], p2["slope_dB_per_count"] = [10e9], [[0.6, 0.3, 0.0]], [[0.06]]
    d = vernier_cal.average_passes([p1, p2])
    assert d["freqs_Hz"] == [10e9]
    assert any("4 GHz dropped" in n for n in d["notes"])


def test_shrink_trusts_a_deviation_only_as_far_as_it_stands_above_its_spread():
    assert vernier_cal.shrink(0.5, 0.0) == 0.5                    # no spread: as measured
    assert vernier_cal.shrink(0.15, 0.15) == pytest.approx(0.075)  # spread = size: half
    assert vernier_cal.shrink(0.07, 0.11) == pytest.approx(0.07 * 0.0049 / (0.0049 + 0.0121))
    assert vernier_cal.shrink(0.63, 0.14) == pytest.approx(0.60, abs=0.01)   # clear: ~all
    assert vernier_cal.shrink(-0.2, 0.1) < 0                      # never flips the sign
    for m, sd in ((0.3, 0.05), (-0.1, 0.4), (0.02, 0.02)):
        assert abs(vernier_cal.shrink(m, sd)) <= abs(m)          # never bigger


def test_the_module_uses_the_shrunk_deviation():
    """The 4 GHz case from the bench: -10.5 dBm read -0.01 in one pass and
    +0.15 in another. Correcting by the mean over-corrected; the shrunk value
    keeps the level near the nominal step."""
    passes = [_one_pass([0.10, -0.01, 0.0], 0.044), _one_pass([0.14, 0.15, 0.0], 0.046)]
    c = vernier_cal.Calibration.from_dict(vernier_cal.average_passes(passes))
    m, sd = c.dev_dB[0][1], c.dev_std_dB[0][1]
    assert m == pytest.approx(0.07) and sd > m
    assert c.dev(4e9, -10.5) == pytest.approx(vernier_cal.shrink(m, sd))
    assert abs(c.dev(4e9, -10.5)) < 0.03
    assert c.dev(10e9, -11.0) == pytest.approx(0.6)               # no spread there


def test_the_service_prints_its_calibration(tmp_path):
    p = tmp_path / "dssg_power_calibration.json"
    line = vernier_cal.summary_line(str(p))
    assert "no file" in line
    passes = [_one_pass([0.10, -0.01, 0.0], 0.044), _one_pass([0.14, 0.15, 0.0], 0.046)]
    vernier_cal.save_calibration(vernier_cal.average_passes(passes), p)
    line = vernier_cal.summary_line(str(p))
    assert line.startswith("power calibration: dssg_power_calibration.json, measured 2026-10-08")
    assert "4-10 GHz" in line and "2 passes" in line and "worst spread" in line
    assert line.isascii()
    assert "NOT used" in vernier_cal.summary_line(str(p), fine_power=False)
    p.write_text("{}", encoding="utf-8")
    assert "NOT usable" in vernier_cal.summary_line(str(p))


def test_run_service_prints_the_calibration_line(tmp_path):
    """The real script, as a process: stdout names the file (or says none)."""
    import subprocess
    import sys
    passes = [_one_pass([0.10, -0.01, 0.0], 0.044), _one_pass([0.14, 0.15, 0.0], 0.046)]
    cal = tmp_path / "unit_calibration.json"
    vernier_cal.save_calibration(vernier_cal.average_passes(passes), cal)
    ini = tmp_path / "dssg.ini"
    cfg = Config()
    cfg.hardware.power_calibration = str(cal)
    cfg.save(str(ini))
    script = os.path.join(os.path.dirname(__file__), "..", "scripts", "run_service.py")
    import threading
    # unbuffered, or the child's print() reaches the pipe only at exit (gotcha #19)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, script, "--cmd-port", "17641", "--pub-port",
                             "17642", "--config", str(ini)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace")
    lines, seen = [], threading.Event()

    def reader():                      # readline() blocks: never on the test's thread
        for line in proc.stdout:
            lines.append(line)
            if "power calibration" in line:
                seen.set()

    threading.Thread(target=reader, daemon=True).start()
    try:
        seen.wait(timeout=30)
        text = "".join(lines)
        assert "power calibration: unit_calibration.json, measured 2026-10-08" in text, text
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_calibrate_power_keeps_no_serial_and_drops_bad_readings():
    cp = _script()
    assert cp.parse_idn("DS Instruments,SG12000L,123456,V7.84") == ("SG12000L", "V7.84")
    nan = float("nan")
    got = cp.clean_levels([-40.0, -41.0, -42.0, -43.0], [10.0, 10.0, 10.01, 10.0],
                          [0, 1, 0, 0], 10.0)
    assert got[0] == -40.0
    assert math.isnan(got[1])            # overloaded
    assert math.isnan(got[2])            # 10 MHz off: not our tone
    assert got[3] == -43.0
    assert math.isnan(cp.clean_levels(nan, 10.0, 0, 10.0))


def test_calibrate_power_recipes_compile_in_scan_core():
    pytest.importorskip("scan_core")
    from scan_core.recipe import Recipe
    cp = _script()
    a, b = cp.build_recipes([1, 4, 10], cp.power_steps(-21.5, 10, 5), [-20.0, -10.0, 0.0])
    assert Recipe.from_dict(a).compile().shape == (3, len(cp.interleave(cp.power_steps(-21.5, 10, 5))))
    assert Recipe.from_dict(b).compile().shape == (3, 3, 5)
