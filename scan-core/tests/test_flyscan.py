"""The FLY SCAN (flyscan.py): continuous move, binned by the measured position.

Three layers, tested separately:

  * the binning itself -- pure numpy, synthetic streams with known answers;
  * the recipe rules (validate_fly) -- what a fly scan refuses and why;
  * whole scans against the SIMULATOR, whose stage really travels at a set
    speed and whose lock-in stream really lags behind a 2nd-order filter. The
    one test that matters most is there: an edge flown forwards and backwards
    lands in the same place WITH the lag correction and does not WITHOUT it.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core.flyscan import (bin_samples, find_speed_param, pixel_grid,
                               row_seconds, validate_fly)
from scan_core.hooks import firings
from scan_core.registry import build_sim_registry


# ─────────────────────────────── the binning ─────────────────────────────────

def _ramp(t0=0.0, t1=10.0, x0=0.0, x1=10.0, rate=1000.0):
    """A stage moving x0 -> x1 at constant speed between t0 and t1."""
    t = np.arange(t0, t1, 1.0 / rate)
    return t, x0 + (x1 - x0) * (t - t0) / (t1 - t0)


def test_pixel_grid_matches_a_linear_axis():
    centres, edges = pixel_grid(-2.0, 2.0, 5)
    assert np.allclose(centres, np.linspace(-2, 2, 5))
    assert np.allclose(edges, [-2.5, -1.5, -0.5, 0.5, 1.5, 2.5])


def test_binning_puts_each_sample_at_its_measured_position():
    t_pos, pos = _ramp()
    # the detector reports the position itself: every pixel's mean is its centre
    t_det = np.arange(0.0, 10.0, 1 / 300.0)
    vals = np.interp(t_det, t_pos, pos)
    centres, edges = pixel_grid(0.5, 9.5, 10)
    mean, n, std = bin_samples(t_pos, pos, t_det, vals, edges)
    assert np.allclose(mean, centres, atol=0.01)
    assert n.sum() == len(t_det) and np.all(n >= 29)
    assert np.all(std < 0.35)                      # uniform over 1 um: 0.29


def test_binning_a_downward_axis_keeps_the_coordinate_order():
    t_pos, pos = _ramp(x0=10.0, x1=0.0)            # flying backwards
    t_det = np.arange(0.0, 10.0, 1 / 300.0)
    vals = np.interp(t_det, t_pos, pos)
    centres, edges = pixel_grid(9.5, 0.5, 10)      # coordinates run downwards too
    mean, _, _ = bin_samples(t_pos, pos, t_det, vals, edges)
    assert np.allclose(mean, centres, atol=0.01)


def test_an_empty_pixel_is_nan_not_zero():
    t_pos, pos = _ramp()
    t_det = np.array([0.5, 0.6, 9.5])              # nothing between 1 and 9 um
    mean, n, std = bin_samples(t_pos, pos, t_det, np.ones(3), pixel_grid(0.5, 9.5, 10)[1])
    assert n.tolist() == [2, 0, 0, 0, 0, 0, 0, 0, 0, 1]
    assert np.isnan(mean[1]) and np.isnan(std[1]) and mean[0] == 1.0


def test_samples_outside_the_position_record_are_dropped():
    """Extrapolating a position is guessing: those samples go nowhere."""
    t_pos, pos = _ramp(t0=1.0, t1=9.0, x0=1.0, x1=9.0)
    t_det = np.array([0.0, 0.5, 5.0, 9.5])
    _, n, _ = bin_samples(t_pos, pos, t_det, np.ones(4), pixel_grid(0.5, 9.5, 10)[1])
    assert n.sum() == 1


def test_the_delay_moves_samples_back_in_time():
    """A detector that is 0.3 s late reads the position of 0.3 s ago; told
    its delay, the binning puts every value back where it belongs."""
    t_pos, pos = _ramp()
    t_det = np.arange(0.5, 10.0, 1 / 300.0)
    late = np.interp(t_det - 0.3, t_pos, pos)      # value = where the stage WAS
    centres, edges = pixel_grid(0.5, 9.5, 10)
    wrong, _, _ = bin_samples(t_pos, pos, t_det, late, edges)
    right, _, _ = bin_samples(t_pos, pos, t_det, late, edges, delay_s=0.3)
    assert np.nanmean(np.abs(wrong[1:] - centres[1:])) == pytest.approx(0.3, abs=0.02)
    # (the last pixel is only partly covered: the record ends 0.3 s early)
    assert np.allclose(right[1:-1], centres[1:-1], atol=0.01)


# ─────────────────────────────── recipe rules ────────────────────────────────

def _fly(**kw):
    ax = {"type": "fly", "param": "pos_x", "start": -10, "stop": 10, "num": 41,
          "speed": 20, "speed_param": "stage_speed"}
    ax.update(kw)
    return ax


def _recipe(axes=None, detectors=("lockin_r",), hooks=(), **kw):
    return Recipe(name="fly", fixed={"field": 40.0, "rf_freq": 890.0},
                  axes=list(axes if axes is not None else [_fly()]),
                  detectors=list(detectors), hooks=list(hooks), **kw)


@pytest.fixture
def reg():
    return build_sim_registry()


def test_a_well_formed_fly_scan_validates(reg):
    assert _recipe().validate(reg) == []
    assert _recipe(axes=[{"type": "linear", "param": "pos_y", "start": -5,
                          "stop": 5, "num": 3}, _fly()]).validate(reg) == []


@pytest.mark.parametrize("axes, detectors, hooks, needle", [
    ([_fly(), {"type": "linear", "param": "pos_y", "start": 0, "stop": 1, "num": 2}],
     ("lockin_r",), (), "innermost"),
    ([_fly(speed=0)], ("lockin_r",), (), "speed > 0"),
    ([_fly(speed=900)], ("lockin_r",), (), "outside the limits"),
    ([_fly(num=1)], ("lockin_r",), (), "at least 2 pixels"),
    ([_fly(param="pos_z")], ("lockin_r",), (), "cannot be recorded continuously"),
    ([_fly()], ("s21",), (), "whole trace"),
    ([_fly()], ("lockin_r",), ({"when": "before_point", "action": "wait_ms"},),
     "stage does not stop"),
    ([_fly()], ("lockin_r",), ({"when": "every_n_points", "n": 5, "action": "wait_ms"},),
     "stage does not stop"),
    ([_fly()], ("lockin_r",), ({"when": "before_axis", "axis": "pos_x",
                                "action": "wait_ms"},), "changes continuously"),
    ([_fly(speed_param="nothing")], ("lockin_r",), (), "not available"),
])
def test_what_a_fly_scan_refuses(reg, axes, detectors, hooks, needle):
    errs = _recipe(axes=axes, detectors=detectors, hooks=hooks).validate(reg)
    assert any(needle in e for e in errs), errs


def test_a_detector_that_cannot_stream_is_refused(reg):
    from scan_core.registry import Gettable
    reg.add(Gettable("slow", "Slow", "V", lambda: 1.0))
    errs = _recipe(detectors=("lockin_r", "slow")).validate(reg)
    assert any("'slow' cannot be recorded continuously" in e for e in errs)


def test_stepped_recipes_are_untouched_by_the_fly_rules(reg):
    stepped = _recipe(axes=[{"type": "linear", "param": "pos_x", "start": 0,
                             "stop": 1, "num": 3}],
                      hooks=[{"when": "before_point", "action": "wait_ms"}])
    assert validate_fly(stepped, reg) == []


def test_the_speed_knob_is_found_for_a_position(reg):
    assert find_speed_param(reg, "pos_x") == "stage_speed"
    assert find_speed_param(reg, "field") is None


def test_row_seconds_is_the_run_in_to_run_out_time():
    assert row_seconds(_fly()) == pytest.approx((20 + 0.5) / 20)


def test_the_fly_axis_survives_a_yaml_round_trip(tmp_path, reg):
    r = _recipe(zigzag=True)
    r.save(tmp_path / "fly.yaml")
    back = Recipe.load(tmp_path / "fly.yaml")
    assert back.axes == r.axes and back.zigzag and back.validate(reg) == []


def test_the_schema_accepts_a_fly_axis():
    jsonschema = pytest.importorskip("jsonschema")
    import json
    from pathlib import Path
    schema = json.loads((Path(__file__).parents[1] / "schema" /
                         "scan.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(_recipe().to_dict(), schema)


# ─────────────────────────── whole scans in the sim ──────────────────────────

def _fast(reg, tc=0.004):
    reg._state.lockin_tc_s = tc
    return reg


def test_a_fly_scan_gives_a_stepped_scans_grid(reg):
    """Same coordinates, same variables, plus the per-pixel count and spread;
    the speed is put back afterwards."""
    _fast(reg)
    ds = run(_recipe(axes=[_fly(start=-15, stop=15, num=31, speed=40)]), reg)
    assert np.allclose(ds["pos_x"].values, np.linspace(-15, 15, 31))
    assert set(ds.data_vars) == {"lockin_r", "lockin_r_n", "lockin_r_std"}
    assert np.all(ds["lockin_r_n"].values >= 3)
    assert np.all(np.isfinite(ds["lockin_r"].values))
    assert ds["lockin_r_std"].attrs["units"] == "V"
    assert ds["lockin_r_n"].attrs["fly_stat"] == "count"
    assert ds["pos_x"].attrs["fly"] == "true" and ds["pos_x"].attrs["speed"] == 40
    assert reg._state.stage_speed_um_s == 0.0     # the speed it had before


def test_the_fly_image_agrees_with_the_stepped_one(reg):
    """Across an island, a fly line and a stepped line are the same profile."""
    _fast(reg)
    fly = run(_recipe(axes=[_fly(start=-15, stop=15, num=31, speed=30)]), reg)
    step = run(_recipe(axes=[{"type": "linear", "param": "pos_x", "start": -15,
                              "stop": 15, "num": 31}]), reg)
    a, b = fly["lockin_r"].values, step["lockin_r"].values
    assert np.corrcoef(a, b)[0, 1] > 0.97
    assert np.max(np.abs(a - b)) < 0.25 * np.ptp(b)


def _centroids(reg, lag_correction):
    """Two rows over the island at (0, 0), the second flown BACKWARDS
    (zig-zag), with a slow filter: 2 x 25 ms = 50 ms of lag at 60 um/s is
    3 um, six pixels of 0.5 um. Returns the island's CENTROID on each row --
    the quantity a filter shifts by exactly its mean delay (a convolution
    moves a feature's centroid by the kernel's mean), so the one the
    correction must get right. Edge crossings are not: a filtered step
    crosses half height before the mean delay, and they are quantised to a
    pixel."""
    reg._state.lockin_tc_s = 0.025
    r = _recipe(axes=[{"type": "array", "param": "pos_z", "values": [0.0, 0.0]},
                      _fly(start=-14, stop=14, num=57, speed=60,
                           lag_correction=lag_correction)], zigzag=True)
    r.detectors = ["lockin_x"]
    ds = run(r, reg)
    x = ds["pos_x"].values
    out = []
    # X, not R: the centroid rule is for a LINEAR filter acting on a signal,
    # and R = |X + iY| is not linear -- the filtered R of a resonance that
    # also has a dispersive Y is not the R of a shifted resonance.
    for row in ds["lockin_x"].values:
        w = np.clip(row - np.nanmin(row), 0, None)
        out.append(float(np.nansum(w * x) / np.nansum(w)))
    return out


def test_the_lag_correction_lines_up_forward_and_backward_rows(reg):
    """THE test. Without the correction the island is shifted by the filter
    lag one way on the forward row and the other way on the return --
    ~2 x v x delay apart. With it they coincide to within a pixel."""
    fwd, back = _centroids(reg, lag_correction=False)
    raw = fwd - back
    fwd, back = _centroids(reg, lag_correction=True)
    fixed = fwd - back
    # forward row drawn late = shifted +v*d, backward row -v*d: 2 x 3 um apart
    assert raw == pytest.approx(6.0, abs=1.5)
    assert abs(fixed) < 0.5                         # one pixel


def test_each_sweep_routines_fire_per_row_and_are_counted_right(reg):
    _fast(reg)
    fired = []
    reg.add_action(__import__("scan_core.registry", fromlist=["Action"]).Action(
        "mark", "Mark", lambda: fired.append(reg._state.x_um)))
    hooks = [{"when": "each_sweep", "axis": "pos_x", "edge": "start",
              "action": "call", "args": {"action": "mark"}},
             {"when": "each_sweep", "axis": "pos_y", "edge": "end",
              "action": "call", "args": {"action": "mark"}}]
    r = _recipe(axes=[{"type": "linear", "param": "pos_y", "start": -1, "stop": 1,
                       "num": 3}, _fly(start=-4, stop=4, num=9, speed=80)],
                hooks=hooks)
    assert r.validate(reg) == []
    run(r, reg)
    comp = r.compile(reg)
    expected = sum(firings(h, comp.shape, [d.name for d in comp.dims]) for h in hooks)
    assert len(fired) == expected == 3 + 0         # 3 rows; the y sweep is one, its end is the scan's


def test_abort_mid_row_stops_the_stage_and_keeps_the_part_measured(reg):
    _fast(reg)
    t_abort = time.monotonic() + 0.6
    after = []
    reg.add_action(__import__("scan_core.registry", fromlist=["Action"]).Action(
        "after", "After", lambda: after.append(True)))
    r = _recipe(axes=[_fly(start=-40, stop=40, num=81, speed=20)],
                hooks=[{"when": "after_scan", "action": "call",
                        "args": {"action": "after"}}])
    ds = run(r, reg, should_abort=lambda: time.monotonic() > t_abort)
    x = reg._state.x_um
    time.sleep(0.3)
    assert reg._state.x_um == pytest.approx(x)      # it stopped and stays
    assert -40 < x < 0                              # well short of the far end
    vals = ds["lockin_r"].values
    assert np.isfinite(vals[:5]).all() and np.isnan(vals[-5:]).all()
    assert after == [True]                          # after_scan still ran
    assert reg._state.stage_speed_um_s == 0.0       # speed put back


def test_the_live_plot_sees_the_row_fill_in(reg):
    _fast(reg)
    seen = []
    run(_recipe(axes=[_fly(start=-20, stop=20, num=41, speed=25)]), reg,
        on_point=lambda done, total, snap: seen.append(
            int(np.isfinite(snap()["lockin_r"].values).sum())))
    assert len(seen) >= 3
    assert 0 < min(seen) < 41 and seen[-1] == 41


def test_the_first_row_warns_about_smearing(reg):
    reg._state.lockin_tc_s = 0.05                   # 100 ms lag at 40 um/s: 4 um
    log = []
    run(_recipe(axes=[_fly(start=-5, stop=5, num=21, speed=40)]), reg,
        on_log=log.append)
    assert any("smeared" in m for m in log), log
