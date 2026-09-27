"""The fly option in the Scan Builder: a checkbox on an axis row.

Ticked, the row becomes a `type: fly` axis with a speed (and the module's
speed knob, found by name), `pts` read as pixels, and the summary estimates
the time per ROW. It is offered only where the module streams the position,
survives a save / load, and a scan run from the builder is a fly scan.
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

from scan_core import build_sim_registry                               # noqa: E402


@pytest.fixture
def builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtCore, QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    reg = build_sim_registry()
    reg._state.lockin_tc_s = 0.004
    win = ScanBuilder(reg)
    for it in win._det_items():
        it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole)
                         == "lockin_r" else QtCore.Qt.Unchecked)
    yield win
    win.close()


def _fly_row(builder, pid="pos_x", speed=40.0):
    builder.add_axis(pid)
    row = builder.rows[-1]
    row.start.setValue(-10.0)
    row.stop.setValue(10.0)
    row.num.setValue(21)
    row.fly.setChecked(True)
    row.speed.setValue(speed)
    return row


def test_ticking_fly_makes_a_fly_axis_with_the_speed_knob(builder):
    row = _fly_row(builder)
    assert row.fly.isEnabled() and row.speed.isVisibleTo(row)
    assert row.num_lbl.text() == "pixels"
    ax = builder.build_recipe().axes[-1]
    assert ax == {"type": "fly", "param": "pos_x", "start": -10.0, "stop": 10.0,
                  "num": 21, "speed": 40.0, "speed_param": "stage_speed"}
    assert "(fly)" in builder.summary.text()
    assert "row(s)" in builder.detail.text()
    row.fly.setChecked(False)
    assert builder.build_recipe().axes[-1]["type"] == "linear"
    assert row.num_lbl.text() == "pts" and not row.speed.isVisibleTo(row)


def test_fly_is_offered_only_where_the_position_is_streamed(builder):
    builder.add_axis("pos_z")
    assert not builder.rows[-1].fly.isEnabled()
    builder.add_axis("pos_y")
    assert builder.rows[-1].fly.isEnabled()


def test_a_fly_row_that_is_not_innermost_shows_as_invalid(builder):
    _fly_row(builder)
    builder.add_axis("field")
    assert "invalid" in builder.summary.text()
    assert "innermost" in builder.detail.text()


def test_a_fly_definition_comes_back_when_loaded(builder, tmp_path):
    _fly_row(builder, speed=25.0)
    builder.zigzag_box.setChecked(True)
    recipe = builder.build_recipe()
    builder.load_recipe(recipe.__class__(name="other", axes=[]))
    assert builder.rows == []
    assert builder.load_recipe(recipe) == []
    row = builder.rows[-1]
    assert row.fly.isChecked() and row.speed.value() == 25.0
    assert builder.build_recipe().axes == recipe.axes


def test_running_from_the_builder_flies(builder):
    _fly_row(builder, speed=60.0)
    builder.per_pt.setValue(0.0)
    builder.run_scan(block=True)
    ds = builder.dataset
    assert ds["pos_x"].attrs.get("fly") == "true"
    assert np.all(ds["lockin_r_n"].values >= 2)
    assert np.all(np.isfinite(ds["lockin_r"].values))


def _camera_rig():
    """A registry shaped like the KIM rig: a MEASURED camera coordinate (it
    streams, has no speed knob) and a KIM stage whose axes do."""
    from scan_core.registry import Registry, Settable, StreamSpec
    reg = Registry()
    spec = StreamSpec("g", lambda: None, lambda: {})
    for pid, unit in (("camera.laser_x", "um"), ("camera.laser_y", "um"),
                      ("kim.position_x", "um"), ("kim.position_y", "um"),
                      ("kim.velocity_x", "um/s"), ("kim.velocity_y", "um/s")):
        p = reg.add(Settable(pid, pid, unit, (-50, 50) if unit == "um" else (0.1, 40),
                             lambda v: None, lambda: 2.0))
        if pid.startswith("camera"):
            p.stream, p.stream_channel = spec, pid[-7:]
    return reg


def test_a_measured_coordinate_gets_a_move_with_box(builder):
    builder.set_registry(_camera_rig())
    builder.add_axis("camera.laser_x")
    row = builder.rows[-1]
    assert row.move_choices == ["kim.position_x", "kim.position_y"]
    row.fly.setChecked(True)
    assert row.move_box.isVisibleTo(row)
    row.move_box.setCurrentIndex(1)            # the rig's camera is rotated 90 deg
    row.speed.setValue(3.0)
    ax = builder.build_recipe().axes[-1]
    assert ax["move"] == "kim.position_y" and ax["speed_param"] == "kim.velocity_y"
    assert ax["param"] == "camera.laser_x" and ax["speed"] == 3.0
    # and back from a saved definition
    recipe = builder.build_recipe()
    assert builder.load_recipe(recipe) == []
    assert builder.rows[-1].move_param() == "kim.position_y"
    assert builder.build_recipe().axes == recipe.axes


def test_a_stage_position_gets_no_move_with_box(builder):
    builder.set_registry(_camera_rig())
    builder.add_axis("kim.position_x")
    assert builder.rows[-1].move_choices == []


def test_ticking_fly_greys_the_detectors_that_cannot_fly_and_gives_them_back(builder):
    from PySide6 import QtCore
    items = {it.data(0, QtCore.Qt.UserRole): it for it in builder._det_items()}
    items["s21"].setCheckState(0, QtCore.Qt.Checked)        # a whole trace: cannot fly
    row = _fly_row(builder)
    assert items["s21"].isDisabled() and items["s21"].checkState(0) == QtCore.Qt.Unchecked
    assert "cannot be recorded in a FLY scan" in items["s21"].toolTip(0)
    assert not items["lockin_r"].isDisabled()                # streams: stays available
    assert "set aside while flying" in builder.detail.text()
    assert builder.build_recipe().validate(builder.registry) == []
    row.fly.setChecked(False)                                # back to stepping
    assert not items["s21"].isDisabled()
    assert items["s21"].checkState(0) == QtCore.Qt.Checked   # the selection came back
