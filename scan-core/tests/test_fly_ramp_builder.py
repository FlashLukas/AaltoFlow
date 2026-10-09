"""Fly scans over ANY knob, set up in the Scan Builder window (2026-10-09).

The engine flies any knob whose module declares a `ramp` block (clMag's
field, dssg's frequency, ppms's field; in the simulator `field` and
`rf_freq`): the MODULE sweeps it over each row. The window has to offer that:

  * the fly tick is enabled on a ramp knob, not only on a streamed position;
  * a ramp has no use for a stage's boxes (speed knob, readback override,
    'move with'), so they are hidden -- and nothing of them is written;
  * the speed box speaks the ramp's language: its rate unit, limits, default;
  * the pace can be a speed, a ROW TIME, or the module's default -- exactly
    one of `speed` / `row_time_s` is written, or neither;
  * the row says what the samples are binned by (measurement / command);
  * a recipe with only row_time_s, or with neither, loads and saves back
    unchanged; the ETA counts the row time and the default rate;
  * Copy from axis... copies a speed only when the units match, a row time
    always.

And a field-flown map built in the window runs on the simulator and lands
on the same grid (and the same resonance) as the demo's stepped map.
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, build_sim_registry, run                  # noqa: E402

# the simulator's ramps (registry._attach_sim_ramps)
FIELD = {"unit": "mT/s", "limits": (0.01, 200.0), "default": 10.0}
FREQ = {"unit": "MHz/s", "limits": (0.01, 5000.0), "default": 100.0}


@pytest.fixture
def builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtCore, QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.005
    win = ScanBuilder(reg)
    for it in win._det_items():
        it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole)
                         == "lockin_r" else QtCore.Qt.Unchecked)
    win.resize(1500, 950)
    win.show()
    yield win
    win.close()


def _row(builder, pid, start, stop, num, fly=True):
    builder.add_axis(pid)
    row = builder.rows[-1]
    row.start.setValue(start); row.stop.setValue(stop); row.num.setValue(num)
    builder.open_advanced(row)
    if fly:
        row.fly.setChecked(True)
    return row


# ─────────────────────────────── what is offered ──────────────────────────────

def test_the_fly_tick_is_enabled_on_a_ramp_knob(builder):
    for pid in ("field", "rf_freq"):
        builder.add_axis(pid)
        row = builder.rows[-1]
        assert row.ramp is not None and not row.streamed
        assert row.fly.isEnabled(), pid
    # a knob with neither a stream nor a ramp: disabled, and the tooltip says why
    builder.add_axis("pos_z")
    row = builder.rows[-1]
    assert not row.fly.isEnabled()
    tip = row.fly.toolTip().replace("\n", " ")
    assert "can neither stream nor sweep (no stream and no ramp block" in tip
    assert "step it" in tip


def test_a_ramp_knob_hides_the_stage_only_boxes(builder):
    row = _row(builder, "field", 10.0, 90.0, 41)
    assert row.is_ramp()
    for w in (row.knob_box, row.knob_lbl, row.readback_box, row.readback_lbl,
              row.move_box):
        assert not w.isVisibleTo(row), w
    # ... and a stage keeps them (pos_x streams and has a speed knob)
    stage = _row(builder, "pos_x", -10.0, 10.0, 21)
    assert not stage.is_ramp()
    assert stage.knob_box.isVisibleTo(stage) and stage.readback_box.isVisibleTo(stage)
    # nothing of the stage path is written for the ramp
    ax = row.to_axis()
    assert "speed_param" not in ax and "readback" not in ax and "move" not in ax


@pytest.mark.parametrize("pid, ramp", [("field", FIELD), ("rf_freq", FREQ)])
def test_the_speed_box_takes_the_ramps_unit_range_and_default(builder, pid, ramp):
    row = _row(builder, pid, 10.0, 90.0, 41) if pid == "field" else \
        _row(builder, pid, 700.0, 1300.0, 21)
    assert row.speed_unit() == ramp["unit"]
    assert row.speed_lbl.text() == ramp["unit"]
    assert (row.speed.minimum(), row.speed.maximum()) == ramp["limits"]
    assert row.speed.value() == ramp["default"]


# ─────────────────────────────── the pace ─────────────────────────────────────

def test_speed_or_row_time_or_neither_is_written(builder):
    row = _row(builder, "field", 10.0, 90.0, 41)
    row.speed.setValue(25.0)
    ax = builder.build_recipe().axes[-1]
    assert ax["speed"] == 25.0 and "row_time_s" not in ax
    row.set_pace("row_time")
    row.row_time.setValue(12.5)
    ax = builder.build_recipe().axes[-1]
    assert ax["row_time_s"] == 12.5 and "speed" not in ax
    assert row.row_time.isVisibleTo(row) and not row.speed.isVisibleTo(row)
    assert "fly 12.5 s/row" in row.tag_texts()
    row.set_pace("default")                    # the module's own default rate
    ax = builder.build_recipe().axes[-1]
    assert "speed" not in ax and "row_time_s" not in ax
    assert row.speed_lbl.text() == "10 mT/s"
    assert "fly 10 mT/s (default)" in row.tag_texts()
    assert builder.build_recipe().validate(builder.registry) == []
    # a STAGE has no default rate to fall back on: not offered there
    stage = _row(builder, "pos_x", -10.0, 10.0, 21)
    assert stage.pace_box.findData("default") < 0
    assert stage.pace_box.findData("row_time") >= 0


@pytest.mark.parametrize("pace", [{"speed": 20.0}, {"row_time_s": 7.5}, {}],
                         ids=["speed", "row_time", "neither"])
def test_a_ramp_fly_axis_loads_and_saves_back_unchanged(builder, tmp_path, pace):
    ax = {"type": "fly", "param": "field", "start": 10.0, "stop": 90.0, "num": 41,
          **pace}
    r = Recipe(axes=[{"type": "linear", "param": "rf_freq", "start": 700.0,
                      "stop": 1300.0, "num": 3}, ax], detectors=["lockin_r"])
    path = tmp_path / "ramp.yaml"
    r.save(path)
    assert builder.load_recipe(Recipe.load(path)) == []
    row = builder.rows[1]
    assert row.is_fly() and row.is_ramp()
    assert row.pace() == ("speed" if "speed" in pace else
                          "row_time" if pace else "default")
    assert builder.build_recipe().axes == r.axes
    # and back through a second save / load
    builder.build_recipe().save(tmp_path / "again.yaml")
    assert Recipe.load(tmp_path / "again.yaml").axes == r.axes


def test_a_ramp_knob_flown_as_a_stage_by_name_keeps_its_speed_param(builder):
    """A loaded recipe that names a speed knob on a ramp knob asked for the
    stage path (flyscan.ramp_of): the key is kept, not dropped."""
    ax = {"type": "fly", "param": "field", "start": 10.0, "stop": 90.0, "num": 41,
          "speed": 5.0, "speed_param": "stage_speed"}
    builder.load_recipe(Recipe(axes=[ax], detectors=["lockin_r"]))
    assert builder.build_recipe().axes == [ax]


# ─────────────────────────────── binned by ────────────────────────────────────

def test_the_row_says_what_it_is_binned_by(builder):
    field = _row(builder, "rf_freq", 700.0, 1300.0, 21, fly=False)
    field.fly.setChecked(True)
    assert field.binned_lbl.text() == "binned by command"
    assert "fly 100 MHz/s (by command)" in field.tag_texts()
    f = _row(builder, "field", 10.0, 90.0, 41)
    assert f.binned_lbl.text() == "binned by measurement"
    assert "fly 10 mT/s" in f.tag_texts()
    assert not any("by command" in t for t in f.tag_texts())
    stage = _row(builder, "pos_x", -10.0, 10.0, 21)
    assert stage.binned_lbl.text() == "binned by measurement"


# ─────────────────────────────── the estimate ─────────────────────────────────

def test_the_eta_counts_the_row_time_and_the_default_rate(builder, monkeypatch):
    import apps.scan_builder as sb
    seen = []
    real = sb.row_seconds

    def spy(ax, registry=None):
        seen.append(registry)
        return real(ax, registry)

    monkeypatch.setattr(sb, "row_seconds", spy)
    _row(builder, "rf_freq", 700.0, 1300.0, 3, fly=False)
    row = _row(builder, "field", 10.0, 90.0, 41)
    row.set_pace("row_time")
    row.row_time.setValue(8.0)
    builder._rebuild_summary()
    assert seen and seen[-1] is builder.registry
    assert "3 row(s) × 8 s" in builder.detail.text()
    # the module's default 10 mT/s over 80 mT + one 2 mT pixel = 8.2 s a row
    row.set_pace("default")
    builder._rebuild_summary()
    assert "3 row(s) × 8.2 s" in builder.detail.text()


# ─────────────────────────────── copy from ────────────────────────────────────

def test_copy_between_a_ramp_and_a_stage_skips_the_speed_keeps_the_row_time(builder):
    field = _row(builder, "field", 10.0, 90.0, 41)
    stage = _row(builder, "pos_x", -10.0, 10.0, 21)
    field.speed.setValue(20.0)
    before = stage.speed.value()
    msg = stage.copy_from(field)
    assert stage.is_fly() and stage.speed.value() == before
    assert "skipped: speed (20 mT/s does not fit an axis in um/s)" in msg
    # a row time is seconds whatever the knob: it fits
    field.set_pace("row_time")
    field.row_time.setValue(30.0)
    msg = stage.copy_from(field)
    assert stage.pace() == "row_time" and stage.row_time.value() == 30.0
    assert "row time 30 s" in msg and "skipped" not in msg
    assert builder.build_recipe().axes[-1].get("row_time_s") == 30.0
    # the other way: a um/s speed onto the field is skipped too
    stage.set_pace("speed"); stage.speed.setValue(5.0)
    field.set_pace("speed"); field.speed.setValue(12.0)
    msg = field.copy_from(stage)
    assert field.speed.value() == 12.0
    assert "skipped: speed (5 um/s does not fit an axis in mT/s)" in msg


# ─────────────────────────────── end to end ───────────────────────────────────

def _peak_rows(ds, det, dim):
    """The resonance's place along `dim` per row, in pixels (the centroid of
    the response above its row minimum, as tests/test_fly_ramp.py does)."""
    da = ds[det].transpose(..., dim)
    v = np.nan_to_num(da.values, nan=0.0)
    w = np.clip(v - v.min(axis=-1, keepdims=True), 0, None)
    idx = np.arange(v.shape[-1])
    return (w * idx).sum(axis=-1) / w.sum(axis=-1)


def test_a_field_flown_map_built_in_the_window_runs_like_the_demo(builder):
    """run_fly_any_demo.py's FIELD-flown map (field 10..90 mT in 41 pixels at
    30 mT/s, zig-zag, off the islands), with two frequency rows instead of
    21: built in the window, it is the demo's recipe, and it runs on the
    simulator onto the same grid and the same resonance as the stepped map."""
    builder.add_fixed("pos_x", 30.0)
    builder.add_fixed("pos_y", 50.0)
    _row(builder, "rf_freq", 850.0, 950.0, 2, fly=False)
    row = _row(builder, "field", 10.0, 90.0, 41)
    row.speed.setValue(30.0)
    builder.zigzag_box.setChecked(True)
    builder.per_pt.setValue(0.0)
    r = builder.build_recipe()
    # the demo's axes and conditions (run_fly_any_demo.py, 'field' recipe)
    assert r.axes == [{"type": "linear", "param": "rf_freq", "start": 850.0,
                       "stop": 950.0, "num": 2},
                      {"type": "fly", "param": "field", "start": 10.0, "stop": 90.0,
                       "num": 41, "speed": 30.0}]
    assert r.fixed == {"pos_x": 30.0, "pos_y": 50.0} and r.zigzag
    builder.run_scan(block=True)
    ds = builder.dataset
    assert ds["field"].attrs["fly_binned_by"] == "measurement"
    assert ds["field"].attrs["fly_mode"] == "ramp"
    np.testing.assert_allclose(ds["field"].values, np.linspace(10.0, 90.0, 41))
    np.testing.assert_allclose(ds["rf_freq"].values, [850.0, 950.0])
    n = ds["lockin_r_n"].values
    assert np.nanmedian(n) >= 4 and np.all(n[:, 1:-1] > 0)
    # the stepped reference: the resonance on the same field pixel (+-1)
    step = Recipe(axes=[r.axes[0], {"type": "linear", "param": "field", "start": 10.0,
                                    "stop": 90.0, "num": 41}],
                  detectors=["lockin_r"], fixed=dict(r.fixed))
    ref = run(step, builder.registry)
    a = _peak_rows(ds, "lockin_r", "field")
    b = _peak_rows(ref, "lockin_r", "field")
    assert np.all(np.abs(a - b) <= 1), (a, b)
    assert not builder.registry.get("field")._sim_ramp.running
