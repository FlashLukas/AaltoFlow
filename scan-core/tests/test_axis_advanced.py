"""ADVANCED AXIS SETTINGS in the Scan Builder (2026-10-09).

Lukas approved the mockup: each axis row keeps only index, parameter, unit,
from / to / pts and its buttons, plus an "Advanced" button that opens a panel
IN PLACE under the row (one at a time) with three groups -- FLY, SCOUT,
POINT -- and "Copy from axis..." / "Reset". Whatever is not the default shows
as an amber TAG on the row, so nothing is hidden silently. The scout margin
became PER AXIS (the Scout pass section's single box showed only the largest).

Pinned here: the tags follow the settings; one panel open at a time; copy
takes only what fits (and says what it skipped); reset; the panel builds the
same recipe the old ticks did (and every option of the recipe has a box);
loaded recipes fill the right panel; the layout does not jump; each axis's
advanced settings are attributes of its coordinate in the data file.
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
from scan_core.recipe import axis_attrs                                # noqa: E402

RECIPES = Path(__file__).resolve().parents[1] / "recipes"
RASTER = {"type": "raster",
          "x": {"param": "pos_x", "start": -45, "stop": 45, "num": 31},
          "y": {"param": "pos_y", "start": -45, "stop": 45, "num": 31}}


@pytest.fixture
def builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtCore, QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    for it in win._det_items():
        it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole)
                         == "lockin_r" else QtCore.Qt.Unchecked)
    win.resize(1500, 950)
    win.show()
    yield win
    win.close()


def _pump():
    from PySide6 import QtWidgets
    for _ in range(5):
        QtWidgets.QApplication.processEvents()


def _row(builder, pid):
    return next(r for r in builder.rows if r.param.id == pid)


def _top(builder, w):
    return w.mapTo(builder, w.rect().topLeft()).y()


# ─────────────────────────────── the row itself ───────────────────────────────

def test_the_row_keeps_only_the_sweep_and_an_advanced_button(builder):
    builder.add_axis("pos_x")
    row = builder.rows[0]
    # the fly / scout controls are in the (closed) panel, not on the line
    for w in (row.fly, row.speed, row.scout, row.scout_step):
        assert row.advanced.isAncestorOf(w)
    assert not row.advanced.isVisibleTo(row)
    assert row.adv_btn.text() == row.ADV_CLOSED and "Advanced" in row.adv_btn.text()
    # the from / to / pts boxes are no narrower than before (84 px)
    for w in (row.start, row.stop, row.num):
        assert w.width() >= 84 or w.minimumWidth() >= 84
    assert row.tag_texts() == []


def test_tags_reflect_the_settings(builder):
    builder.add_axis("pos_y")
    builder.add_axis("pos_x")
    y, x = builder.rows
    x.fly.setChecked(True)
    x.speed.setValue(5.0)
    assert x.tag_texts() == ["fly 5 um/s"]
    builder.zigzag_box.setChecked(True)               # the scan-wide setting ...
    assert "zig-zag" in x.tag_texts()                 # ... shows on the fly row
    assert x.dir_box.currentData() is True
    x.lag_box.setChecked(False)
    x.timeout_auto.setChecked(False); x.timeout_spin.setValue(90)
    assert {"no lag correction", "row max 90 s"} <= set(x.tag_texts())
    y.scout.setChecked(True)
    y.scout_step.setValue(3)
    assert y.tag_texts() == ["scout x3"]
    y.margin_auto.setChecked(False); y.margin_spin.setValue(2)
    assert y.tag_texts() == ["scout x3, margin 2"]
    y.name_edit.setText("y_um"); y.name_edit.editingFinished.emit()
    assert "name y_um" in y.tag_texts()
    # ... and the tags are really on the row (amber labels, in its line)
    shown = [y.tags_box.itemAt(i).widget().text() for i in range(y.tags_box.count() - 1)]
    assert shown == y.tag_texts()
    y.reset_advanced()
    assert y.tag_texts() == []


def test_the_direction_box_is_the_scans_zig_zag(builder):
    builder.add_axis("pos_x")
    row = builder.rows[0]
    row.fly.setChecked(True)
    row.dir_box.setCurrentIndex(row.dir_box.findData(True))
    assert builder.zigzag_box.isChecked() and builder.build_recipe().zigzag
    builder.zigzag_box.setChecked(False)
    assert row.dir_box.currentData() is False


# ─────────────────────────────── open / close ─────────────────────────────────

def test_advanced_opens_under_its_row_and_only_one_at_a_time(builder):
    builder.add_axis("field")
    builder.add_axis("pos_x")
    a, b = builder.rows
    a.adv_btn.click()
    _pump()
    assert a.advanced_open() and a.adv_btn.text().startswith("^")
    assert a.advanced.isVisible()
    # IN PLACE: right under its own row line, above the next row
    assert _top(builder, a.advanced) > _top(builder, a.level_lbl)
    assert _top(builder, a.advanced) < _top(builder, b)
    b.adv_btn.click()
    _pump()
    assert b.advanced_open() and not a.advanced_open()
    assert a.adv_btn.text() == a.ADV_CLOSED
    b.adv_btn.click()
    assert not b.advanced_open()


def test_opening_advanced_moves_nothing_below_the_axis_stack(builder):
    for pid in ("rf_freq", "pos_y", "pos_x"):
        builder.add_axis(pid)
    _pump()
    cards = (builder.scout_section, builder.routines_card)
    before = [_top(builder, w) for w in cards]
    stack_h = builder.axis_scroll.height()
    builder.open_advanced(builder.rows[1])
    _pump()
    assert [_top(builder, w) for w in cards] == before
    assert builder.axis_scroll.height() == stack_h
    # the content grew INSIDE the axis list (it scrolls), the list kept its rows
    assert builder.axis_scroll.height() >= builder.AXIS_ROWS_MIN * builder.AXIS_ROW_PX - 1
    builder.scout_section.set_expanded(True)          # ... also with the scout open
    _pump()
    assert _top(builder, builder.routines_card) == before[1]
    builder.rows[1].set_advanced_open(False)
    _pump()
    assert _top(builder, builder.routines_card) == before[1]


def test_the_groups_wrap_to_one_column_when_narrow(builder):
    from PySide6 import QtWidgets
    builder.add_axis("pos_x")
    row = builder.rows[0]
    builder.open_advanced(row)
    _pump()
    # the suite's Scan tab is ~1100 px wide here: FLY, SCOUT, POINT side by side
    row.advanced.resize(1100, row.advanced.height())
    row.advanced.arrange()
    assert row.advanced.groups_box.direction() == QtWidgets.QBoxLayout.LeftToRight
    row.advanced.resize(300, row.advanced.height())
    row.advanced.arrange()
    assert row.advanced.groups_box.direction() == QtWidgets.QBoxLayout.TopToBottom


def test_the_repeat_row_shows_only_what_applies(builder):
    rep = builder.add_repeat(num=3)
    assert not hasattr(rep, "fly_group") and not hasattr(rep, "scout_group")
    assert not hasattr(rep, "copy_btn")               # nothing to copy onto a repeat
    assert rep.point_group in rep.advanced.groups
    rep.name_edit.setText("run"); rep.name_edit.editingFinished.emit()
    assert rep.tag_texts() == ["name run"]
    assert builder.build_recipe().axes[0]["name"] == "run"
    rep.reset_advanced()
    assert "name" not in builder.build_recipe().axes[0]


# ─────────────────────────────── copy / reset ─────────────────────────────────

def test_copy_from_a_compatible_axis(builder):
    builder.add_axis("pos_y")
    builder.add_axis("pos_x")
    y, x = builder.rows
    y.fly.setChecked(True)                 # (only to have something to copy)
    y.speed.setValue(7.5)
    y.lag_box.setChecked(False)
    y.scout.setChecked(True); y.scout_step.setValue(4)
    msg = x.copy_from(y)
    assert x.is_fly() and x.speed.value() == 7.5 and not x.lag_box.isChecked()
    assert x.is_scout() and x.scout_step.value() == 4
    assert "skipped" not in msg and "speed 7.5 um/s" in msg
    assert x.dim_name() == ""                          # the name is never copied
    assert "copied from Position Y" in x.adv_note.text() or "Position Y" in msg


def test_copy_skips_what_does_not_fit_and_says_so(builder):
    logged = []
    builder.on_log = logged.append
    builder.add_axis("rf_freq")
    builder.add_axis("pos_x")
    f, x = builder.rows
    x.fly.setChecked(True); x.speed.setValue(5.0)
    x.scout.setChecked(True); x.scout_step.setValue(2)
    msg = f.copy_from(x)
    assert not f.is_fly()                              # a frequency does not stream
    assert "skipped: fly" in msg
    assert f.is_scout() and f.scout_step.value() == 2  # the scout fits any axis
    assert logged and logged[-1] == msg
    # from a repeat row: nothing
    rep = builder.add_repeat(num=2)
    assert "nothing copied" in x.copy_from(rep)
    assert x.is_fly() and x.is_scout()


def test_copy_skips_a_speed_whose_unit_does_not_fit(builder):
    """Two streamed axes in different units: fly is copied, the speed is not
    (5 um/s means nothing to a rotation stage)."""
    from scan_core.registry import Registry, Settable, StreamSpec
    reg = Registry()
    spec = StreamSpec("g", lambda: None, lambda: {})
    for pid, unit, lim in (("stage.position_x", "um", (-50, 50)),
                           ("stage.velocity_x", "um/s", (0.1, 40)),
                           ("rot.angle", "deg", (0, 360)),
                           ("rot.velocity", "deg/s", (0.1, 20))):
        p = reg.add(Settable(pid, pid, unit, lim, lambda v: None, lambda: 2.0))
        if pid in ("stage.position_x", "rot.angle"):
            p.stream, p.stream_channel = spec, pid
    builder.set_registry(reg)
    builder.add_axis("rot.angle")
    builder.add_axis("stage.position_x")
    a, x = builder.rows
    x.fly.setChecked(True); x.speed.setValue(5.0)
    before = a.speed.value()
    msg = a.copy_from(x)
    assert a.is_fly() and a.speed.value() == before
    assert "skipped: speed (5 um/s does not fit an axis in deg/s)" in msg


def test_reset_goes_back_to_plain_stepping(builder):
    builder.add_axis("pos_x")
    row = builder.rows[0]
    plain = builder.build_recipe().axes[0]
    row.fly.setChecked(True); row.speed.setValue(9.0); row.lag_box.setChecked(False)
    row.readback_box.setCurrentIndex(row.readback_box.findData("pos_y"))
    row.scout.setChecked(True)
    row.name_edit.setText("x"); row.name_edit.editingFinished.emit()
    assert builder.build_recipe().axes[0]["type"] == "fly"
    row.reset_btn.click()
    assert builder.build_recipe().axes[0] == plain
    assert row.tag_texts() == [] and not row.is_scout()
    assert builder.build_recipe().scout is None


# ─────────────────────────── the recipe it builds ─────────────────────────────

def test_fly_set_in_advanced_builds_what_the_old_tick_did(builder):
    builder.add_axis("pos_x")
    row = builder.rows[0]
    row.start.setValue(-10.0); row.stop.setValue(10.0); row.num.setValue(21)
    builder.open_advanced(row)
    row.fly.setChecked(True)
    row.speed.setValue(40.0)
    # exactly the dict the old "fly" tick + speed box produced
    assert builder.build_recipe().axes[-1] == {
        "type": "fly", "param": "pos_x", "start": -10.0, "stop": 10.0,
        "num": 21, "speed": 40.0, "speed_param": "stage_speed"}


def test_every_fly_option_has_a_box_and_round_trips(builder, tmp_path):
    """The demo's fly axis (run_fly_demo.py) with every option the recipe
    knows -- once a loaded fly axis with any of these was passed through
    untouched ("raw"), now each has its box."""
    ax = {"type": "fly", "param": "pos_x", "start": -40.0, "stop": 40.0, "num": 81,
          "speed": 30.0, "speed_param": "stage_speed", "lag_correction": False,
          "readback": "pos_y", "timeout_s": 90.0, "name": "x_fly"}
    r = Recipe(axes=[{"type": "linear", "param": "pos_y", "start": -45.0,
                      "stop": 45.0, "num": 3}, ax],
               detectors=["lockin_r"], zigzag=True)
    path = tmp_path / "fly.yaml"
    r.save(path)
    assert builder.load_recipe(Recipe.load(path)) == []
    row = builder.rows[1]
    assert row.raw is None and row.is_fly()
    assert not row.lag_box.isChecked() and row.readback_box.currentData() == "pos_y"
    assert not row.timeout_auto.isChecked() and row.timeout_spin.value() == 90.0
    assert row.dim_name() == "x_fly" and row.dir_box.currentData() is True
    assert set(row.tag_texts()) >= {"fly 30 um/s", "zig-zag", "no lag correction",
                                    "row max 90 s", "readback pos_y", "name x_fly"}
    assert builder.build_recipe().axes == r.axes
    assert builder.build_recipe().zigzag


def test_unknown_axis_keys_survive_the_builder(builder):
    r = Recipe(axes=[{"type": "linear", "param": "field", "start": 0.0, "stop": 10.0,
                      "num": 5, "note": "kept"}], detectors=["lockin_r"])
    builder.load_recipe(r)
    assert builder.build_recipe().axes == r.axes


def test_scout_set_in_advanced_builds_the_scout_xy_recipe(builder):
    ref = Recipe.load(RECIPES / "scout_xy.yaml")
    builder.load_recipe(Recipe(axes=[dict(ref.axes[0])], detectors=["lockin_r"]))
    row = builder.rows[0]
    builder.open_advanced(row)
    row.scout.setChecked(True)
    row.scout_step.setValue(3)
    sec = builder.scout_section
    sec.det_box.setCurrentIndex(sec.det_box.findData("reflectivity"))
    assert builder.build_recipe().scout == ref.scout
    assert row.tag_texts() == ["scout x3"]


@pytest.mark.parametrize("path", sorted(RECIPES.glob("*.yaml")), ids=lambda p: p.stem)
def test_every_example_recipe_comes_back_unchanged(builder, path):
    """Old recipes load and save back unchanged: axes, scout block, zig-zag."""
    ref = Recipe.load(path)
    if builder.load_recipe(ref):
        pytest.skip("names a parameter the simulator does not have")
    got = builder.build_recipe()
    # (a repeat row has always written its default mode out: not new)
    want = [{"mode": "keep", **ax} if ax.get("type") == "repeat" else ax
            for ax in ref.axes]
    assert got.axes == want
    assert got.scout == ref.scout
    assert got.zigzag == ref.zigzag


def test_per_axis_margins_show_on_their_rows_and_save_back(builder, tmp_path):
    r = Recipe(axes=[{"type": "linear", "param": "pos_y", "start": -45.0, "stop": 45.0,
                      "num": 31},
                     {"type": "linear", "param": "pos_x", "start": -45.0, "stop": 45.0,
                      "num": 31}],
               detectors=["lockin_r"],
               scout={"axes": {"pos_x": 3, "pos_y": 5}, "detector": "reflectivity",
                      "keep": "above", "threshold": "auto",
                      "margin": {"pos_x": 2.0, "pos_y": "auto"}})
    assert builder.load_recipe(r) == []
    y, x = builder.rows
    assert not x.margin_auto.isChecked() and x.margin_spin.value() == 2.0
    assert y.margin_auto.isChecked() and y.scout_step.value() == 5
    assert x.tag_texts() == ["scout x3, margin 2"] and y.tag_texts() == ["scout x5"]
    assert builder.build_recipe().scout == r.scout
    # the Scout pass section's line says it per axis
    assert "pos_x every 3 (margin 2)" in builder.scout_section.state.text()
    # one number for all axes is written as one number, as before
    y.margin_auto.setChecked(False); y.margin_spin.setValue(2.0)
    assert builder.build_recipe().scout["margin"] == 2.0
    assert builder.build_recipe().validate(builder.registry) == []


def test_a_raster_with_two_margins_keeps_them_until_edited(builder):
    r = Recipe(axes=[RASTER], detectors=["lockin_r"],
               scout={"axes": {"pos_x": 3, "pos_y": 3}, "detector": "reflectivity",
                      "keep": "above", "threshold": "auto",
                      "margin": {"pos_x": 1.0, "pos_y": 2.5}})
    builder.load_recipe(r)
    row = builder.rows[0]
    assert builder.build_recipe().scout["margin"] == {"pos_x": 1.0, "pos_y": 2.5}
    assert "margin per axis" in row.tag_texts()[0]
    assert not row.scout_note.isHidden() and "pos_y 2.5 pts" in row.scout_note.text()
    row.margin_spin.setValue(1.5)                      # an edit sets both
    assert builder.build_recipe().scout["margin"] == 1.5


def test_point_lists_the_routines_on_the_axis(builder):
    builder.add_axis("pos_y")
    builder.add_axis("pos_x")
    sec = builder.add_throughout({"when": "each_sweep", "axis": "pos_x",
                                  "edge": "start", "action": "call",
                                  "args": {"action": "sim_autofocus"}})
    sec.add_action("sim_autofocus")
    builder._rebuild_summary()
    y, x = builder.rows
    assert "sim_autofocus" in x.routines_lbl.text()
    assert "each sweep of pos_x" in x.routines_lbl.text()
    assert "none on this axis" in y.routines_lbl.text()
    assert sec is not None


# ─────────────────────────────── in the data file ─────────────────────────────

def test_axis_attrs_only_what_is_set():
    r = Recipe(axes=[{"type": "linear", "param": "field", "start": 0, "stop": 1, "num": 2}])
    assert axis_attrs(r) == {}
    r = Recipe(axes=[{"type": "linear", "param": "pos_y", "start": 0, "stop": 1, "num": 2},
                     {"type": "fly", "param": "pos_x", "start": 0, "stop": 1, "num": 5,
                      "speed": 3.0, "speed_param": "stage_speed"}], zigzag=True)
    a = axis_attrs(r, lambda pid: "um")
    assert set(a) == {"pos_x"}
    assert a["pos_x"] == {"fly": "true", "fly_speed": 3.0, "fly_speed_units": "um/s",
                          "fly_speed_param": "stage_speed", "fly_lag_correction": 1,
                          "fly_zigzag": 1, "fly_binned_by": "measurement"}


def test_the_coordinates_carry_the_fly_settings_in_the_file(tmp_path):
    xr = pytest.importorskip("xarray")
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.003
    r = Recipe(axes=[{"type": "linear", "param": "pos_y", "start": -5, "stop": 5, "num": 2},
                     {"type": "fly", "param": "pos_x", "start": -10, "stop": 10, "num": 11,
                      "speed": 80.0, "speed_param": "stage_speed", "timeout_s": 60.0,
                      "lag_correction": False}],
               detectors=["lockin_r"], zigzag=True)
    ds = run(r, reg)
    path = tmp_path / "fly.nc"
    ds.to_netcdf(path)
    with xr.open_dataset(path) as back:
        a = back["pos_x"].attrs
        assert a["fly"] == "true"                      # what readers test for, as before
        assert float(a["fly_speed"]) == 80.0 and a["fly_speed_units"] == "um/s"
        assert a["fly_speed_param"] == "stage_speed"
        assert int(a["fly_lag_correction"]) == 0 and int(a["fly_zigzag"]) == 1
        assert float(a["fly_timeout_s"]) == 60.0
        # a plain stepped axis gets none of it
        assert not any(k.startswith(("fly", "scout")) for k in back["pos_y"].attrs)
        assert back["pos_y"].attrs["units"] == "um"


def test_the_coordinates_carry_the_scout_settings_in_the_file(tmp_path):
    xr = pytest.importorskip("xarray")
    r = Recipe(axes=[{"type": "linear", "param": "pos_y", "start": -45, "stop": 45, "num": 13},
                     {"type": "linear", "param": "pos_x", "start": -45, "stop": 45, "num": 13}],
               detectors=["lockin_r"],
               scout={"axes": {"pos_x": 3, "pos_y": 4}, "detector": "reflectivity",
                      "margin": {"pos_y": 2.0}})
    ds = run(r, build_sim_registry())
    path = tmp_path / "scout.nc"
    ds.to_netcdf(path)
    with xr.open_dataset(path) as back:
        x, y = back["pos_x"].attrs, back["pos_y"].attrs
        assert int(x["scout_every"]) == 3 and x["scout_margin"] == "auto"
        assert float(x["scout_margin_points"]) == 1.5  # auto = half the step
        assert int(y["scout_every"]) == 4 and float(y["scout_margin_points"]) == 2.0
        assert "scout_margin" not in y
        # the old recipe still loads from the file
        from scan_core import scan_queue
        again = scan_queue.recipe_from_file(str(path))
        assert again.scout == r.scout and again.axes == r.axes


def test_a_file_written_before_this_change_loads_unchanged(builder, tmp_path):
    """An .nc without the new attributes (as every file before 2026-10-09):
    its recipe_json is what loads, and it comes back as it was."""
    xr = pytest.importorskip("xarray")
    r = Recipe(axes=[{"type": "linear", "param": "field", "start": 0.0, "stop": 20.0,
                      "num": 3}], detectors=["lockin_r"])
    ds = run(r, build_sim_registry())
    for c in ds.coords:
        for k in [k for k in ds[c].attrs if k.startswith(("fly_", "scout_"))]:
            del ds[c].attrs[k]
    path = tmp_path / "old.nc"
    ds.to_netcdf(path)
    assert builder.load_recipe(builder.recipe_from_file(str(path))) == []
    assert builder.build_recipe().axes == r.axes
    assert np.isfinite(xr.open_dataset(path)["lockin_r"].values).all()
