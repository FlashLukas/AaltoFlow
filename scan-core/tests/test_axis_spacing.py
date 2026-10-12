"""The axis row's SPACING (2026-10-12): linear / log / list / file.

Until now the Scan Builder could only make linear axes; a log-spaced time
constant, a hand-picked list of fields or the options of an enum knob (a
filter wheel, a lock-in's input, made scannable in phase 1 of the de-rig
work) needed a hand-edited .yaml. Each spacing round-trips to a recipe type:
linear -> linear, log -> linear with `spacing: log`, list -> array, file ->
file. A knob that is not a number offers only "list" (a multi-select of its
options), and nothing in the builder float()s or :g-formats such a value.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtWidgets                                    # noqa: E402

from scan_core import Recipe, build_sim_registry                  # noqa: E402
from scan_core.preview import preview_axis, step_summary          # noqa: E402
from scan_core.registry import Gettable, Registry, Settable       # noqa: E402

INPUTS = ["A", "B", "A-B"]


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _registry():
    """The simulator plus an enum knob and a string knob."""
    reg = build_sim_registry()
    state = {"input": "A", "label": ""}
    reg.add(Settable("lockin.input", "Input", "", (float("-inf"), float("inf")),
                     lambda v: state.__setitem__("input", v), lambda: state["input"],
                     dtype="enum", options=INPUTS))
    reg.add(Settable("lockin.label", "Label", "", (float("-inf"), float("inf")),
                     lambda v: state.__setitem__("label", v), lambda: state["label"],
                     dtype="string"))
    return reg


def _builder(reg=None):
    from apps.scan_builder import ScanBuilder
    return ScanBuilder(registry=reg or _registry())


# ───────────────────────────── the recipe side ────────────────────────────

def test_log_spacing_in_the_recipe():
    r = Recipe(axes=[{"type": "linear", "param": "rf_freq", "start": 10, "stop": 1000,
                      "num": 3, "spacing": "log"}], detectors=["lockin_r"])
    assert r.validate(build_sim_registry()) == []
    assert np.allclose(r.compile().dims[0].coord, [10, 100, 1000])
    (d,) = preview_axis(r.axes[0], build_sim_registry())
    assert step_summary(d.members[0].sent) == "log, x10 per step"
    # a ratio cannot cross zero: refused with a reason, not a NaN sweep
    bad = Recipe(axes=[{"type": "linear", "param": "field", "start": -10, "stop": 10,
                        "num": 3, "spacing": "log"}], detectors=["lockin_r"])
    assert any("same sign" in e for e in bad.validate(build_sim_registry()))


def test_an_empty_list_is_refused():
    r = Recipe(axes=[{"type": "array", "param": "field", "values": []}],
               detectors=["lockin_r"])
    assert any("no values" in e for e in r.validate(build_sim_registry()))


# ───────────────────────────── the builder side ───────────────────────────

def test_each_spacing_round_trips(qapp, tmp_path):
    b = _builder()
    b.add_axis("field")
    row = b.rows[-1]
    assert [row.spacing_box.itemData(i) for i in range(row.spacing_box.count())] == \
        ["linear", "log", "list", "file"]
    assert row.to_axis()["type"] == "linear" and "spacing" not in row.to_axis()

    row.start.setValue(1); row.stop.setValue(100); row.num.setValue(3)
    row.set_spacing("log")
    ax = row.to_axis()
    assert ax["type"] == "linear" and ax["spacing"] == "log"
    assert b.build_recipe().validate(b.registry) == []

    row.set_spacing("list")
    row.set_list_values([5, -2.5, 40])
    ax = row.to_axis()
    assert ax == {"type": "array", "param": "field", "values": [5.0, -2.5, 40.0]}
    assert row.range_w.isHidden() and not row.list_w.isHidden()

    f = tmp_path / "fields.txt"
    f.write_text("1\n2\n3\n", encoding="utf-8")
    row.set_spacing("file")
    row.set_file_path(str(f))
    assert row.to_axis() == {"type": "file", "param": "field", "path": str(f)}
    assert b.build_recipe().validate(b.registry) == []

    # ... and every one of them comes back from a saved definition
    for ax in ({"type": "linear", "param": "field", "start": 1.0, "stop": 100.0,
                "num": 3, "spacing": "log"},
               {"type": "array", "param": "field", "values": [5.0, -2.5, 40.0]},
               {"type": "file", "param": "field", "path": str(f)}):
        b2 = _builder()
        assert b2.load_recipe(Recipe(axes=[ax], detectors=["lockin_r"])) == []
        assert b2.rows[0].raw is None              # an editable row, not a pass-through
        assert b2.rows[0].to_axis() == ax


def test_a_typed_list_keeps_what_is_not_a_number_for_validate(qapp):
    b = _builder()
    b.add_axis("field")
    row = b.rows[-1]
    row.set_spacing("list")
    row.list_edit.setText("1, 2,far")
    assert row.list_values() == [1.0, 2.0, "far"]
    assert any("takes numbers" in e for e in b.build_recipe().validate(b.registry))


def test_fly_is_offered_with_linear_spacing_only(qapp):
    b = _builder()
    b.add_axis("pos_x")
    row = b.rows[-1]
    assert row.fly.isEnabled()
    row.fly.setChecked(True)
    row.set_spacing("log")
    assert not row.fly.isChecked() and not row.fly.isEnabled()
    row.set_spacing("linear")
    assert row.fly.isEnabled()


def test_an_enum_axis_is_a_list_of_its_options(qapp):
    from apps.scan_builder import OptionPicker
    b = _builder()
    b.add_axis("lockin.input")
    row = b.rows[-1]
    # only "list", a multi-select of the options; all of them by default
    assert [row.spacing_box.itemData(i) for i in range(row.spacing_box.count())] == ["list"]
    assert isinstance(row.option_picker, OptionPicker)
    assert row.option_picker.all_box.isChecked()
    assert row.to_axis() == {"type": "array", "param": "lockin.input", "values": INPUTS}
    assert "3 options" in row.limits_lbl.text()
    assert row.fly_group.isHidden()
    # pick two: the values stay the options themselves, in their order
    row.option_picker.set_values(["A-B", "A"])
    assert row.to_axis()["values"] == ["A", "A-B"]
    assert not row.option_picker.all_box.isChecked()
    assert "2 of 3" in row.option_picker.text()
    recipe = b.build_recipe()
    assert recipe.validate(b.registry) == []
    # the summary and the preview never :g an option
    b._rebuild_summary()
    assert b.summary.text().startswith("1-D")
    dlg = b.preview_row(row)
    try:
        assert "A → A-B" in dlg.header.text() and "2 values" in dlg.header.text()
        assert dlg.table.item(1, 1).text() == "A-B"
        assert "A-B" in dlg.values_text()
    finally:
        dlg.close()
    # the round trip through a saved definition
    b2 = _builder()
    assert b2.load_recipe(recipe) == []
    assert b2.rows[0].to_axis()["values"] == ["A", "A-B"]


def test_a_string_axis_is_a_typed_list(qapp):
    b = _builder()
    b.add_axis("lockin.label")
    row = b.rows[-1]
    assert row.option_picker is None and row.list_edit is not None
    row.list_edit.setText("sample 7; sample 8")
    assert row.to_axis()["values"] == ["sample 7", "sample 8"]
    assert b.build_recipe().validate(b.registry) == []


def test_conditions_and_set_steps_offer_the_options(qapp):
    from apps.scan_builder import OptionBox, SetStepRow
    reg = _registry()
    b = _builder(reg)
    b.add_axis("field")
    row = b.add_fixed("lockin.input", "B")
    assert isinstance(row.value_box, OptionBox)
    assert [row.value_box.itemText(i) for i in range(row.value_box.count())] == INPUTS
    assert row.value() == "B"
    row.value_box.setValue("A-B")
    recipe = b.build_recipe()
    assert recipe.fixed == {"lockin.input": "A-B"}
    assert recipe.validate(reg) == []
    b._rebuild_summary()
    assert "Input = A-B" in b.detail.text()
    # a routine's set step: a combo too, and its text says the option
    step = SetStepRow(reg.get("lockin.input"), "A")
    assert isinstance(step.value_box, OptionBox) and step.text() == "Input = A"
    assert step.to_step() == {"set": {"lockin.input": "A"}}
    b.add_routine_set("before_scan", "lockin.input", "B")
    hooks = b.build_recipe().hooks
    assert hooks and hooks[0]["args"]["set"] == {"lockin.input": "B"}
    # ... and a loaded routine keeps the option (never float()ed)
    b2 = _builder(reg)
    assert b2.load_recipe(b.build_recipe()) == []
    assert b2.build_recipe().hooks[0]["args"]["set"] == {"lockin.input": "B"}
    assert b2.build_recipe().fixed == {"lockin.input": "A-B"}
    # a string knob as a condition: an editable box
    txt = b.add_fixed("lockin.label", "run 3")
    assert txt.value() == "run 3"


def test_diagonal_row_change_is_offered_with_two_stepped_axes(qapp):
    b = _builder(build_sim_registry())
    b.add_axis("pos_y")
    assert b.diagonal_box.isHidden()              # one axis: no row change
    b.add_axis("pos_x")
    assert not b.diagonal_box.isHidden()
    b.rows[-1].fly.setChecked(True)               # a flown inner axis is not stepped
    b._rebuild_summary()
    assert b.diagonal_box.isHidden()
    assert "camera" not in b.diagonal_box.toolTip().lower()
    assert "kim" not in b.diagonal_box.toolTip().lower()
    # a box ticked while there WAS a row change is not written once there
    # is none (hidden option = not in the definition)
    b.rows[-1].fly.setChecked(False)
    b._rebuild_summary()
    b.diagonal_box.setChecked(True)
    assert b.build_recipe().to_dict()["diagonal"] is True
    b.rows[-1].fly.setChecked(True)
    b._rebuild_summary()
    assert b.build_recipe().diagonal is False
    assert "diagonal" not in b.build_recipe().to_dict()


def test_a_grid_row_says_what_it_is(qapp):
    b = _builder(build_sim_registry())
    grid = {"type": "grid", "x": {"param": "pos_x", "start": 0, "stop": 10, "num": 3},
            "y": {"param": "pos_y", "start": 0, "stop": 5, "num": 2}, "fast": "x"}
    assert b.load_recipe(Recipe(axes=[grid], detectors=["lockin_r"])) == []
    row = b.rows[0]
    assert row.raw is not None and row.range_w.isHidden() and row.spacing_w.isHidden()
    assert row.raw_lbl.text().startswith("2-D grid: pos_x")
    assert row.to_axis()["type"] == "grid"
