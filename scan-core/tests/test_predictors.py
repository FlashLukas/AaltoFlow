"""The window's PREDICTORS (predictors.py, 2026-10-12): "add option for another physics".

  * kittel: the old FMR window, number for number (resonance.py);
  * linear: peak = a + b*x, tracks a;
  * expression: a typed formula through a whitelist parser -- unsafe input is
    refused, never evaluated -- tracking one parameter by a root find;
  * the old FMR-only window block is translated on load;
  * the engine runs a windowed scan with each predictor on the simulator.

No ports are used.
"""

from __future__ import annotations

import json
import math
import time

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core import predictors as P
from scan_core import resonance as R
from scan_core import window as W
from scan_core.registry import build_sim_registry


# ─────────────────────────────── kittel ──────────────────────────────────────

@pytest.mark.parametrize("model,B,ang", [("inplane", 50, 0), ("inplane", 100, 90),
                                         ("inplane", 80, 180), ("outofplane", 2000, 0)])
def test_kittel_is_resonance_py_unchanged(model, B, ang):
    k = P.get("kittel")
    p = {"g": 2.0, "meff_mT": 1750.0, "hk_mT": 20.0, "easy_axis_deg": 0.0}
    f = k.predict({"field": B, "angle": ang}, p, {"model": model})
    assert f == R.kittel_hz(model, B, ang, p)            # the same function, exactly
    assert k.track(f, {"field": B, "angle": ang}, dict(p, meff_mT=1000.0),
                   {"model": model}) == pytest.approx(1750.0, abs=1e-6)
    assert k.output_unit == "Hz" and k.tracked == "meff_mT"


def test_kittel_not_valid_is_nan():
    k = P.get("kittel")
    assert math.isnan(k.predict({"field": 900, "angle": 0}, {"meff_mT": 1000.0},
                                {"model": "outofplane"}))


# ─────────────────────────────── linear ──────────────────────────────────────

def test_linear_predicts_and_tracks_the_offset():
    lin = P.get("linear")
    assert lin.predict({"x": 3.0}, {"a": 10.0, "b": 2.0}, {}) == 16.0
    assert lin.track(20.0, {"x": 3.0}, {"a": 0.0, "b": 2.0}, {}) == 14.0
    assert lin.tracked_for({}) == "a"
    assert lin.output_unit_for({"unit": "GHz"}) == "GHz"


# ─────────────────────────────── expression ──────────────────────────────────

def test_formula_evaluates_the_whitelist():
    f = P.Formula("0.028*sqrt((B+hk)*(B+hk+meff)) - abs(-1) + 2**3/4")
    assert f.names == ["B", "hk", "meff"]
    want = 0.028 * math.sqrt(105 * 1855) - 1 + 2
    assert f({"B": 100, "hk": 5, "meff": 1750}) == pytest.approx(want)
    g = P.Formula("sin(pi/2) + cos(0) + exp(0) + log(exp(2))")
    assert g({}) == pytest.approx(5.0)
    # math errors and complex results are "not valid here", not exceptions
    assert math.isnan(P.Formula("sqrt(x)")({"x": -1}))
    assert math.isnan(P.Formula("1/x")({"x": 0}))
    assert math.isnan(P.Formula("x**0.5")({"x": -8}))
    assert math.isnan(P.Formula("log(x)")({"x": 0}))


@pytest.mark.parametrize("text", [
    "__import__('os').system('echo hi')",
    "().__class__",
    "x.real",
    "'abc'",
    "[1, 2]",
    "x if y else z",
    "lambda: 1",
    "open('f')",
    "eval('1')",
    "sqrt(x, y)",
    "sqrt(x=1)",
    "sqrt",
    "x == 1",
    "x // 2",
    "x % 2",
    "_hidden + 1",
    "True + 1",
    "",
    "1 +",
    "x" * 600,
])
def test_formula_refuses_everything_else(text):
    with pytest.raises(P.FormulaError):
        P.Formula(text)


def test_a_huge_power_is_nan_at_once_not_a_hang():
    t0 = time.monotonic()
    assert math.isnan(P.Formula("9**9**9")({}))
    assert time.monotonic() - t0 < 1.0


def test_expression_tracks_a_parameter_by_root_finding():
    ex = P.get("expression")
    opts = {"formula": "0.027992*sqrt((B+hk)*(B+hk+meff))", "tracked": "meff",
            "unit": "GHz"}
    w = {"options": opts, "inputs": {"B": "field"}}
    assert [i.role for i in ex.inputs_for(w)] == ["B"]
    assert [p.name for p in ex.params_for(w)] == ["hk", "meff"]
    f = ex.predict({"B": 100.0}, {"hk": 5.0, "meff": 1650.0}, opts)
    est = ex.track(f, {"B": 100.0}, {"hk": 5.0, "meff": 1750.0}, opts)
    assert est == pytest.approx(1650.0, abs=1e-6)
    # it agrees with Kittel in-plane along the easy axis
    kit = R.kittel_hz("inplane", 100.0, 0.0, {"g": 2.0, "meff_mT": 1650.0, "hk_mT": 5.0})
    assert f * 1e9 == pytest.approx(kit, rel=1e-9)


def test_solve_for_picks_the_root_near_the_start_and_says_when_there_is_none():
    sq = lambda t: t * t                               # noqa: E731
    assert P.solve_for(sq, 4.0, 1.5) == pytest.approx(2.0)
    assert P.solve_for(sq, 4.0, -1.5) == pytest.approx(-2.0)
    assert math.isnan(P.solve_for(sq, -1.0, 1.0))      # no t with t^2 = -1
    assert P.solve_for(lambda t: math.sqrt(t) if t >= 0 else math.nan,
                       3.0, 0.0) == pytest.approx(9.0)   # steps over the NaN side


def test_output_units_convert_within_a_family_only():
    assert P.output_factor("GHz", "MHz") == 1000.0
    assert P.output_factor("", "MHz") == 1.0
    assert P.output_factor("Hz", "nm") is None


def test_a_lab_predictor_registers_itself(tmp_path, monkeypatch):
    (tmp_path / "quadratic.py").write_text(
        "from scan_core import predictors as P\n"
        "class Q(P.Predictor):\n"
        "    name = 'quadratic_test'\n"
        "    label = 'peak = c*x^2'\n"
        "    inputs = (P.Input('x', 'x'),)\n"
        "    params = (P.Param('c', 'c', '{axis}/{x}^2', 1.0),)\n"
        "    tracked = 'c'\n"
        "    def predict(self, x, p, options):\n"
        "        return p['c'] * x['x'] ** 2\n"
        "P.register(Q())\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("raise RuntimeError('oops')\n", encoding="utf-8")
    monkeypatch.setattr(P, "LAB_DIR", tmp_path)
    monkeypatch.setattr(P, "_plugins_loaded", False)
    monkeypatch.setattr(P, "plugin_errors", [])
    try:
        q = P.get("quadratic_test")
        assert q is not None and q.predict({"x": 3.0}, {"c": 2.0}, {}) == 18.0
        # the default track is the root find
        assert q.track(18.0, {"x": 3.0}, {"c": 1.0}, {}) == pytest.approx(2.0)
        assert any("broken.py" in e and "oops" in e for e in P.plugin_errors)
    finally:
        P._REGISTRY.pop("quadratic_test", None)


# ─────────────────────────────── migration ───────────────────────────────────

OLD_BLOCK = {"detector": "fmr", "field": "field", "angle": "field_angle",
             "model": "inplane", "params": {"g": 2.0, "meff_mT": 1750.0, "hk_mT": 5.0},
             "margin_MHz": 300.0, "dip": "min", "track": True, "full_every": 20}
NEW_BLOCK = {"detector": "fmr", "predictor": "kittel",
             "inputs": {"field": "field", "angle": "field_angle"},
             "params": {"g": 2.0, "meff_mT": 1750.0, "hk_mT": 5.0},
             "options": {"model": "inplane"}, "margin": 300.0, "margin_unit": "MHz",
             "dip": "min", "track": True, "full_every": 20}


def test_an_old_window_block_is_translated_on_load(tmp_path):
    assert W.upgrade(dict(OLD_BLOCK)) == NEW_BLOCK
    assert W.upgrade(dict(NEW_BLOCK)) == NEW_BLOCK          # idempotent
    # the old default margin and a fixed angle
    old = {"detector": "fmr", "field": "field", "angle": 30.0}
    assert W.upgrade(old)["margin"] == 300.0 and W.upgrade(old)["inputs"]["angle"] == 30.0
    axes = [{"type": "linear", "param": "field", "start": 30, "stop": 190, "num": 4}]
    r = Recipe.from_dict({"axes": axes, "detectors": ["fmr"], "window": dict(OLD_BLOCK)})
    assert r.window == NEW_BLOCK
    assert Recipe(axes=axes, detectors=["fmr"], window=dict(OLD_BLOCK)).window == NEW_BLOCK
    assert r.validate(build_sim_registry()) == []
    path = tmp_path / "w.yaml"
    r.save(path)
    assert "margin_MHz" not in path.read_text(encoding="utf-8")


def test_an_old_and_a_new_block_run_the_same_scan():
    axes = [{"type": "linear", "param": "field", "start": 30, "stop": 190, "num": 8}]
    out = []
    for block in (OLD_BLOCK, NEW_BLOCK):
        reg = build_sim_registry()
        reg._state.fmr_noise_dB = 0.0
        out.append(run(Recipe(axes=axes, detectors=["fmr"], window=dict(block)), reg))
    np.testing.assert_array_equal(out[0]["fmr_tracked"].values, out[1]["fmr_tracked"].values)
    np.testing.assert_array_equal(out[0]["fmr_measured"].values, out[1]["fmr_measured"].values)


def test_an_old_nc_recipe_opens_with_the_new_block():
    # the recipe_json a pre-2026-10-12 file carries
    old_json = json.dumps({"axes": [{"type": "linear", "param": "field", "start": 30,
                                     "stop": 190, "num": 4}],
                           "detectors": ["fmr"], "window": OLD_BLOCK})
    r = Recipe.from_dict(json.loads(old_json))
    assert r.window["predictor"] == "kittel" and r.window["margin_unit"] == "MHz"


# ─────────────────────────────── the engine ──────────────────────────────────

FIELD_AXIS = [{"type": "linear", "param": "field", "start": 30, "stop": 190, "num": 25}]


def _run(window, axes=FIELD_AXIS, **kw):
    reg = build_sim_registry()
    r = Recipe(axes=axes, fixed={"field_angle": 0.0}, detectors=["fmr"], window=window)
    assert r.validate(reg) == []
    return run(r, reg, **kw), reg


def test_a_linear_window_finds_and_follows_the_line():
    # over 100..120 mT the sim's in-plane line (Meff 1650, Hk 5) is nearly
    # straight: ~58.6 MHz/mT. Start the offset 670 MHz off; track it.
    true_a = 27.992 * math.sqrt(115 * 1770) - 58.56 * 110
    win = {"detector": "fmr", "predictor": "linear", "inputs": {"x": "field"},
           "params": {"a": true_a - 670.0, "b": 58.56}, "margin": 300,
           "margin_unit": "MHz", "track": True, "full_every": 0}
    ds, reg = _run(win, axes=[{"type": "linear", "param": "field", "start": 100,
                               "stop": 120, "num": 11}])
    a = ds["fmr_tracked"].values
    assert a[0] == pytest.approx(true_a - 670.0)          # the guess, used once
    assert abs(a[-1] - true_a) < 30.0                     # corrected from the line
    assert ds["fmr_tracked"].attrs["units"] == "MHz"
    assert not ds["fmr_full_sweep"].values[1:].any()      # windows after the first
    fit, pred = ds["fmr_peak_fit"].values, ds["fmr_peak_pred"].values
    assert np.all(np.abs(fit[1:] - pred[1:]) < 60)
    assert 0 < ds["fmr_measured"].values.mean() < 0.3


def test_an_expression_window_tracks_meff_like_kittel():
    win = {"detector": "fmr", "predictor": "expression", "inputs": {"B": "field"},
           "params": {"hk": 5.0, "meff": 1750.0},
           "options": {"formula": "0.027992*sqrt((B+hk)*(B+hk+meff))",
                       "unit": "GHz", "tracked": "meff"},
           "margin": 0.3, "margin_unit": "GHz", "track": True, "full_every": 20}
    on = []
    ds, _ = _run(win, on_window=on.append)
    assert abs(ds["fmr_tracked"].values[-1] - 1650.0) < 5.0
    assert ds["fmr_tracked"].attrs["tracked_param"] == "meff"
    assert ds["fmr_peak_pred"].attrs["predictor"] == "expression"
    assert on[-1]["tracked_label"] == "meff" and on[-1]["axis_unit"] == "MHz"


def test_expression_validation():
    reg = build_sim_registry()

    def errs(**change):
        w = {"detector": "fmr", "predictor": "expression", "inputs": {"B": "field"},
             "params": {"c": 28.0}, "options": {"formula": "c*B", "tracked": "c"},
             "margin": 300, "margin_unit": "MHz"}
        for k, v in change.items():
            w[k] = v
        return Recipe(axes=FIELD_AXIS, detectors=["fmr"], window=w).validate(reg)

    assert errs() == []
    assert any("formula" in e for e in errs(options={"formula": "__import__('os')"}))
    assert any("c needs a value" in e for e in errs(params={}))
    assert any("not a name in the formula" in e
               for e in errs(inputs={"B": "field", "T": "field"}))
    assert any("unknown parameter 'd'" in e for e in errs(params={"c": 1, "d": 2}))
    assert any("choose one" in e for e in errs(options={"formula": "c*B"}))
    assert any("tracked parameter 'B'" in e
               for e in errs(options={"formula": "c*B", "tracked": "B"}))
    assert any("does not convert" in e
               for e in errs(options={"formula": "c*B", "tracked": "c", "unit": "nm"}))
