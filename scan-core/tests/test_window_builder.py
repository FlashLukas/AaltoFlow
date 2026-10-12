"""The WINDOW card in the Scan Builder.

Hidden until a ticked detector supports a window; when switched on it becomes
the recipe's `window` block, survives a save and a load (.yaml and the .nc the
scan writes), shows a live readout while the scan runs, and a missing input
parameter on load is flagged like any other missing id. Its rows are built
from the chosen PREDICTOR's declaration (2026-10-12): Kittel, linear and the
typed expression with its live check each round-trip and run.
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
    card.set_predictor("kittel")
    card.input_box("field").setCurrentIndex(card.input_box("field").findData("field"))
    card.input_box("angle").setCurrentIndex(card.input_box("angle").findData("field_angle"))
    card.param_spin("meff_mT").setValue(1750.0)
    card.param_spin("hk_mT").setValue(5.0)
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
    w = r.window
    assert w["detector"] == "fmr" and w["predictor"] == "kittel"
    assert w["inputs"] == {"field": "field", "angle": "field_angle"}
    assert w["options"] == {"model": "inplane"} and w["margin_unit"] == "MHz"
    assert w["params"]["meff_mT"] == 1750.0 and w["params"]["hk_mT"] == 5.0
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
    assert "peak predicted" in card.live.text() and "μ0Meff" in card.live.text()
    nc = tmp_path / "w.nc"
    ds.to_netcdf(nc)
    builder.window_card.load_block(None)
    assert builder.load_recipe(builder.recipe_from_file(str(nc))) == []
    assert builder.build_recipe().window == r.window


def test_an_old_fmr_block_loads_into_the_kittel_rows(builder):
    _windowed(builder)
    old = {"detector": "fmr", "field": "field", "angle": 30.0, "model": "outofplane",
           "params": {"g": 2.1, "meff_mT": 1500.0}, "margin_MHz": 0.25e3,
           "full_every": 5}
    card = builder.window_card
    assert card.load_block(old) == []
    w = card.to_block()
    assert w["predictor"] == "kittel" and w["options"]["model"] == "outofplane"
    assert w["inputs"] == {"field": "field", "angle": 30.0}
    assert w["params"]["g"] == 2.1 and w["margin"] == 250.0 and w["full_every"] == 5


def test_a_fixed_angle_and_out_of_plane(builder):
    card = _windowed(builder)
    card.input_box("angle").setCurrentIndex(card.input_box("angle").findData(""))
    card.input_fixed("angle").setValue(30.0)
    model = card.option_widget("model")
    model.setCurrentIndex(model.findData("outofplane"))
    w = builder.build_recipe().window
    assert w["inputs"]["angle"] == 30.0 and w["options"]["model"] == "outofplane"


def test_the_rows_follow_the_predictor_and_come_back(builder):
    card = _windowed(builder)
    card.param_spin("meff_mT").setValue(1600.0)
    card.set_predictor("linear")
    # no FMR wording on a linear window: its own rows only
    assert set(card.param_spins) == {"a", "b"} and set(card.input_rows) == {"x"}
    assert "meff_mT" not in card.param_spins
    assert card.param_spin("a").suffix().strip() == "MHz"
    card.input_box("x").setCurrentIndex(card.input_box("x").findData("field"))
    assert card.param_spin("b").suffix().strip() == "MHz/mT"
    card.param_spin("a").setValue(6170.0); card.param_spin("b").setValue(58.56)
    w = builder.build_recipe().window
    assert w["predictor"] == "linear" and w["inputs"] == {"x": "field"}
    assert w["params"] == {"a": 6170.0, "b": 58.56}
    assert builder.build_recipe().validate(builder.registry) == []
    assert card.track_box.text() == "track a"
    card.set_predictor("kittel")                          # what was set is still there
    assert card.param_spin("meff_mT").value() == 1600.0


def test_the_expression_editor_checks_live_and_runs(builder, tmp_path):
    card = _windowed(builder)
    card.set_predictor("expression")
    card.formula_edit.setText("0.027992*sqrt((B+hk)*(B+hk+meff)")   # unbalanced
    assert "✗" in card.formula_check.text()
    card.formula_edit.setText("__import__('os')")
    assert "✗" in card.formula_check.text()
    card.formula_edit.setText("0.027992*sqrt((B+hk)*(B+hk+meff))")
    assert "✓" in card.formula_check.text()
    assert list(card.input_rows) == ["B", "hk", "meff"]
    card.input_box("B").setCurrentIndex(card.input_box("B").findData("field"))
    card.input_fixed("hk").setValue(5.0)
    card.input_fixed("meff").setValue(1750.0)
    card.option_widget("unit").setText("GHz")
    card.tracked_box.setCurrentIndex(card.tracked_box.findData("meff"))
    assert card.tracked_box.findData("B") < 0             # an input is not trackable
    w = builder.build_recipe().window
    assert w["inputs"] == {"B": "field"}
    assert w["params"] == {"hk": 5.0, "meff": 1750.0}
    assert w["options"] == {"formula": "0.027992*sqrt((B+hk)*(B+hk+meff))",
                            "unit": "GHz", "tracked": "meff"}
    r = builder.build_recipe()
    assert r.validate(builder.registry) == []
    path = tmp_path / "e.yaml"
    r.save(path)
    card.load_block(None)
    assert builder.load_recipe(Recipe.load(path)) == []
    assert builder.build_recipe().window == r.window
    builder.run_scan(block=True)
    assert abs(builder.dataset["fmr_tracked"].values[-1] - 1650.0) < 10.0
    assert "meff" in card.live.text()


def test_a_missing_input_parameter_is_flagged_on_load(builder):
    _windowed(builder)
    r = builder.build_recipe()
    r.window["inputs"]["field"] = "mag2d.field"
    missing = builder.load_recipe(r)
    assert "mag2d.field" in missing
    assert builder.build_recipe().window["inputs"]["field"] == "mag2d.field"
    assert any("field" in e for e in builder.build_recipe().validate(builder.registry))
