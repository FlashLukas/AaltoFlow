"""The RESONANCE WINDOW card in the Scan Builder.

Hidden until a ticked detector supports a window; when switched on it becomes
the recipe's `window` block, survives a save and a load (.yaml and the .nc the
scan writes), shows a live readout while the scan runs, and a missing field
parameter on load is flagged like any other missing id.
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

from scan_core import Recipe, build_sim_registry                       # noqa: E402


@pytest.fixture
def builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    yield win
    win.close()


def _tick(win, ids):
    from PySide6 import QtCore
    for it in win._det_items():
        it.setCheckState(0, QtCore.Qt.Checked if it.data(0, QtCore.Qt.UserRole) in ids
                         else QtCore.Qt.Unchecked)


def _windowed(win):
    win.add_axis("field")
    row = win.rows[-1]
    row.start.setValue(30); row.stop.setValue(190); row.num.setValue(12)
    _tick(win, {"fmr"})
    card = win.window_card
    card.enable.setChecked(True)
    card.field_box.setCurrentIndex(card.field_box.findData("field"))
    card.angle_box.setCurrentIndex(card.angle_box.findData("field_angle"))
    card.meff_spin.setValue(1750.0)
    card.hk_spin.setValue(5.0)
    card.margin_spin.setValue(300.0)
    card.full_spin.setValue(0)
    return card


def test_the_card_appears_only_for_a_windowable_detector(builder):
    card = builder.window_card
    _tick(builder, {"lockin_r"})
    builder._rebuild_summary()
    assert card.isHidden()
    assert builder.build_recipe().window is None
    _tick(builder, {"lockin_r", "fmr"})
    builder._rebuild_summary()
    assert not card.isHidden()
    assert builder.build_recipe().window is None          # shown, but off


def test_window_block_round_trip_and_run(builder, tmp_path):
    card = _windowed(builder)
    r = builder.build_recipe()
    assert r.window["detector"] == "fmr" and r.window["field"] == "field"
    assert r.window["angle"] == "field_angle" and r.window["model"] == "inplane"
    assert r.validate(builder.registry) == []
    assert "window ±300 MHz on fmr" in builder.detail.text()

    path = tmp_path / "w.yaml"
    r.save(path)
    card.load_block(None)
    assert builder.build_recipe().window is None
    assert builder.load_recipe(Recipe.load(path)) == []
    assert builder.build_recipe().window == r.window

    builder.run_scan(block=True)
    ds = builder.dataset
    assert "fmr_measured" in ds and ds["fmr_full_sweep"].values[0]
    assert "f_res predicted" in card.live.text()
    nc = tmp_path / "w.nc"
    ds.to_netcdf(nc)
    builder.window_card.load_block(None)
    assert builder.load_recipe(builder.recipe_from_file(str(nc))) == []
    assert builder.build_recipe().window == r.window


def test_a_fixed_angle_and_out_of_plane(builder):
    card = _windowed(builder)
    card.angle_box.setCurrentIndex(card.angle_box.findData(""))
    card.angle_fixed.setValue(30.0)
    card.model_box.setCurrentIndex(card.model_box.findData("outofplane"))
    w = builder.build_recipe().window
    assert w["angle"] == 30.0 and w["model"] == "outofplane"
    assert not card.hk_spin.isEnabled()                   # no Hk out of plane


def test_a_missing_field_parameter_is_flagged_on_load(builder):
    _windowed(builder)
    r = builder.build_recipe()
    r.window["field"] = "mag2d.field"
    missing = builder.load_recipe(r)
    assert "mag2d.field" in missing
    assert builder.build_recipe().window["field"] == "mag2d.field"
    assert any("field" in e for e in builder.build_recipe().validate(builder.registry))
