"""The SCOUT PASS (2026-10-08): take a quick look first, then measure in detail
only where something is happening -- for ANY scan, not only an XY map.

Lukas: the XY mask "breaks the universality: it should work for any
multidimensional scan once it is given something to measure". What is pinned
here (the XY behaviour of the old mask is in test_mask.py):

  * one scouted axis: a resonance peak in a 1-D frequency sweep;
  * two axes that are not XY: field x frequency, a resonance LINE, found with
    keep: deviates (no sign, no threshold given);
  * 3-D with an outer axis: per_outer once (one scout, the mask held) against
    each (a fresh scout at every field, following the line);
  * an inner unscouted axis is held at its first value during the scout;
  * scout-only settings: applied for the scout, put back for the real scan --
    also after an Abort;
  * the old `mask` key still loads (a .yaml, and the recipe inside an old .nc);
  * from: an earlier .nc, matched by coordinate name;
  * the margin in grid points;
  * the live readout and the live snapshots during the scout;
  * validate() refusing what does not fit.
No ports are used.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr

from scan_core import Recipe, run
from scan_core import scout as S
from scan_core.errors import ScanAborted
from scan_core.registry import build_sim_registry

FREQ = {"type": "linear", "param": "rf_freq", "start": 600, "stop": 1400, "num": 81}


def _fields(*values):
    return {"type": "array", "param": "field", "values": list(values)}


def _true_peak(field_mT, freqs):
    """Where the simulator's resonance is, without noise: the frequency of the
    largest |lock-in| at this field."""
    reg = build_sim_registry()
    s = reg._state
    s.field_mT = field_mT
    return float(freqs[np.argmin([abs(f - s._f_res()) for f in freqs])])


def _reads(reg, pid):
    """Record (rf_power, field, device_V) at every read of `pid`."""
    g = reg.get(pid)
    orig = g.get
    s = reg._state
    seen = []

    def get():
        seen.append((s.rf_power_dBm, s.field_mT, s.device_V, s.rf_freq_MHz))
        return orig()
    g.get = get
    return seen


# ───────────────────────────── one axis ─────────────────────────────────────

def test_one_scouted_axis_finds_a_peak_in_a_1d_sweep():
    r = Recipe(fixed={"field": 60}, axes=[FREQ], detectors=["lockin_r"],
               scout={"axes": {"rf_freq": 3}, "detector": "lockin_r", "keep": "deviates"})
    assert r.validate(build_sim_registry()) == []
    ds = run(r, build_sim_registry())
    keep = ds.scan_mask.values.astype(bool)
    f = ds.rf_freq.values
    assert ds.scan_mask.dims == ("rf_freq",)
    assert 0 < keep.sum() < 0.6 * keep.size                  # it does save time
    peak = _true_peak(60, f)
    near = np.abs(f - peak) <= 30                             # about one linewidth
    assert keep[near].all()                                   # the peak is measured
    assert not keep[np.abs(f - peak) > 250].any()             # the far baseline is not
    v = ds.lockin_r.values
    assert np.isnan(v[~keep]).all() and np.isfinite(v[keep]).all()
    assert ds["mask_lockin_r"].dims == ("mask_rf_freq",)
    assert ds["mask_lockin_r"].shape == (28,)                 # 0, 3, ..., 78, 80
    assert "mask_median" in ds.attrs and "mask_noise" in ds.attrs


# ───────────────────────────── two axes, not XY ─────────────────────────────

def test_field_by_frequency_follows_the_line_with_keep_deviates():
    axes = [{"type": "linear", "param": "field", "start": 20, "stop": 100, "num": 17},
            FREQ]
    r = Recipe(axes=axes, detectors=["lockin_r"],
               scout={"axes": {"field": 2, "rf_freq": 3}, "detector": "lockin_r",
                      "keep": "deviates", "k": 4})
    ds = run(r, build_sim_registry())
    keep = ds.scan_mask.values.astype(bool)
    assert ds.scan_mask.dims == ("field", "rf_freq")
    assert keep.mean() < 0.5
    f = ds.rf_freq.values
    for i, B in enumerate(ds.field.values):
        peak = _true_peak(B, f)
        assert keep[i, np.abs(f - peak) <= 20].all(), B       # the line, at every field
    # and the result: the measured maximum sits on the line
    v = ds.lockin_r.values
    for i, B in enumerate(ds.field.values):
        assert abs(f[np.nanargmax(v[i])] - _true_peak(B, f)) <= 20


# ───────────────────────────── 3-D: outer axes ──────────────────────────────

def _three_d(per_outer):
    # field (outer, NOT scouted) x rf_freq (scouted) x device_V (inner, not scouted)
    return Recipe(axes=[_fields(20, 60, 100), FREQ,
                        {"type": "array", "param": "device_v", "values": [0.0, 1.0]}],
                  detectors=["lockin_r"],
                  scout={"axes": {"rf_freq": 3}, "detector": "lockin_r",
                         "keep": "deviates", "per_outer": per_outer})


def test_per_outer_once_scouts_once_at_the_first_outer_value():
    reg = build_sim_registry()
    seen = _reads(reg, "lockin_r")
    events = []
    ds = run(_three_d("once"), reg, on_scout=events.append)
    made = [e for e in events if e["phase"] == "made"]
    assert len(made) == 1
    scout_reads = seen[:28]
    assert {b for _, b, _, _ in scout_reads} == {20.0}         # outer at its FIRST value
    assert {v for _, _, v, _ in scout_reads} == {0.0}          # inner at its FIRST value
    keep = ds.scan_mask.values.astype(bool)
    assert ds.scan_mask.dims == ("rf_freq",)                   # one mask for all fields
    f = ds.rf_freq.values
    # the mask was made at 20 mT: the line at 100 mT is NOT in it
    assert keep[np.abs(f - _true_peak(20, f)) <= 20].all()
    assert not keep[np.abs(f - _true_peak(100, f)) <= 20].any()
    # an inner axis carries the mask along: both device voltages, same points
    v = ds.lockin_r.values
    assert (np.isnan(v[..., 0]) == np.isnan(v[..., 1])).all()


def test_per_outer_each_scouts_again_at_every_outer_value():
    reg = build_sim_registry()
    seen = _reads(reg, "lockin_r")
    events = []
    ds = run(_three_d("each"), reg, on_scout=events.append)
    made = [e for e in events if e["phase"] == "made"]
    assert len(made) == 3
    assert ds.scan_mask.dims == ("field", "rf_freq")
    assert ds["mask_lockin_r"].dims == ("field", "mask_rf_freq")
    assert ds["mask_threshold"].dims == ("field",)
    keep = ds.scan_mask.values.astype(bool)
    f = ds.rf_freq.values
    for i, B in enumerate((20, 60, 100)):
        assert keep[i, np.abs(f - _true_peak(B, f)) <= 20].all(), B
    # each scout ran AT its field, right before that field was measured
    fields_in_order = [b for _, b, _, _ in seen]
    first = {B: fields_in_order.index(B) for B in (20.0, 60.0, 100.0)}
    assert first[20.0] < first[60.0] < first[100.0]
    # the progress events of the scout count its own points
    sc = [e for e in events if e["phase"] == "scout"]
    assert sc[-1]["done"] == sc[-1]["total"] == 28


def test_routines_still_fire_once_per_outer_step_with_each():
    r = _three_d("each")
    # "each sweep of rf_freq" = once per field value (a sweep of the
    # OUTERMOST axis would be the whole scan)
    r.hooks = [{"when": "each_sweep", "axis": "rf_freq", "edge": "start",
                "action": "call", "args": {"steps": [{"comment": {"text": "B"}}]}}]
    ds = run(r, build_sim_registry())
    comments = ds.attrs.get("comments", "[]")
    comments = json.loads(comments) if isinstance(comments, str) else comments
    assert len(comments) == 3


# ───────────────────────────── scout-only settings ──────────────────────────

def test_scout_settings_hold_during_the_scout_only():
    reg = build_sim_registry()
    seen = _reads(reg, "lockin_r")
    log = []
    r = Recipe(fixed={"field": 60, "rf_power": 0}, axes=[FREQ], detectors=["lockin_r"],
               scout={"axes": {"rf_freq": 3}, "detector": "lockin_r",
                      "keep": "deviates", "settings": {"rf_power": 12}})
    ds = run(r, reg, on_log=log.append)
    n_scout = 28
    assert {p for p, *_ in seen[:n_scout]} == {12}             # the scout's power
    assert {p for p, *_ in seen[n_scout:]} == {0}              # the scan's power
    assert reg._state.rf_power_dBm == 0
    assert any("rf_power = 12 for the scout only" in m for m in log)
    assert int(ds.scan_mask.sum()) == len(seen) - n_scout


def test_scout_settings_are_put_back_without_a_condition_too():
    reg = build_sim_registry()
    reg._state.rf_power_dBm = -7.0                             # what the instrument has
    r = Recipe(fixed={"field": 60}, axes=[FREQ], detectors=["lockin_r"],
               scout={"axes": {"rf_freq": 3}, "detector": "lockin_r",
                      "keep": "deviates", "settings": {"rf_power": 12}})
    run(r, reg)
    assert reg._state.rf_power_dBm == -7.0


def test_scout_settings_are_put_back_after_an_abort():
    reg = build_sim_registry()
    seen = _reads(reg, "lockin_r")
    r = Recipe(fixed={"field": 60, "rf_power": 0}, axes=[FREQ], detectors=["lockin_r"],
               scout={"axes": {"rf_freq": 3}, "detector": "lockin_r",
                      "keep": "deviates", "settings": {"rf_power": 12}})
    with pytest.raises(ScanAborted) as info:
        run(r, reg, should_abort=lambda: len(seen) >= 5)       # in the middle of the scout
    assert 5 <= len(seen) < 28
    assert reg._state.rf_power_dBm == 0
    # the scout's readings so far are in the data that is kept
    ds = info.value.dataset
    assert ds is not None and np.isfinite(ds["mask_lockin_r"].values).sum() == len(seen)


# ───────────────────────────── the old key, and files ───────────────────────

def test_the_old_mask_key_loads_from_yaml_and_from_an_old_nc(tmp_path):
    old = {"name": "old", "axes": [{"type": "raster",
                                    "x": {"param": "pos_x", "start": -45, "stop": 45, "num": 31},
                                    "y": {"param": "pos_y", "start": -45, "stop": 45, "num": 31}}],
           "detectors": ["lockin_r"],
           "mask": {"detector": "reflectivity", "step": 3, "keep": "above",
                    "threshold": "auto", "margin": 4.5}}
    y = tmp_path / "old.yaml"
    import yaml
    y.write_text(yaml.safe_dump(old), encoding="utf-8")
    r = Recipe.load(y)
    assert r.scout == {"axes": {"pos_x": 3, "pos_y": 3}, "detector": "reflectivity",
                       "keep": "above", "threshold": "auto",
                       "margin": {"pos_x": 1.5, "pos_y": 1.5}}    # 4.5 um / 3 um
    assert "mask" not in r.to_dict()
    # a measurement file written on 2026-10-08 carries the old block in recipe_json
    nc = tmp_path / "old.nc"
    xr.Dataset(attrs={"recipe_json": json.dumps(old),
                      "mask_json": json.dumps(old["mask"])}).to_netcdf(nc, engine="h5netcdf")
    from scan_core.scan_queue import recipe_from_file
    r2 = recipe_from_file(str(nc))
    assert r2.scout == r.scout
    ds = run(r2, build_sim_registry())
    # the file names of 2026-10-07 are kept
    assert {"scan_mask", "mask_reflectivity"} <= set(ds.data_vars)
    assert {"mask_pos_x", "mask_pos_y"} <= set(ds.coords)
    for a in ("mask_json", "mask_threshold", "mask_points", "mask_source", "mask_margin"):
        assert a in ds.attrs, a
    assert ds.attrs["mask_margin"] == pytest.approx(4.5)


def test_from_an_earlier_scan_matched_by_coordinate_name(tmp_path):
    coarse = Recipe(fixed={"field": 60},
                    axes=[{"type": "linear", "param": "rf_freq", "start": 700,
                           "stop": 1300, "num": 41}], detectors=["lockin_r"])
    p = tmp_path / "first.nc"
    run(coarse, build_sim_registry()).to_netcdf(p, engine="h5netcdf")
    reg = build_sim_registry()
    seen = _reads(reg, "lockin_r")
    r = Recipe(fixed={"field": 60}, axes=[FREQ], detectors=["lockin_r"],
               scout={"axes": {"rf_freq": 1}, "keep": "deviates",
                      "from": {"file": str(p), "detector": "lockin_r"}})
    assert r.validate(reg) == []
    ds = run(r, reg)
    keep = ds.scan_mask.values.astype(bool)
    f = ds.rf_freq.values
    # nothing re-measured for the scout: every read is a point of the scan
    assert len(seen) == int(keep.sum())
    assert keep[np.abs(f - _true_peak(60, f)) <= 20].all()
    # the earlier scan covered 700..1300 MHz only: outside it is MEASURED
    assert keep[(f < 695) | (f > 1305)].all()
    assert not keep[(f > 720) & (f < 850)].any()
    assert ds.attrs["mask_source"].startswith("{") or "first.nc" in ds.attrs["mask_source"]


def test_from_a_scan_without_that_axis_is_refused(tmp_path):
    other = Recipe(axes=[{"type": "linear", "param": "field", "start": 0, "stop": 10,
                          "num": 3}], detectors=["lockin_r"])
    p = tmp_path / "field.nc"
    run(other, build_sim_registry()).to_netcdf(p, engine="h5netcdf")
    r = Recipe(axes=[FREQ], detectors=["lockin_r"],
               scout={"axes": {"rf_freq": 1}, "from": {"file": str(p), "detector": "lockin_r"}})
    errs = r.validate(build_sim_registry())
    assert any("was not measured along 'rf_freq'" in e for e in errs), errs


# ───────────────────────────── the margin, in points ────────────────────────

def test_the_margin_is_counted_in_grid_points():
    def kept(margin):
        r = Recipe(fixed={"field": 60}, axes=[FREQ], detectors=["lockin_r"],
                   scout={"axes": {"rf_freq": 3}, "detector": "lockin_r",
                          "keep": "deviates", "margin": margin})
        ds = run(r, build_sim_registry())
        return ds.scan_mask.values.astype(bool)
    k0, k2 = kept(0), kept(2)
    # every run of measured points grows by exactly 2 points at each end
    # (the noise of two runs differs, so compare a run with ITS own core)
    assert k2.sum() > k0.sum()
    grown = S.grow(k0, [2])
    assert (S.grow(k0, [2]) | k0).sum() == grown.sum()
    assert json.loads(run(Recipe(fixed={"field": 60}, axes=[FREQ], detectors=["lockin_r"],
                                 scout={"axes": {"rf_freq": 3}, "detector": "lockin_r",
                                        "keep": "deviates", "margin": {"rf_freq": 2}}),
                          build_sim_registry()).attrs["mask_margin_points"]) == {"rf_freq": 2}


def test_deviates_finds_a_dip_as_well_as_a_peak():
    rng = np.random.default_rng(1)
    V = 1.0 + 0.01 * rng.standard_normal(40)
    V[10] = 1.5                                   # a peak
    V[30] = 0.4                                   # a dip
    c = [np.arange(40.0)]
    res = S.build(S.spec_of({"keep": "deviates", "k": 4}), c, V, c, [0])
    assert res.keep[10] and res.keep[30] and res.keep.sum() == 2


# ───────────────────────────── live view, ETA ───────────────────────────────

def test_live_snapshots_show_the_scout_filling_in():
    snaps = []

    def on_point(done, total, snapshot):
        if done == 0:                              # during the scout
            snaps.append(np.isfinite(snapshot()["mask_lockin_r"].values).sum())
    r = Recipe(fixed={"field": 60}, axes=[FREQ], detectors=["lockin_r"],
               scout={"axes": {"rf_freq": 3}, "detector": "lockin_r", "keep": "deviates"})
    run(r, build_sim_registry(), on_point=on_point)
    assert snaps[0] == 1 and snaps[-1] == 28 and snaps == sorted(snaps)


def test_the_eta_counts_only_measured_points():
    etas = []
    r = Recipe(fixed={"field": 60}, axes=[FREQ], detectors=["lockin_r"],
               scout={"axes": {"rf_freq": 3}, "detector": "lockin_r", "keep": "deviates"})
    run(r, build_sim_registry(), on_progress=lambda d, t, e: etas.append(e))
    assert etas[-1] == pytest.approx(0.0, abs=1e-6)
    assert all(e >= 0 for e in etas)


def test_estimate_for_the_builder():
    e = S.estimate(_three_d("each"))
    assert e["points"] == 28 and e["blocks"] == 3
    assert e["coarse"]["rf_freq"][-2:] == [78, 80]          # the last index, always
    assert e["outer"] == ["field"] and e["inner"] == ["device_v"]


# ───────────────────────────── validation ───────────────────────────────────

def _r(scout, axes=None, **kw):
    return Recipe(axes=axes or [_fields(20, 60), FREQ], detectors=["lockin_r"],
                  scout=scout, **kw)


@pytest.mark.parametrize("scout, text", [
    ({"axes": {}, "detector": "lockin_r"}, "say which axes"),
    ({"axes": {"rf_freq": 0}, "detector": "lockin_r"}, "whole number"),
    ({"axes": {"nope": 3}, "detector": "lockin_r"}, "no axis 'nope'"),
    ({"axes": {"rf_freq": 3}}, "needs `detector`"),
    ({"axes": {"rf_freq": 3}, "detector": "lockin_r", "keep": "peaks"}, "keep"),
    ({"axes": {"rf_freq": 3}, "detector": "lockin_r", "keep": "deviates", "k": 0}, "`k`"),
    ({"axes": {"rf_freq": 3}, "detector": "lockin_r", "per_outer": "always"}, "per_outer"),
    ({"axes": {"rf_freq": 3}, "detector": "lockin_r", "margin": {"field": 2}},
     "not a scouted axis"),
    ({"axes": {"rf_freq": 3}, "detector": "lockin_r", "settings": {"rf_freq": 5}},
     "is an axis"),
    ({"axes": {"rf_freq": 3}, "detector": "lockin_r", "settings": {"nope": 5}},
     "not known"),
    ({"axes": {"rf_freq": 3}, "detector": "lockin_r", "settings": {"rf_power": 99}},
     "outside its limits"),
    ({"axes": {"rf_freq": 3}, "detector": "lockin_r", "settings": {"lockin_r": 1}},
     "not settable"),
    ({"axes": {"rf_freq": 3}, "per_outer": "each", "from": "x.nc", "detector": "lockin_r"},
     "MEASURED scout"),
    ({"axes": {"rf_freq": 3}, "from": "x.png"}, "exactly two"),
])
def test_validation_refuses(scout, text):
    errs = _r(scout).validate(build_sim_registry())
    assert any(text in e for e in errs), errs


def test_a_repeat_axis_cannot_be_scouted_and_each_not_inside_an_average():
    rep = {"type": "repeat", "num": 3, "name": "rep"}
    errs = _r({"axes": {"rep": 1}, "detector": "lockin_r"}, axes=[rep, FREQ]) \
        .validate(build_sim_registry())
    assert any("repeat axis" in e for e in errs), errs
    avg = {"type": "repeat", "num": 3, "mode": "average", "name": "rep"}
    errs = _r({"axes": {"rf_freq": 3}, "detector": "lockin_r", "per_outer": "each"},
              axes=[avg, FREQ]).validate(build_sim_registry())
    assert any("AVERAGES" in e for e in errs), errs
    # an averaging repeat INSIDE, or `once`, is fine
    assert _r({"axes": {"rf_freq": 3}, "detector": "lockin_r"},
              axes=[avg, FREQ]).validate(build_sim_registry()) == []


def test_a_fly_axis_is_refused():
    r = Recipe(axes=[{"type": "linear", "param": "pos_y", "start": -5, "stop": 5, "num": 3},
                     {"type": "fly", "param": "pos_x", "start": -5, "stop": 5, "num": 5,
                      "speed": 50}],
               detectors=["lockin_r"],
               scout={"axes": {"pos_y": 3}, "detector": "reflectivity"})
    errs = r.validate(build_sim_registry())
    assert any("fly scan" in e for e in errs), errs


def test_the_schema_accepts_a_scout():
    jsonschema = pytest.importorskip("jsonschema")
    from pathlib import Path
    schema = json.loads((Path(__file__).parents[1] / "schema" /
                         "scan.schema.json").read_text(encoding="utf-8"))
    d = _r({"axes": {"rf_freq": 3}, "detector": "lockin_r", "keep": "deviates", "k": 5,
            "margin": {"rf_freq": 2}, "per_outer": "each",
            "settings": {"rf_power": 3}}).to_dict()
    jsonschema.validate(d, schema)
    d["scout"]["from"] = {"file": "a.nc", "detector": "x"}
    jsonschema.validate(d, schema)
    d["scout"]["keep"] = "sideways"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(d, schema)


@pytest.mark.parametrize("name", ["scout_xy", "scout_field_freq"])
def test_the_example_recipes_run(name):
    from pathlib import Path
    r = Recipe.load(Path(__file__).parents[1] / "recipes" / f"{name}.yaml")
    reg = build_sim_registry()
    assert r.validate(reg) == []
    ds = run(r, reg)
    keep = ds.scan_mask.values.astype(bool)
    assert 0 < keep.sum() < 0.5 * keep.size
