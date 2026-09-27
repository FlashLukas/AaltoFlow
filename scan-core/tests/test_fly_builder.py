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
