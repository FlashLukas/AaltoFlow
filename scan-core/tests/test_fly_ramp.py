"""Fly scans over ANY knob that can sweep (ramp.py), not just a stage.

Lukas (2026-10-09): "make fly scanning much more common between modules ...
magnetic field, RF frequency ...". A module declares a `ramp` block; the fly
engine then asks the module to SWEEP the knob over each row and bins the
samples by the ramp's readback -- the MEASURED value when the module can
report it while sweeping, the COMMANDED value (with its time stamp) when not,
and the file says which.

The simulator has one of each: `field` (a simulated Hall probe streams the
actual field: binned by measurement) and `rf_freq` (the software ramp's own
record: binned by command).
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core.flyscan import fly_rate, row_seconds, validate_fly
from scan_core.ramp import CommandTrack, RampSpec
from scan_core.registry import build_sim_registry


def _peak_rows(ds, det, dim):
    """Index of the maximum along `dim`, per row."""
    da = ds[det].transpose(..., dim)
    return np.nanargmax(np.nan_to_num(da.values, nan=-np.inf), axis=-1)


def _field_fly(**kw):
    ax = {"type": "fly", "param": "field", "start": 30, "stop": 70, "num": 41,
          "speed": 20.0}
    ax.update(kw)
    return ax


def _recipe(axes, dets=("lockin_r",), **kw):
    return Recipe(name="t", axes=axes, detectors=list(dets), **kw)


def test_field_flown_matches_the_stepped_map_and_says_measurement():
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.005
    outer = {"type": "array", "param": "rf_freq", "values": [850.0, 950.0]}
    r = _recipe([outer, _field_fly()], fixed={"pos_x": 30.0, "pos_y": 50.0})
    assert r.validate(reg) == []
    ds = run(r, reg)
    assert ds["field"].attrs["fly_binned_by"] == "measurement"
    assert ds["field"].attrs["fly_mode"] == "ramp"
    assert ds["field"].attrs["fly_ramp"] == "software"
    n = ds["lockin_r_n"].values
    assert np.nanmedian(n) >= 4 and np.all(n[:, 1:-1] > 0)
    # the same map STEPPED: the resonance sits on the same field pixel (+-1)
    step = _recipe([outer, {"type": "linear", "param": "field", "start": 30,
                            "stop": 70, "num": 41}], fixed={"pos_x": 30.0, "pos_y": 50.0})
    ref = run(step, reg)
    a = _peak_rows(ds, "lockin_r", "field")
    b = _peak_rows(ref, "lockin_r", "field")
    assert np.all(np.abs(a - b) <= 1), (a, b)
    # the module's ramp is not left running
    assert not reg.get("field")._sim_ramp.running


def test_frequency_flown_is_binned_by_command():
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.005
    r = _recipe([{"type": "array", "param": "field", "values": [40.0, 60.0]},
                 {"type": "fly", "param": "rf_freq", "start": 700, "stop": 1100,
                  "num": 41, "speed": 200.0}],
                fixed={"pos_x": 30.0, "pos_y": 50.0}, zigzag=True)
    assert r.validate(reg) == []
    ds = run(r, reg)
    assert ds["rf_freq"].attrs["fly_binned_by"] == "command"
    step = _recipe([{"type": "array", "param": "field", "values": [40.0, 60.0]},
                    {"type": "linear", "param": "rf_freq", "start": 700, "stop": 1100,
                     "num": 41}], fixed={"pos_x": 30.0, "pos_y": 50.0})
    ref = run(step, reg)
    a = _peak_rows(ds, "lockin_r", "rf_freq")
    b = _peak_rows(ref, "lockin_r", "rf_freq")
    # zig-zag: the second row was swept BACKWARDS, and still lines up
    assert np.all(np.abs(a - b) <= 1), (a, b)


def test_zigzag_field_rows_agree_with_lag_correction():
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.02                 # 40 ms group delay = 0.8 mT at 20 mT/s
    r = _recipe([{"type": "array", "param": "rf_freq", "values": [900.0, 900.0]},
                 _field_fly()], fixed={"pos_x": 30.0, "pos_y": 50.0}, zigzag=True)
    ds = run(r, reg)
    fwd, bwd = ds["lockin_r"].values
    f = ds["field"].values

    def centroid(y):
        y = np.nan_to_num(y - np.nanmin(y))
        return float(np.sum(f * y) / np.sum(y))
    assert abs(centroid(fwd) - centroid(bwd)) < 0.5


def test_validation_refuses_a_knob_that_cannot_sweep():
    reg = build_sim_registry()
    r = _recipe([{"type": "fly", "param": "rf_power", "start": -10, "stop": 0,
                  "num": 11, "speed": 1.0}])
    errs = validate_fly(r, reg)
    assert any("cannot be flown" in e and "ramp" in e for e in errs), errs
    # a rate the module cannot do
    r = _recipe([_field_fly(speed=1e4)])
    assert any("outside what 'field' can sweep" in e for e in validate_fly(r, reg))


def test_row_time_and_default_rate():
    reg = build_sim_registry()
    ax = _field_fly()
    del ax["speed"]
    ax["row_time_s"] = 4.1                     # 41 mT run-in to run-out in 4.1 s
    assert fly_rate(ax, reg) == pytest.approx(10.0)
    assert row_seconds(ax, reg) == pytest.approx(4.1)
    del ax["row_time_s"]
    assert fly_rate(ax, reg) == reg.get("field").ramp.rate_default
    assert validate_fly(_recipe([ax]), reg) == []
    assert np.isnan(fly_rate(ax))              # no registry, no default: unknown


def test_abort_mid_row_stops_the_sweep():
    reg = build_sim_registry()
    r = _recipe([_field_fly(speed=2.0)])        # 20 s row
    stop = threading.Event()
    threading.Timer(1.5, stop.set).start()
    t0 = time.monotonic()
    ds = run(r, reg, should_abort=stop.is_set)
    assert time.monotonic() - t0 < 8.0
    walk = reg.get("field")._sim_ramp
    assert not walk.running
    here = reg._state.field_mT
    time.sleep(0.2)
    assert reg._state.field_mT == here         # stopped where it was
    assert ds is not None


def test_a_ramp_without_any_readback_is_binned_by_computed_command():
    """A module that can sweep but streams nothing: scan-core computes the
    commanded value from the start time and the rate (CommandTrack)."""
    reg = build_sim_registry()
    p = reg.get("rf_freq")
    walk = p._sim_ramp
    p.ramp = RampSpec(lambda to, rate: walk.start(to, rate), walk.stop,
                      lambda rid: walk.ramp_id != rid or not walk.running,
                      rate_unit="MHz/s", rate_limits=(0.1, 5000), measured=False,
                      readback=None)
    r = _recipe([{"type": "fly", "param": "rf_freq", "start": 700, "stop": 1100,
                  "num": 41, "speed": 200.0}], fixed={"field": 40.0,
                                                      "pos_x": 30.0, "pos_y": 50.0})
    assert r.validate(reg) == []
    ds = run(r, reg)
    assert ds["rf_freq"].attrs["fly_binned_by"] == "command"
    assert ds["rf_freq"].attrs["readback"] == "rf_freq#command"
    assert np.all(ds["lockin_r_n"].values[1:-1] > 0)


def test_command_track_is_exact_between_reads():
    tr = CommandTrack()
    tr.rest(5.0)
    c = tr.read()
    assert c["values"]["commanded"][-1] == 5.0
    t_go = time.time() - 1.0                   # started 1 s ago at 2 /s: 7 now
    tr.go(5.0, 6.0, 2.0, t_go=t_go)            # ... but the end is 6, at t_go+0.5
    c = tr.read()
    t, v = c["t"], c["values"]["commanded"]
    assert t[0] == t_go and v[0] == 5.0
    assert v[1] == 6.0 and t[1] == pytest.approx(t_go + 0.5)   # the corner
    assert v[-1] == 6.0
