"""The Scan Builder's ROUTINES card and the five generic steps (offscreen).

The card must build each step from a recipe and write it back IDENTICALLY
(key for key, so a definition re-saves unchanged), check conditions as they
are typed, keep existing recipes unchanged, and -- for `pause` -- show the
operator banner and hand the answer to the scan thread.
"""

import copy
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe, build_sim_registry                       # noqa: E402

RECIPES = Path(__file__).resolve().parent.parent / "recipes"


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


def _pump_until(cond, timeout=10.0):
    from PySide6 import QtWidgets
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        QtWidgets.QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


#: Every kind, in both columns, in both spellings of the optional keys
#: (written out / left to their defaults), plus an older hook the card must
#: leave exactly as it was.
HOOKS = [
    {"when": "before_scan", "action": "call", "args": {"steps": [
        {"set": {"rf_power": -5.0}},
        {"wait_until": {"condition": "abs(rf_power + 5) < 0.1", "hold_s": 600,
                        "timeout_s": 7200, "on_timeout": "stop"}},
        {"wait_until": {"condition": "field < 1", "timeout_s": 30}},
        {"pause": {"message": "Insert the polariser, then Continue"}},
        {"pause": {"message": "check the laser", "headless": "continue"}},
        {"comment": {"text": "sample rotated 90 deg; P = {rf_power}"}},
        {"compute_set": {"set": {"rf_freq": "2800 + 28 * field"}}},
        {"action": "vna_reference"}]}},
    {"when": "before_point", "on_error": "stop", "action": "call", "args": {"steps": [
        {"abort_if": {"condition": "lockin_r > 0.9"}},
        {"skip_if": {"condition": "field > 10 and field < 20"}}]}},
    {"when": "after_point", "on_error": "continue", "action": "call", "args": {"steps": [
        {"skip_if": {"condition": "overload == True"}},
        {"comment": {"text": "point done"}}]}},
    {"when": "after_scan", "action": "call", "args": {"steps": [
        {"comment": {"text": "end at {field} mT"}},
        {"set": {"field": 0.0}}]}},
]


def _recipe(hooks):
    return Recipe(name="steps",
                  axes=[{"type": "linear", "param": "field", "start": 0.0,
                         "stop": 30.0, "num": 4}],
                  detectors=["lockin_r"], hooks=copy.deepcopy(hooks))


def test_the_step_combo_offers_the_five_kinds(builder):
    from apps.scan_builder import ADD_STEP, GenericStepRow
    for section in list(builder.routines.values()) + [builder.add_throughout()]:
        combo = section.step_combo
        assert combo.itemText(0) == ADD_STEP and combo.isEnabled()
        kinds = [combo.itemData(i) for i in range(1, combo.count())]
        assert kinds == ["wait_until", "abort_if", "skip_if", "pause", "comment",
                         "compute_set"]
        for i in range(1, combo.count()):
            combo.activated.emit(i)
            assert combo.currentIndex() == 0
        assert [s.kind for s in section.steps if isinstance(s, GenericStepRow)] == kinds
    # the action list is untouched: still exactly the registry's actions
    combo = builder.routines["before_scan"].add_combo
    assert [combo.itemData(i) for i in range(combo.count())] == [
        None, "vna_reference", "sim_autofocus", "sim_focus_at"]


def test_each_step_loads_and_writes_back_identically(builder):
    r = _recipe(HOOKS)
    assert r.validate(builder.registry) == []
    missing = builder.load_recipe(r)
    assert missing == []
    assert builder.build_recipe().hooks == HOOKS
    # and twice (load what was written)
    builder.load_recipe(builder.build_recipe())
    assert builder.build_recipe().hooks == HOOKS
    # the per-point routines became THROUGHOUT sections with the new triggers
    assert [s.to_hook()["when"] for s in builder.throughout] == ["before_point",
                                                                 "after_point"]
    assert "invalid" not in builder.summary.text()


def test_the_yaml_round_trip_through_the_card(builder, tmp_path):
    builder.load_recipe(_recipe(HOOKS))
    builder.build_recipe().save(tmp_path / "x.yaml")
    assert Recipe.load(tmp_path / "x.yaml").hooks == HOOKS


def test_an_older_per_point_routine_re_saves_unchanged(builder):
    """A before_point call hook from before 2026-10-04 has no on_error: it is
    now shown as a THROUGHOUT routine, and must be written back without one."""
    old = [{"when": "before_point", "action": "call",
            "args": {"set": {"rf_power": -3.0}}},
           {"when": "before_point", "action": "wait_ms", "args": {"ms": 0}}]
    builder.load_recipe(_recipe(old))
    assert builder.build_recipe().hooks == old


@pytest.mark.parametrize("path", sorted(RECIPES.glob("*.yaml")), ids=lambda p: p.name)
def test_existing_recipes_round_trip_unchanged(builder, path):
    r = Recipe.load(path)
    builder.load_recipe(r)
    assert builder.build_recipe().hooks == r.hooks


def test_conditions_are_checked_as_you_type(builder):
    from apps.theme import C
    section = builder.routines["before_scan"]
    builder.add_axis("field")
    row = section.add_generic("abort_if")
    assert row.err_lbl.isVisibleTo(row)                 # empty: not valid yet
    row.cond.setText("fieldd > 10")
    assert "unknown parameter 'fieldd'" in row.err_lbl.text()
    assert C["danger"] in row.cond.styleSheet()
    assert builder.summary.text() == "invalid"           # the run is refused too
    row.cond.setText("open('x')")
    assert "not allowed" in row.err_lbl.text()
    row.cond.setText("field > 10")
    assert not row.err_lbl.isVisibleTo(row) and row.cond.styleSheet() == ""
    assert builder.summary.text() != "invalid"
    assert row.to_step() == {"abort_if": {"condition": "field > 10"}}


def test_new_steps_write_their_defaults_only_where_needed(builder):
    section = builder.routines["before_scan"]
    w = section.add_generic("wait_until")
    w.cond.setText("rf_power < 0")
    w.timeout.setValue(120)
    assert w.to_step() == {"wait_until": {"condition": "rf_power < 0", "timeout_s": 120.0}}
    w.hold.setValue(5); w.on_timeout.setCurrentIndex(w.on_timeout.findData("continue"))
    assert w.to_step() == {"wait_until": {"condition": "rf_power < 0", "hold_s": 5.0,
                                          "timeout_s": 120.0, "on_timeout": "continue"}}
    c = section.add_generic("compute_set")
    c.param_box.setCurrentIndex(c.param_box.findData("rf_freq"))
    c.cond.setText("1000 + field")
    assert c.to_step() == {"compute_set": {"set": {"rf_freq": "1000 + field"}}}
    p = section.add_generic("pause")
    assert p.problems()                                  # a pause needs a message
    p.text_edit.setText("look")
    assert p.to_step() == {"pause": {"message": "look"}} and not p.problems()
    hook = section.to_hook()
    assert hook["args"]["steps"][-1] == {"pause": {"message": "look"}}
    assert "wait until rf_power < 0, hold 5 s (max 120 s)" in section.describe()


def test_a_missing_parameter_in_a_condition_is_named(builder):
    r = _recipe([{"when": "before_scan", "action": "call", "args": {"steps": [
        {"wait_until": {"condition": "ppms.temperature < 10", "timeout_s": 60}},
        {"compute_set": {"set": {"smb.frequency": "1 + field"}}}]}}])
    missing = builder.load_recipe(r)
    assert "ppms.temperature" in missing and "smb.frequency" in missing
    # the steps are still there (red), and are saved back as they were
    assert builder.build_recipe().hooks == r.hooks


def test_the_operator_banner_continue(builder):
    builder.load_recipe(Recipe(
        name="p", axes=[{"type": "linear", "param": "field", "start": 0.0, "stop": 10.0,
                         "num": 3}],
        detectors=["lockin_r"],
        hooks=[{"when": "before_scan", "action": "call", "args": {"steps": [
            {"pause": {"message": "Insert the polariser, then Continue"}}]}}]))
    builder._start_worker(builder.build_recipe())
    try:
        assert _pump_until(lambda: builder.ask_box.isVisibleTo(builder))
        assert builder.ask_lbl.text() == "Insert the polariser, then Continue"
        assert builder.abort_btn.isEnabled()
        builder.ask_continue_btn.click()
        assert _pump_until(lambda: builder.worker is None, timeout=15.0)
        assert not builder.ask_box.isVisibleTo(builder)
        assert np.isfinite(builder.dataset["lockin_r"].values).all()
    finally:
        if builder.worker is not None:
            builder.worker.abort()


def test_the_operator_banner_abort_scan(builder):
    builder.load_recipe(Recipe(
        name="p", axes=[{"type": "linear", "param": "field", "start": 0.0, "stop": 10.0,
                         "num": 3}],
        detectors=["lockin_r"],
        hooks=[{"when": "after_point", "action": "call", "args": {"steps": [
            {"pause": {"message": "rotate"}}]}}]))
    builder._start_worker(builder.build_recipe())
    assert _pump_until(lambda: builder.ask_box.isVisibleTo(builder))
    builder.ask_abort_btn.click()
    assert _pump_until(lambda: builder.worker is None, timeout=15.0)
    assert not builder.ask_box.isVisibleTo(builder)
    assert "aborted" in builder.detail.text()
    ds = builder.dataset
    assert "Abort at the pause: rotate" in ds.attrs["stopped_by"]
    v = ds["lockin_r"].values
    assert np.isfinite(v[0]) and np.isnan(v[1:]).all()


def test_the_abort_button_clears_the_banner(builder):
    builder.load_recipe(Recipe(
        name="p", axes=[{"type": "linear", "param": "field", "start": 0.0, "stop": 10.0,
                         "num": 3}],
        detectors=["lockin_r"],
        hooks=[{"when": "before_scan", "action": "call", "args": {"steps": [
            {"pause": {"message": "never answered"}}]}}]))
    builder._start_worker(builder.build_recipe())
    assert _pump_until(lambda: builder.ask_box.isVisibleTo(builder))
    builder.abort_btn.click()
    assert _pump_until(lambda: builder.worker is None, timeout=15.0)
    assert not builder.ask_box.isVisibleTo(builder)
