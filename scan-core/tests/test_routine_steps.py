"""The five generic routine steps (2026-10-04): wait_until, abort_if, skip_if,
pause, comment, compute_set -- in the engine, on the simulator.

Each is a step of a `call` routine (what the Scan Builder writes) or a hook of
its own. What these tests pin down: wait_until holds for hold_s without a
break, times out the way on_timeout says, and gives way to Abort; abort_if
stops at the right point with the data and the reason kept; skip_if leaves
exactly the points it should as "not measured"; pause waits for an answer
(and fails or carries on without a GUI); comment lands in the file; compute_set
sets the computed value and refuses one outside the limits; validation refuses
what makes no sense; and a recipe using all five round-trips through .yaml and
the recipe inside the .nc.
"""

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, build_sim_registry, run                  # noqa: E402
from scan_core import hooks                                            # noqa: E402
from scan_core.errors import ScanAborted, ScanStopped                  # noqa: E402
from scan_core.registry import Gettable                                # noqa: E402


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    """The real polls are 0.5 s / 30 s / 0.2 s: far too slow for a test."""
    monkeypatch.setattr(hooks, "WAIT_POLL_S", 0.01)
    monkeypatch.setattr(hooks, "WAIT_LOG_S", 0.05)
    monkeypatch.setattr(hooks, "PAUSE_POLL_S", 0.01)


def _call(when, *steps, **extra):
    return {"when": when, "action": "call", "args": {"steps": list(steps)}, **extra}


def _field_scan(hooks_, num=6, dets=("lockin_r",), **kw):
    """field 0, 10, ..., 50 (num=6)."""
    return Recipe(name="steps", axes=[{"type": "linear", "param": "field",
                                       "start": 0.0, "stop": 10.0 * (num - 1),
                                       "num": num}],
                  detectors=list(dets), hooks=list(hooks_), **kw)


def _clock_gettable(reg, pid, fn):
    """A detector whose value is fn(seconds since it was made)."""
    t0 = time.monotonic()
    reg.add(Gettable(pid, pid, "K", lambda: fn(time.monotonic() - t0)))


def _logged():
    lines = []
    return lines, lines.append


# ─────────────────────────────── wait_until ───────────────────────────────────

def test_wait_until_waits_for_the_condition_to_HOLD():
    reg = build_sim_registry()
    # true between 0.10 and 0.15 s (a flicker, shorter than the hold), then
    # false, then true for good from 0.30 s
    _clock_gettable(reg, "temp", lambda t: 5.0 if (0.10 < t < 0.15 or t > 0.30) else 20.0)
    lines, log = _logged()
    r = _field_scan([_call("before_scan", {"wait_until": {
        "condition": "temp < 10", "hold_s": 0.1, "timeout_s": 5}})], num=2)
    t0 = time.monotonic()
    ds = run(r, reg, on_log=log)
    took = time.monotonic() - t0
    # the flicker did not count: true from 0.30 s, held 0.1 s -> >= 0.40 s
    assert took >= 0.39
    assert np.isfinite(ds["lockin_r"].values).all()
    assert any("temp < 10 is true, held 0.1 s" in m for m in lines)
    # progress lines while waiting, with the value and the hold count
    assert any(m.startswith("waiting: temp = 20") and "held" in m for m in lines)


def test_wait_until_hold_zero_is_true_once():
    reg = build_sim_registry()
    _clock_gettable(reg, "temp", lambda t: 5.0 if t > 0.05 else 20.0)
    r = _field_scan([_call("before_scan", {"wait_until": {
        "condition": "temp < 10", "timeout_s": 5}})], num=2)
    t0 = time.monotonic()
    run(r, reg)
    assert time.monotonic() - t0 < 2.0


def test_wait_until_timeout_stop_ends_the_scan_cleanly_with_the_reason():
    reg = build_sim_registry()
    _clock_gettable(reg, "temp", lambda t: 20.0)
    lines, log = _logged()
    r = _field_scan([
        _call("before_point", {"wait_until": {"condition": "temp < 10",
                                               "timeout_s": 0.1}}),
        _call("after_scan", {"set": {"rf_power": -20.0}})], num=4)
    with pytest.raises(ScanStopped) as info:
        run(r, reg, on_log=log)
    exc = info.value
    assert isinstance(exc, ScanAborted)                  # handled like an Abort
    assert "timed out" in exc.reason and "temp < 10" in exc.reason
    ds = exc.dataset
    assert ds is not None and "timed out" in ds.attrs["stopped_by"]
    assert np.isnan(ds["lockin_r"].values).all()        # stopped before point 1
    assert reg.get("rf_power").get() == -20.0            # after-scan routine ran
    assert any("scan STOPPED" in m for m in lines)


def test_wait_until_timeout_at_a_point_keeps_the_points_before():
    reg = build_sim_registry()
    _clock_gettable(reg, "temp", lambda t: 20.0)
    r = _field_scan([{"when": "every_n_points", "n": 3, "action": "call",
                      "args": {"steps": [{"wait_until": {
                          "condition": "temp < 10 or field < 25", "timeout_s": 0.05}}]}}])
    with pytest.raises(ScanStopped) as info:
        run(r, reg)
    vals = info.value.dataset["lockin_r"].values
    # points 0..2 measured; the wait before point 3 (field 30) timed out
    assert np.isfinite(vals[:3]).all() and np.isnan(vals[3:]).all()


def test_wait_until_timeout_continue_carries_on():
    reg = build_sim_registry()
    _clock_gettable(reg, "temp", lambda t: 20.0)
    lines, log = _logged()
    r = _field_scan([_call("before_scan", {"wait_until": {
        "condition": "temp < 10", "timeout_s": 0.05, "on_timeout": "continue"}})], num=3)
    ds = run(r, reg, on_log=log)
    assert np.isfinite(ds["lockin_r"].values).all()
    assert "stopped_by" not in ds.attrs
    assert any("timed out" in m and "carrying on" in m for m in lines)


def test_abort_during_a_wait():
    reg = build_sim_registry()
    _clock_gettable(reg, "temp", lambda t: 20.0)
    stop = threading.Event()
    threading.Timer(0.15, stop.set).start()
    r = _field_scan([_call("before_point", {"wait_until": {
        "condition": "temp < 10", "timeout_s": 60}})], num=3)
    t0 = time.monotonic()
    with pytest.raises(ScanAborted) as info:
        run(r, reg, should_abort=stop.is_set)
    assert not isinstance(info.value, ScanStopped)      # the OPERATOR's abort
    assert time.monotonic() - t0 < 5.0


# ──────────────────────────────── abort_if ────────────────────────────────────

def test_abort_if_before_point_stops_at_the_right_point(tmp_path):
    reg = build_sim_registry()
    lines, log = _logged()
    r = _field_scan([_call("before_point", {"abort_if": {"condition": "field > 25"}}),
                     _call("after_scan", {"set": {"field": 0.0}})])
    with pytest.raises(ScanStopped) as info:
        run(r, reg, on_log=log)
    ds = info.value.dataset
    vals = ds["lockin_r"].values
    # 0, 10, 20 measured; at 30 the condition is true BEFORE it is measured
    assert np.isfinite(vals[:3]).all() and np.isnan(vals[3:]).all()
    assert "abort_if field > 25 at point 4" in ds.attrs["stopped_by"]
    assert "field = 30" in ds.attrs["stopped_by"]
    assert reg.get("field").get() == 0.0                 # after-scan routine ran
    # the reason survives the file
    path = tmp_path / "stopped.nc"
    ds.to_netcdf(path)
    import xarray as xr
    with xr.open_dataset(path) as back:
        assert "abort_if field > 25" in back.attrs["stopped_by"]


def test_abort_if_after_point_keeps_that_point():
    reg = build_sim_registry()
    r = _field_scan([{"when": "after_point", "action": "abort_if",
                      "args": {"condition": "field > 25"}}])      # the hook form
    with pytest.raises(ScanStopped) as info:
        run(r, reg)
    vals = info.value.dataset["lockin_r"].values
    assert np.isfinite(vals[:4]).all() and np.isnan(vals[4:]).all()


def test_abort_if_false_changes_nothing():
    reg = build_sim_registry()
    r = _field_scan([_call("before_point", {"abort_if": {"condition": "field > 1000"}})])
    ds = run(r, reg)
    assert np.isfinite(ds["lockin_r"].values).all() and "stopped_by" not in ds.attrs


def test_abort_if_before_scan_starts_nothing():
    reg = build_sim_registry()
    r = _field_scan([_call("before_scan", {"abort_if": {"condition": "rf_power < 0"}})])
    with pytest.raises(ScanStopped, match="at before_scan"):
        run(r, reg)


# ──────────────────────────────── skip_if ─────────────────────────────────────

@pytest.mark.parametrize("when", ["before_point", "after_point"])
def test_skip_if_makes_exactly_those_points_nan(when):
    reg = build_sim_registry()
    lines, log = _logged()
    r = _field_scan([_call(when, {"skip_if": {"condition": "field > 15 and field < 35"}})],
                    dets=("lockin_r", "photon_counts", "lockin_state"))
    ds = run(r, reg, on_log=log)
    v = ds["lockin_r"].values
    assert np.isnan(v[[2, 3]]).all()
    assert np.isfinite(v[[0, 1, 4, 5]]).all()
    # every storage type gets its own "not measured", int and enum included
    pc = ds["photon_counts"]
    assert np.isnan(pc.values[[2, 3]]).all() and np.isfinite(pc.values[[0, 1, 4, 5]]).all()
    assert json.loads(ds.attrs["skipped_points"]) == [[2], [3]]
    assert ds.attrs["skipped_count"] == 2
    assert sum("skipped -- skip_if" in m for m in lines) == 2


def test_skip_if_before_point_does_not_measure():
    reg = build_sim_registry()
    reads = []
    reg.add(Gettable("counted", "counted", "", lambda: reads.append(1) or 1.0))
    r = _field_scan([_call("before_point", {"skip_if": {"condition": "field >= 30"}})],
                    dets=("counted",))
    run(r, reg)
    assert len(reads) == 3                               # only 0, 10, 20 were read


def test_skip_if_inside_a_routine_still_restores():
    """A routine that moved something and then skipped the point must put it
    back -- the next point is measured at what the scan says."""
    reg = build_sim_registry()
    r = _field_scan([_call("before_point", {"set": {"rf_power": 10.0}},
                           {"skip_if": {"condition": "field > 15"}},
                           {"set": {"rf_power": 12.0}})],
                    fixed={"rf_power": -5.0})
    ds = run(r, reg)
    assert reg.get("rf_power").get() == -5.0
    assert np.isnan(ds["lockin_r"].values[2:]).all()


def test_skip_if_on_error_continue_does_not_swallow_a_skip():
    reg = build_sim_registry()
    r = _field_scan([{"when": "every_n_points", "n": 1, "on_error": "continue",
                      "action": "call",
                      "args": {"steps": [{"skip_if": {"condition": "field == 20"}}]}}])
    ds = run(r, reg)
    assert np.isnan(ds["lockin_r"].values[2]) and ds.attrs["skipped_count"] == 1


# ───────────────────────────────── pause ──────────────────────────────────────

def _answering(go_on, delay=0.05):
    calls = []

    def on_pause(message, answer):
        calls.append(message)
        if answer is not None:
            # answered from ANOTHER thread, as the GUI does
            threading.Timer(delay, answer, args=(go_on,)).start()
    return calls, on_pause


def test_pause_continue():
    reg = build_sim_registry()
    calls, on_pause = _answering(True)
    r = _field_scan([_call("before_scan", {"pause": {"message": "Insert the polariser"}})],
                    num=2)
    ds = run(r, reg, on_pause=on_pause)
    assert calls == ["Insert the polariser", None]       # asked, then dismissed
    assert np.isfinite(ds["lockin_r"].values).all()


def test_pause_abort_answer_stops_with_the_reason():
    reg = build_sim_registry()
    calls, on_pause = _answering(False)
    r = _field_scan([{"when": "each_sweep", "axis": "field", "action": "call",
                      "args": {"steps": [{"pause": {"message": "Rotate the sample"}}]}}])
    with pytest.raises(ScanStopped) as info:
        run(r, reg, on_pause=on_pause)
    assert "Abort at the pause: Rotate the sample" in info.value.dataset.attrs["stopped_by"]
    assert calls == ["Rotate the sample", None]


def test_abort_button_during_a_pause():
    reg = build_sim_registry()
    calls = []
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    r = _field_scan([_call("before_scan", {"pause": {"message": "never answered"}})])
    with pytest.raises(ScanAborted):
        run(r, reg, should_abort=stop.is_set,
            on_pause=lambda m, a: calls.append(m))
    assert calls == ["never answered", None]             # the banner went away


def test_pause_headless_fail_and_continue():
    reg = build_sim_registry()
    r = _field_scan([_call("before_scan", {"pause": {"message": "check the laser"}})])
    with pytest.raises(RuntimeError, match="no operator"):
        run(r, reg)
    lines, log = _logged()
    r = _field_scan([_call("before_scan", {"pause": {"message": "check the laser",
                                                     "headless": "continue"}})], num=2)
    ds = run(r, reg, on_log=log)
    assert np.isfinite(ds["lockin_r"].values).all()
    assert any("check the laser" in m and "headless" in m for m in lines)


# ──────────────────────────────── comment ─────────────────────────────────────

def test_comments_with_placeholders_land_in_the_attrs(tmp_path):
    reg = build_sim_registry()
    lines, log = _logged()
    r = _field_scan([
        _call("before_scan", {"comment": {"text": "start; power = {rf_power} dBm"}}),
        {"when": "every_n_points", "n": 3, "action": "comment",
         "args": {"text": "field now {field}, T = {ppms.temperature}"}},
        _call("after_scan", {"comment": {"text": "done"}})],
        fixed={"rf_power": -7.25})
    ds = run(r, reg, on_log=log)
    comments = json.loads(ds.attrs["comments"])
    assert [c["text"] for c in comments] == [
        "start; power = -7.25 dBm",
        "field now 0, T = {ppms.temperature}",           # unknown: left as text
        "field now 30, T = {ppms.temperature}",
        "done"]
    assert [c["point"] for c in comments] == [None, 1, 4, None]
    assert comments[2]["index"] == [3]
    assert all(c["time"][:4].isdigit() for c in comments)
    assert any("{ppms.temperature}" in m and "warning" in m for m in lines)
    path = tmp_path / "c.nc"
    ds.to_netcdf(path)
    import xarray as xr
    with xr.open_dataset(path) as back:
        assert json.loads(back.attrs["comments"])[0]["text"] == "start; power = -7.25 dBm"


def test_live_snapshots_carry_the_comments():
    reg = build_sim_registry()
    seen = []
    r = _field_scan([_call("before_scan", {"comment": {"text": "hello"}})], num=2)
    run(r, reg, on_point=lambda d, t, snap: seen.append(snap().attrs.get("comments")))
    assert seen and all("hello" in s for s in seen)


# ────────────────────────────── compute_set ───────────────────────────────────

def test_compute_set_sets_the_computed_value():
    reg = build_sim_registry()
    r = _field_scan([_call("before_point", {"compute_set": {"set": {
        "rf_freq": "1000 + 10 * field"}}})], num=3, dets=("rf_freq",))
    ds = run(r, reg)
    assert list(ds["rf_freq"].values) == [1000.0, 1100.0, 1200.0]


def test_compute_set_refuses_out_of_limits():
    reg = build_sim_registry()
    r = _field_scan([_call("before_point", {"compute_set": {"set": {
        "rf_freq": "1000 + 1000 * field"}}})], num=3)     # 11000 MHz at field 10
    with pytest.raises(ValueError, match="outside its limits"):
        run(r, reg)
    assert reg.get("rf_freq").get() == 1000.0            # never clamped to 6000
    # on_error: continue -> logged, the scan carries on
    lines, log = _logged()
    r = _field_scan([_call("before_point", {"compute_set": {"set": {
        "rf_freq": "1000 + 1000 * field"}}}, on_error="continue")], num=3)
    ds = run(r, reg, on_log=log)
    assert np.isfinite(ds["lockin_r"].values).all()
    assert sum("outside its limits" in m and "carrying on" in m for m in lines) == 2


def test_compute_set_is_restored_like_a_set():
    reg = build_sim_registry()
    r = _field_scan([{"when": "each_sweep", "axis": "field", "action": "call",
                      "args": {"steps": [{"compute_set": {"set": {"rf_power": "rf_power + 5"}}}]}}],
                    num=2, fixed={"rf_power": -10.0})
    run(r, reg)
    assert reg.get("rf_power").get() == -10.0            # put back after the routine


# ─────────────────────────────── validation ───────────────────────────────────

@pytest.mark.parametrize("hook, words", [
    (_call("before_scan", {"wait_until": {"condition": "field > 1"}}), "needs timeout_s"),
    (_call("before_scan", {"wait_until": {"condition": "field > 1", "timeout_s": 0}}),
     "timeout_s must be"),
    (_call("before_scan", {"wait_until": {"condition": "field > 1", "timeout_s": 5,
                                          "hold_s": -1}}), "hold_s must be"),
    (_call("before_scan", {"wait_until": {"condition": "field > 1", "timeout_s": 5,
                                          "on_timeout": "retry"}}), "on_timeout"),
    (_call("before_scan", {"wait_until": {"condition": "fieldd > 1", "timeout_s": 5}}),
     "unknown parameter 'fieldd'"),
    (_call("before_scan", {"abort_if": {"condition": "__import__('os')"}}), "not allowed"),
    (_call("before_point", {"abort_if": {"condition": "open('x')"}}), "calling open()"),
    (_call("after_scan", {"abort_if": {"condition": "field > 1"}}), "cannot run at after_scan"),
    (_call("before_scan", {"skip_if": {"condition": "field > 1"}}), "cannot run at before_scan"),
    (_call("after_scan", {"skip_if": {"condition": "field > 1"}}), "cannot run at after_scan"),
    (_call("before_point", {"skip_if": {"condition": "s21 > 1"}}), "whole trace"),
    (_call("before_scan", {"pause": {"message": "x", "headless": "maybe"}}), "headless"),
    (_call("before_scan", {"pause": {}}), "needs message"),
    (_call("before_scan", {"comment": {"text": 5}}), "text must be text"),
    (_call("before_scan", {"comment": {"text": "x", "extra": 1}}), "unknown key"),
    (_call("before_scan", {"compute_set": {"set": {"lockin_r": "1"}}}), "not a settable"),
    (_call("before_scan", {"compute_set": {"set": {"rf_freq": "1 +"}}}), "not a valid"),
    (_call("before_scan", {"compute_set": {"set": {}}}), "map parameter ids"),
    (_call("before_scan", {"comment": "just text"}), "needs a mapping"),
    (_call("before_scan", {"comment": {"text": "a"}, "action": "x"}), "only its own key"),
    ({"when": "before_point", "action": "skip_if", "args": {}}, "needs condition"),
])
def test_validation_refuses(hook, words):
    errs = _field_scan([hook]).validate(build_sim_registry())
    assert any(words in e for e in errs), errs


def test_skip_if_is_refused_in_a_fly_scan():
    reg = build_sim_registry()
    r = Recipe(axes=[{"type": "array", "param": "pos_y", "values": [0.0, 1.0]},
                     {"type": "fly", "param": "pos_x", "start": -5, "stop": 5,
                      "num": 5, "speed": 50, "speed_param": "stage_speed"}],
               detectors=["lockin_r"],
               hooks=[{"when": "each_sweep", "axis": "pos_x", "action": "call",
                       "args": {"steps": [{"skip_if": {"condition": "pos_y > 0"}}]}}])
    assert any("fly scan" in e and "skip_if" in e for e in r.validate(reg))


def test_validation_happens_before_anything_moves():
    reg = build_sim_registry()
    reg.get("field").set(99.0)
    r = _field_scan([_call("before_scan", {"set": {"field": 150.0}},
                           {"abort_if": {"condition": "field.real > 1"}})])
    with pytest.raises(ValueError, match="invalid recipe"):
        run(r, reg)
    assert reg.get("field").get() == 99.0


# ─────────────────────────── a recipe with all five ───────────────────────────

ALL_FIVE = [
    {"when": "before_scan", "action": "call", "args": {"steps": [
        {"wait_until": {"condition": "abs(rf_power + 10) < 0.5", "hold_s": 0.02,
                        "timeout_s": 5, "on_timeout": "stop"}},
        {"pause": {"message": "Insert the polariser, then Continue",
                   "headless": "continue"}},
        {"comment": {"text": "start; power = {rf_power} dBm"}}]}},
    {"when": "before_point", "action": "call", "args": {"steps": [
        {"abort_if": {"condition": "lockin_r > 100"}},
        {"skip_if": {"condition": "field == 20"}},
        {"compute_set": {"set": {"rf_freq": "1000 + 10 * field"}}}]}},
    {"when": "after_scan", "action": "call", "args": {"steps": [
        {"comment": {"text": "done at {field} mT"}},
        {"set": {"field": 0.0}}]}},
]


def _all_five_recipe():
    return _field_scan(ALL_FIVE, num=4, dets=("lockin_r", "rf_freq"),
                       fixed={"rf_power": -10.0})


def test_all_five_round_trip_yaml_and_nc(tmp_path):
    reg = build_sim_registry()
    r = _all_five_recipe()
    assert r.validate(reg) == []
    r.save(tmp_path / "five.yaml")
    back = Recipe.load(tmp_path / "five.yaml")
    assert back.to_dict() == r.to_dict()
    ds = run(back, reg)
    from_nc = Recipe.from_dict(json.loads(ds.attrs["recipe_json"]))
    assert from_nc.to_dict() == r.to_dict()
    assert list(ds["rf_freq"].values[[0, 1, 3]]) == [1000.0, 1100.0, 1300.0]
    assert np.isnan(ds["lockin_r"].values[2])
    texts = [c["text"] for c in json.loads(ds.attrs["comments"])]
    assert texts == ["start; power = -10 dBm", "done at 30 mT"]
    assert reg.get("field").get() == 0.0


def test_the_schema_accepts_all_five():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).parents[1] / "schema" /
                         "scan.schema.json").read_text(encoding="utf-8"))
    d = _all_five_recipe().to_dict()
    d["hooks"].append({"when": "before_point", "action": "abort_if",
                       "args": {"condition": "field > 100"}})
    jsonschema.validate(d, schema)
    bad = json.loads(json.dumps(d))
    bad["hooks"][0]["args"]["steps"][0]["wait_until"].pop("timeout_s")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
