"""The RESONANCE WINDOW (Lukas, 2026-09-28): sweep a slow detector only near the line.

"Some devices are terribly slow and if I want to see e.g. FMR in field I scan
most of the time in the dark. Can we cheat?" His decisions: (1) outside the
window the data is FILLED WITH THE BASELINE plus a MEASURED MASK in the file;
(2) the window follows a MODEL and SELF-CORRECTS from the measured dip; (3) the
model switches between IN-PLANE and OUT-OF-PLANE.

Layers, as elsewhere:
  * resonance.py: the physics, checked against vna-control's model (numbers
    computed with that module and pasted here -- scan-core never imports it);
  * window.py: dip finding, the bridge, planning;
  * the engine on the simulator's windowable detector `fmr`: bins, widening,
    full sweeps, baseline fill + mask, Meff tracking, pause and abort, netCDF;
  * recipe validation and the describe wire (a fake Instrument, no sockets).

No ports are used (the wire layer is faked in-process).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core import resonance as R
from scan_core import window as W
from scan_core.errors import Fault, ScanAborted
from scan_core.registry import AcquireSpec, AxisSpec, Gettable, build_sim_registry

G28 = 28.0 / 13.996          # the g that gives vna-control's gamma of 28.0 GHz/T


# ─────────────────────────────── physics ─────────────────────────────────────

# (model, B mT, angle deg, Hk mT, f in Hz from vna-control's model.kittel_Hz
#  with ms_mT 1750, gamma 28.0 GHz/T, easy axis 0) -- computed 2026-09-28
VNA_NUMBERS = [
    ("inplane", 50, 0, 0, 8400000000.0),
    ("inplane", 100, 0, 20, 13263845596.206253),     # field along the easy axis
    ("inplane", 100, 90, 20, 10771815074.535954),    # along the hard axis, B > Hk
    ("inplane", 200, 30, 0, 17485994395.515514),
    ("inplane", 80, 180, 10, 11394314371.65045),
    ("outofplane", 2000, 0, 0, 7000000000.0),
]


@pytest.mark.parametrize("model,B,ang,hk,f_vna", VNA_NUMBERS)
def test_kittel_agrees_with_the_vna_module(model, B, ang, hk, f_vna):
    p = {"g": G28, "meff_mT": 1750.0, "hk_mT": hk, "easy_axis_deg": 0.0}
    assert R.kittel_hz(model, B, ang, p) == pytest.approx(f_vna, rel=1e-9)


def test_far_above_hk_the_equilibrium_follows_the_field_and_agrees_with_vna():
    # vna's model assumes M along the field; at B >> Hk the true equilibrium
    # lags by a fraction of a degree and the frequencies agree closely
    p = {"g": G28, "meff_mT": 1750.0, "hk_mT": 20.0, "easy_axis_deg": 0.0}
    B, ang = 500.0, 45.0
    k = 28.0e6
    d = math.radians(ang)
    f_vna = k * math.sqrt((B + 20 * math.cos(2 * d)) * (B + 1750 + 20 * math.cos(d) ** 2))
    phm, unique = R.equilibrium_angle(B, ang, 20.0, 0.0)
    assert unique and 43.0 < phm < 45.0          # pulled towards the easy axis
    assert R.kittel_hz("inplane", B, ang, p) == pytest.approx(f_vna, rel=1e-3)


def test_out_of_plane_is_linear_above_meff_and_invalid_below():
    p = {"g": 2.0, "meff_mT": 1000.0}
    k = R.gamma_hz_per_mT(2.0)
    assert k == pytest.approx(27.992e6)
    f1, f2 = R.kittel_hz("outofplane", 1200, 0, p), R.kittel_hz("outofplane", 1500, 0, p)
    assert f1 == pytest.approx(k * 200) and f2 - f1 == pytest.approx(k * 300)
    assert math.isnan(R.kittel_hz("outofplane", 900, 0, p))
    assert R.kittel_hz("outofplane", -1200, 0, p) == pytest.approx(f1)


def test_in_plane_anisotropy_raises_the_easy_axis_and_the_hysteretic_region_is_invalid():
    p = {"g": 2.0, "meff_mT": 1000.0, "hk_mT": 30.0, "easy_axis_deg": 10.0}
    easy = R.kittel_hz("inplane", 100, 10, p)
    hard = R.kittel_hz("inplane", 100, 100, p)
    assert easy > hard
    # below Hk, field along the hard axis: two symmetric minima -> not valid
    assert math.isnan(R.kittel_hz("inplane", 20, 100, p))
    # below Hk, field ANTI-parallel to the easy axis: also two minima
    assert math.isnan(R.kittel_hz("inplane", 20, 190, p))
    # no field, no anisotropy: nothing to precess about
    assert math.isnan(R.kittel_hz("inplane", 0, 0, {"meff_mT": 1000}))
    # a negative field is the same line as a positive one turned round
    assert R.kittel_hz("inplane", -100, 40, p) == pytest.approx(
        R.kittel_hz("inplane", 100, 220, p))


@pytest.mark.parametrize("model,B,ang", [("inplane", 60, 0), ("inplane", 150, 70),
                                         ("outofplane", 1900, 0)])
def test_meff_from_f_inverts_kittel(model, B, ang):
    p = {"g": 2.05, "meff_mT": 1650.0, "hk_mT": 8.0, "easy_axis_deg": 20.0}
    f = R.kittel_hz(model, B, ang, p)
    guess = dict(p, meff_mT=1234.0)            # the inverse must not use the old Meff
    assert R.meff_from_f(model, f, B, ang, guess) == pytest.approx(1650.0, abs=1e-6)
    assert math.isnan(R.meff_from_f(model, float("nan"), B, ang, p))


# ─────────────────────────────── dip finding ─────────────────────────────────

def _line(f0=10e9, hw=50e6, depth=3.0, noise=0.02, n=201, span=1e9, seed=1,
          centre=None):
    c = f0 if centre is None else centre
    f = np.linspace(c - span / 2, c + span / 2, n)
    rng = np.random.default_rng(seed)
    y = -2 - 0.3 * (f - c) / 1e9 - depth / (1 + ((f - f0) / hw) ** 2)
    return f, y + noise * rng.standard_normal(n)


def test_find_dip_is_sub_bin_accurate_and_refuses_what_is_not_a_line():
    f, y = _line(f0=10.0123e9)
    fit, _ = W.find_dip(f, y)
    assert abs(fit - 10.0123e9) < 0.5 * (f[1] - f[0])
    fit, _ = W.find_dip(f, -y, dip="max")              # a peak, not a dip
    assert abs(fit - 10.0123e9) < 0.5 * (f[1] - f[0])
    _, noise = _line(depth=0.0)
    assert math.isnan(W.find_dip(f, noise)[0])          # only noise
    # a smooth RIPPLE (a TG's flatness) with no line: its trough is not a line
    fb = np.linspace(2e9, 20e9, 721)
    ripple = 0.25 * np.sin(fb / 0.9e9) + 0.03 * np.random.default_rng(3).standard_normal(721)
    assert math.isnan(W.find_dip(fb, ripple)[0])
    fe, ye = _line(f0=10.47e9, centre=10e9)             # line at the window's edge
    fit, info = W.find_dip(fe, ye)
    assert math.isnan(fit) and info["reason"]           # refused, whichever rule


def test_bridge_replaces_a_region_with_a_straight_line():
    y = np.array([0, 1, 2, 3, 50, 60, 70, 7, 8, 9], dtype=float)
    b = W.bridge(y, 4, 6)
    assert np.allclose(b[4:7], [4, 5, 6])               # between 2 (median 1,2,3) and 8
    assert np.array_equal(b[:4], y[:4]) and np.array_equal(b[7:], y[7:])
    z = W.bridge(y.astype(complex) * 1j, 4, 6)
    assert np.allclose(z[4:7], [4j, 5j, 6j])


# ─────────────────────────────── the engine ──────────────────────────────────

def _recipe(num=30, meff=1750.0, margin=300.0, full_every=20, track=True,
            model="inplane", start=30.0, stop=190.0, dets=("fmr",), **extra):
    w = {"detector": "fmr", "field": "field", "angle": "field_angle", "model": model,
         "params": {"g": 2.0, "meff_mT": meff, "hk_mT": 5.0},
         "margin_MHz": margin, "full_every": full_every, "track": track}
    w.update(extra)
    return Recipe(axes=[{"type": "linear", "param": "field", "start": start,
                         "stop": stop, "num": num}],
                  detectors=list(dets), window=w)


def test_windows_are_bin_indices_and_full_sweeps_come_at_the_right_points():
    reg = build_sim_registry()
    s = reg._state
    ds = run(_recipe(num=45, full_every=20), reg)
    full = ds["fmr_full_sweep"].values
    assert list(np.nonzero(full)[0]) == [0, 20, 40]
    # what the detector was ASKED for: None = full, else a pair of plain ints
    asked = s.fmr_windows
    assert len(asked) == 45                              # no widening needed
    for k, w in enumerate(asked):
        if k in (0, 20, 40):
            assert w is None
        else:
            i0, i1 = w
            assert type(i0) is int and i1 - i0 + 1 >= 5
            # the window is centred on the prediction, in bins of the grid
            f = s.fmr_freqs_MHz * 1e6
            pred = ds["fmr_fres_pred_Hz"].values[k]
            assert f[i0] >= pred - 300e6 - 25e6 and f[i1] <= pred + 300e6 + 25e6
            assert ds["fmr_window_lo_Hz"].values[k] == f[i0]
    frac = ds["fmr_measured"].values.mean()
    assert 0.05 < frac < 0.2


def test_the_file_is_filled_with_the_baseline_and_says_what_was_measured():
    reg = build_sim_registry()
    s = reg._state
    s.fmr_noise_dB = 0.0
    ds = run(_recipe(num=6, full_every=0), reg)
    y, m = ds["fmr"].values, ds["fmr_measured"].values
    assert m[0].all()                                    # the full sweep
    first_fit = ds["fmr_fres_fit_Hz"].values[0]
    f = s.fmr_freqs_MHz * 1e6
    for k in range(1, 6):
        assert np.isfinite(y[k]).all()                   # filled everywhere
        i0, i1 = s.fmr_windows[k]
        assert m[k, i0:i1 + 1].all() and m[k].sum() == i1 - i0 + 1
        # outside the window: the FIRST sweep, its own line bridged away
        out = ~m[k]
        region = np.abs(f - first_fit) <= 300e6
        keep = out & ~region
        assert np.allclose(y[k, keep], y[0, keep])
        bridged = y[k, out & region]
        assert bridged.min() > y[0].min() + 2.0          # no ghost of the old line
    # inside the window: the line of THIS point, found where the model says
    assert np.all(np.abs(ds["fmr_fres_fit_Hz"].values[1:]
                         - ds["fmr_fres_pred_Hz"].values[1:]) < 50e6)
    assert ds["fmr"].attrs["window_mask"] == "fmr_measured"
    assert json.loads(ds.attrs["window_json"])["detector"] == "fmr"


def test_meff_converges_from_a_wrong_start():
    reg = build_sim_registry()
    on = []
    ds = run(_recipe(num=25, meff=1750.0), reg, on_window=on.append)
    meff = ds["fmr_meff_mT"].values
    assert meff[0] == 1750.0                             # the assumption, used once
    assert abs(meff[-1] - 1650.0) < 5.0                  # the truth
    assert len(on) == 25 and abs(on[-1]["meff_mT"] - 1650.0) < 5.0
    assert on[-1]["points"] == 25 and 0 < on[-1]["fraction_measured"] < 0.3


def test_a_line_outside_the_window_widens_it_and_measures_the_point_again():
    reg = build_sim_registry()
    s = reg._state
    # no tracking and no full sweeps after the first: the wrong Meff stays wrong
    p_true = {"g": 2.0, "meff_mT": 1650.0, "hk_mT": 5.0}
    p_bad = dict(p_true, meff_mT=1450.0)
    B = 100.0
    shift = R.kittel_hz("inplane", B, 0, p_true) - R.kittel_hz("inplane", B, 0, p_bad)
    margin = 0.7 * shift / 1e6                            # misses at x1, finds at x2
    logs = []
    r = _recipe(num=2, start=B, stop=B, meff=1450.0, margin=margin, track=False,
                full_every=0)
    r.axes = [{"type": "array", "param": "field", "values": [B, B]}]
    ds = run(r, reg, on_log=logs.append)
    assert s.fmr_windows[0] is None
    w1, w2 = s.fmr_windows[1], s.fmr_windows[2]           # point 2: two attempts
    assert len(s.fmr_windows) == 3
    assert (w2[1] - w2[0]) > 1.8 * (w1[1] - w1[0])
    assert any("widening" in m for m in logs)
    fit = ds["fmr_fres_fit_Hz"].values[1]
    assert abs(fit - R.kittel_hz("inplane", B, 0, p_true)) < 30e6
    lo, hi = ds["fmr_window_lo_Hz"].values[1], ds["fmr_window_hi_Hz"].values[1]
    assert lo <= fit <= hi and ds["fmr_measured"].values[1].sum() == w2[1] - w2[0] + 1


def test_below_saturation_out_of_plane_sweeps_the_full_band():
    reg = build_sim_registry()
    s = reg._state
    s.fmr_model = "outofplane"
    s.fmr_true = {"g": 2.0, "meff_mT": 1650.0}
    r = Recipe(axes=[{"type": "array", "param": "field",
                      "values": [1600, 1700, 1800, 1850, 1900]}],
               detectors=["fmr"],
               window={"detector": "fmr", "field": "field", "model": "outofplane",
                       "params": {"g": 2.0, "meff_mT": 1650.0}, "full_every": 0})
    reg.get("field").limits = (-2000, 2000)
    ds = run(r, reg)
    full = ds["fmr_full_sweep"].values
    # 1600: below saturation, no line at all -> full; 1700: the line is at
    # 1.4 GHz, below the 2 GHz band -> a minimal window at the bottom edge
    assert list(full) == [True, False, False, False, False]
    assert math.isnan(ds["fmr_fres_pred_Hz"].values[0])
    assert s.fmr_windows[1] == (0, 4)
    k = R.gamma_hz_per_mT(2.0)
    assert ds["fmr_fres_fit_Hz"].values[-1] == pytest.approx(k * 250, abs=20e6)


def test_a_line_the_model_puts_outside_the_band_is_not_chased():
    reg = build_sim_registry()
    s = reg._state
    s.fmr_freqs_MHz = np.linspace(2000.0, 8000.0, 241)
    r = Recipe(axes=[{"type": "array", "param": "field", "values": [30, 40, 190, 195]}],
               detectors=["fmr"],
               window={"detector": "fmr", "field": "field",
                       "params": {"g": 2.0, "meff_mT": 1650.0, "hk_mT": 5.0},
                       "full_every": 0})
    ds = run(r, reg)
    # 190 mT puts the line at ~17 GHz, far above an 8 GHz band: a minimal
    # window at the top edge, no widening, no forced full sweep next time
    assert s.fmr_windows[2] == (236, 240) and s.fmr_windows[3] == (236, 240)
    assert not ds["fmr_full_sweep"].values[2:].any()
    assert np.isnan(ds["fmr_fres_fit_Hz"].values[2:]).all()


def test_group_mates_are_windowed_and_filled_with_their_own_baseline():
    reg = build_sim_registry()
    s = reg._state
    fmr = reg.get("fmr")
    reg.add(Gettable("fmr_raw", "raw", "dB", lambda: s.read_fmr() + 10.0,
                     axes=fmr.axes, acquire=fmr.acquire))
    ds = run(_recipe(num=4, full_every=0, dets=("fmr", "fmr_raw")), reg)
    assert len(s.fmr_windows) == 4                       # one sweep per point for both
    assert np.allclose(ds["fmr_raw"].values, ds["fmr"].values + 10.0)
    assert ds["fmr_raw"].attrs["window_mask"] == "fmr_measured"


def test_a_fault_redoes_the_point_without_teaching_the_window_twice():
    reg = build_sim_registry()
    s = reg._state
    state = {"calls": 0}

    def check(ids):
        assert "field" in ids and "field_angle" in ids    # the window READS them
        state["calls"] += 1
        # fault seen at the CHECK after the read of point 3 (6th/7th call)
        return [Fault("magnet", "water lost")] if state["calls"] == 8 else []

    seen = []
    ds = run(_recipe(num=6, full_every=0), reg, fault_check=check,
             on_fault=seen.append, pause_poll_s=0.01)
    assert seen and seen[0][0].name == "magnet"
    assert len(s.fmr_windows) == 7                       # one point measured twice
    assert np.isfinite(ds["fmr"].values).all()
    assert ds["fmr_measured"].values.sum(axis=1)[1:].min() > 0


def test_abort_while_widening_keeps_the_points_measured():
    reg = build_sim_registry()
    state = {"n": 0}

    def trigger(window=None):
        state["n"] += 1
        reg._state.trigger_fmr(window)

    reg.get("fmr").acquire = AcquireSpec("fmr", trigger_fn=trigger)
    r = _recipe(num=4, meff=1300.0, margin=20.0, track=False, full_every=0)
    with pytest.raises(ScanAborted) as ei:
        run(r, reg, should_abort=lambda: state["n"] >= 3)
    ds = ei.value.dataset
    assert ds is not None
    assert ds["fmr_measured"].values[0].all()            # the first (full) point kept
    assert not ds["fmr_measured"].values[1:].any()       # the aborted one is not


def test_netcdf_round_trip(tmp_path):
    xr = pytest.importorskip("xarray")
    ds = run(_recipe(num=5), build_sim_registry())
    path = tmp_path / "win.nc"
    ds.to_netcdf(path, engine="h5netcdf")
    back = xr.load_dataset(path, engine="h5netcdf")
    assert back["fmr_measured"].dtype == bool
    assert np.array_equal(back["fmr_measured"].values, ds["fmr_measured"].values)
    assert np.array_equal(back["fmr_full_sweep"].values, ds["fmr_full_sweep"].values)
    np.testing.assert_allclose(back["fmr"].values, ds["fmr"].values)
    np.testing.assert_allclose(back["fmr_meff_mT"].values, ds["fmr_meff_mT"].values)
    assert back["fmr_fres_fit_Hz"].attrs["units"] == "Hz"
    assert Recipe.from_dict(json.loads(back.attrs["recipe_json"])).window["detector"] == "fmr"


def test_a_complex_windowed_detector_is_split_and_filled():
    reg = build_sim_registry()
    s = reg._state
    s.fmr_noise_dB = 0.0
    fmr = reg.get("fmr")
    reg.add(Gettable("fmr_c", "complex", "", lambda: 10 ** (s.read_fmr() / 20) + 0j,
                     axes=fmr.axes, dtype="complex", acquire=fmr.acquire,
                     window=fmr.window))
    r = _recipe(num=4, full_every=0, dets=("fmr_c",))
    r.window["detector"] = "fmr_c"
    ds = run(r, reg)
    assert "fmr_c_real" in ds and ds["fmr_c_real"].attrs["window_mask"] == "fmr_c_measured"
    assert np.isfinite(ds["fmr_c_real"].values).all()
    assert np.isfinite(ds["fmr_c_fres_fit_Hz"].values).all()


# ───────────────────────────── recipe + wire ─────────────────────────────────

def test_validation():
    reg = build_sim_registry()
    assert _recipe().validate(reg) == []
    bad = [
        ({"detector": "s21"}, "window support"),
        ({"detector": "lockin_r"}, "not a 1-D array"),
        ({"field": "nope"}, "unknown field"),
        ({"field": None}, "`field`"),
        ({"model": "tilted"}, "model must be"),
        ({"params": {"meff": 1}}, "unknown parameter"),
        ({"params": {"g": 0}}, "g-factor"),
        ({"margin_MHz": -1}, "margin_MHz"),
        ({"dip": "up"}, "dip must"),
        ({"full_every": -1}, "full_every"),
        ({"track": "yes"}, "track"),
        ({"angle": "nope"}, "unknown angle"),
        ({"typo": 1}, "unknown key"),
    ]
    for change, text in bad:
        r = _recipe()
        r.window.update(change)
        if change.get("detector") in ("s21", "lockin_r"):
            r.detectors = [change["detector"]]
        errs = r.validate(reg)
        assert any(text in e for e in errs), (change, errs)
    r = _recipe()
    r.detectors = ["lockin_r"]
    assert any("not one of the scan's detectors" in e for e in r.validate(reg))
    r = _recipe()
    r.axes = [{"type": "fly", "param": "pos_x", "start": 0, "stop": 10, "num": 11,
               "speed": 5}]
    assert any("fly" in e for e in r.validate(reg))
    r = _recipe()
    r.window["angle"] = 30.0                              # a fixed number is fine
    assert r.validate(reg) == []


def test_old_recipes_are_unchanged():
    recipes = Path(__file__).parents[1] / "recipes"
    for path in recipes.glob("*.yaml"):
        r = Recipe.load(path)
        assert r.window is None and "window" not in r.to_dict()
        assert "window" not in r.to_json()
    reg = build_sim_registry()
    r = Recipe(axes=[{"type": "linear", "param": "field", "start": 0, "stop": 10,
                      "num": 3}], detectors=["fmr", "s21"])
    ds = run(r, reg)
    # the detector sweeps its full band, and nothing of the window is added
    assert reg._state.fmr_windows == [None, None, None]
    assert set(ds.data_vars) == {"fmr", "s21_real", "s21_imag"}
    assert "window_json" not in ds.attrs


def test_the_schema_accepts_a_window():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).parents[1] / "schema" /
                         "scan.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(_recipe().to_dict(), schema)
    d = _recipe().to_dict()
    d["window"]["nonsense"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(d, schema)


class _FakeInst:
    """Just enough of Instrument for manifest.py: records every command."""

    name = "sa"

    def __init__(self):
        self.calls = []
        self.acq = 0

    def command(self, verb, **kw):
        self.calls.append((verb, kw))
        if verb == "acquire":
            self.acq += 1
            return {"ok": True, "acq_id": self.acq}
        if verb == "get_frequencies":
            return {"ok": True, "values_MHz": [1000.0, 1001.0, 1002.0, 1003.0]}
        if verb == "get_trace":
            return {"ok": True, "transmission": [None, -1.0, -2.0, None],
                    "window": [1, 2]}
        return {"ok": True}

    def status(self):
        return {"acq_id": self.acq, "acquiring": False}

    def wait_until(self, pred, timeout_s=None, what="", cancel=None):
        st = self.status()
        assert pred(st)
        return st


def test_describe_window_key_reaches_the_trigger():
    from scan_core.manifest import register_manifest
    from scan_core.registry import Registry
    acquire = {"group": "sweep", "trigger_verb": "acquire", "target_key": "acq_id",
               "ready": {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                         "flag_key": "acquiring", "invert": True}}
    manifest = {"module": "sa", "parameters": [
        {"id": "transmission", "kind": "indicator", "type": "array", "unit": "dB",
         "dtype": "float", "acquire": acquire,
         "dims": [{"name": "freq", "unit": "MHz", "coord_verb": "get_frequencies",
                   "coord_key": "values_MHz"}],
         "read": {"verb": "get_trace", "key": "transmission"},
         "window": {"arg": "window", "unit": "bin", "min_bins": 7}},
        {"id": "raw", "kind": "indicator", "type": "array", "dtype": "float",
         "acquire": acquire, "dims": [{"name": "freq", "unit": "MHz"}],
         "read": {"verb": "get_trace", "key": "transmission"},
         "window": {"unit": "Hz"}},                       # not bins: ignored
    ]}
    inst, reg, warns = _FakeInst(), Registry(), []
    register_manifest(reg, inst, manifest, on_warn=warns.append)
    g = reg.get("transmission")
    assert g.window == {"arg": "window", "unit": "bin", "min_bins": 7}
    assert reg.get("raw").window is None and any("window" in w for w in warns)
    g.acquire.trigger({"window": [1, 2]})
    g.acquire.wait()
    assert inst.calls[-1] == ("acquire", {"window": [1, 2]})
    g.acquire.trigger()
    assert inst.calls[-1] == ("acquire", {})
    trace = g.get()
    assert np.isnan(trace[0]) and trace[1] == -1.0 and np.isnan(trace[3])
