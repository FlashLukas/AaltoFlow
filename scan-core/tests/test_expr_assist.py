"""Routine expressions: pick from a list, do not type from memory (2026-10-12).

Lukas: "we could also just list the possible actions, no? to make it easier.
based on the available instrumentation". The condition / formula box of a
routine step (wait until, abort if, skip if, set from a formula) gets
  * a completer: every parameter an expression can read (settables + scalar
    detectors of the CONNECTED registry, with label and unit) + the functions;
  * an "insert..." button: the same parameters grouped by module;
  * an error that names the closest ids ("did you mean ...?").
The "+ run an action ..." combo lists only what the modules offer now,
grouped by module, and says so when there is nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import build_sim_registry, expr                          # noqa: E402
from scan_core.registry import (Action, AxisSpec, Gettable, Registry,  # noqa: E402
                                Settable)


def _lab_registry():
    """Two 'modules', the ids prefixed as the suite prefixes them."""
    reg = Registry()
    reg.add(Settable("ppms.temperature", "Temperature", "K", (1.8, 400),
                     set_fn=lambda v: None, get_fn=lambda: 10.0))
    reg.add(Settable("ppms.field", "Field", "mT", (-9000, 9000),
                     set_fn=lambda v: None, get_fn=lambda: 0.0))
    reg.add(Gettable("hf2.r1", "R 1", "V", lambda: 1e-6))
    reg.add(Gettable("vna.s", "S trace", "", lambda: [0.0], axes=[AxisSpec("f", unit="Hz")]))
    reg.add_action(Action("ppms.purge", "Purge", lambda **k: None))
    reg.add_action(Action("hf2.autorange", "Auto range", lambda **k: None))
    return reg


# ───────────────────────────── expr: the suggestions ─────────────────────────

def test_readable_params_leave_out_traces():
    ids = [p.id for p in expr.readable_params(_lab_registry())]
    assert ids == ["ppms.temperature", "ppms.field", "hf2.r1"]


def test_an_unknown_id_names_the_closest_ones():
    reg = _lab_registry()
    msg = expr.check("ppms.temprature < 10", reg)[0]
    assert "unknown parameter 'ppms.temprature'" in msg
    assert "did you mean ppms.temperature?" in msg
    # the name without its module finds it too
    assert "ppms.temperature" in expr.check("temperature < 10", reg)[0]
    # 'time' -> no close id here, but the delay step is named
    t = expr.check("time > 200", reg)[0]
    assert "unknown parameter 'time'" in t and "wait (delay)" in t
    # nothing alike: just the plain message
    assert expr.check("zzzz > 1", reg) == ["unknown parameter 'zzzz'"]


# ───────────────────────────── the builder ───────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _builder(reg):
    from apps.scan_builder import ScanBuilder
    return ScanBuilder(registry=reg)


@pytest.mark.parametrize("kind", ["wait_until", "abort_if", "skip_if", "compute_set"])
def test_every_expression_box_has_the_assist(qapp, kind):
    b = _builder(_lab_registry())
    row = b.routines["before_scan"].add_generic(kind)
    a = row.assist
    assert a is not None and a.edit is row.cond
    words = a.completions()
    assert words[:3] == ["ppms.temperature", "ppms.field", "hf2.r1"]
    assert "vna.s" not in words                         # a trace is not one value
    assert {"abs(", "min(", "max(", "round(", "and", "not"} <= set(words)
    # the popup text carries the label and the unit
    first = a.model.item(0).text()
    assert "Temperature" in first and "[K]" in first
    assert a.menu_ids() == {"ppms": ["ppms.temperature", "ppms.field"],
                            "hf2": ["hf2.r1"]}
    b.close()


def test_no_assist_where_there_is_no_expression(qapp):
    b = _builder(_lab_registry())
    for kind in ("pause", "comment", "delay"):
        assert b.routines["before_scan"].add_generic(kind).assist is None
    b.close()


def test_insert_puts_the_id_at_the_cursor(qapp):
    b = _builder(_lab_registry())
    row = b.routines["before_scan"].add_generic("abort_if")
    row.cond.setText(" > 300")
    row.cond.setCursorPosition(0)
    assert row.assist.button.menu() is row.assist.menu
    entry = row.assist.menu_action("ppms.temperature")
    assert entry in row.assist.submenus["ppms"].actions()      # under its module
    entry.trigger()
    assert row.cond.text() == "ppms.temperature > 300"
    assert not row.problems()
    # a function from the functions menu
    row.cond.setCursorPosition(0)
    row.assist.menu_action("abs(").trigger()
    assert row.cond.text().startswith("abs(ppms.temperature")
    b.close()


def test_typing_completes_the_word_at_the_cursor(qapp):
    b = _builder(_lab_registry())
    row = b.routines["before_scan"].add_generic("wait_until")
    a = row.assist
    row.cond.setText("abs(temp")
    row.cond.setCursorPosition(len("abs(temp"))
    a._on_edited(row.cond.text())
    assert a.completer.completionPrefix() == "temp"
    assert a.completer.completionCount() == 1          # matches inside the id
    a._completed(a.completer.completionModel().index(0, 0))
    assert row.cond.text() == "abs(ppms.temperature"
    assert row.cond.cursorPosition() == len("abs(ppms.temperature")
    b.close()


def test_the_assist_follows_the_registry(qapp):
    b = _builder(_lab_registry())
    sec = b.routines["before_scan"]
    row = sec.add_generic("skip_if")
    sim = build_sim_registry()
    sec.set_registry(sim)
    assert "field" in row.assist.completions()
    assert "ppms.temperature" not in row.assist.completions()
    assert list(row.assist.menu_ids()) == ["Simulator"]
    b.close()


def test_the_error_under_the_box_suggests(qapp):
    b = _builder(_lab_registry())
    row = b.routines["before_scan"].add_generic("abort_if")
    row.cond.setText("hf2.r > 1")
    assert "did you mean hf2.r1?" in row.err_lbl.text()
    b.close()


# ───────────────────────────── the action combo ──────────────────────────────

def test_actions_grouped_by_module(qapp):
    from apps.scan_builder import ADD_ACTION
    b = _builder(_lab_registry())
    combo = b.routines["before_scan"].add_combo
    texts = [combo.itemText(i) for i in range(combo.count())]
    data = [combo.itemData(i) for i in range(combo.count())]
    assert texts[0] == ADD_ACTION
    assert texts[1] == "ppms" and data[1] is None       # a heading, not pickable
    assert not combo.model().item(1).isEnabled()
    assert data == [None, None, "ppms.purge", None, "hf2.autorange"]
    assert "Purge" in texts[2]
    b.close()


def test_no_actions_says_so(qapp):
    from apps.scan_builder import NO_ACTIONS
    reg = Registry()
    reg.add(Settable("x", "x", "", (0, 1), set_fn=lambda v: None, get_fn=lambda: 0.0))
    b = _builder(reg)
    combo = b.routines["before_scan"].add_combo
    assert combo.count() == 1 and combo.itemText(0) == NO_ACTIONS
    assert not combo.isEnabled()
    b.close()
