"""A SWITCH as a condition or a routine step: an on/off box, not 0.000 / 1.000.

Lukas 2026-10-06, the DS RF generator's "RF output" in BEFORE SCAN / AFTER
SCAN: "why do i have 1 and 0 for rf generator state?". The value stays 1 / 0
in the definition (older files load unchanged); the box says on / off.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtWidgets                                    # noqa: E402

from scan_core import build_sim_registry                          # noqa: E402
from scan_core.registry import Settable                           # noqa: E402
from scan_core.storage import Storage                             # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _rf_output():
    p = Settable("dssg.rf_output", "RF output", "", (0, 1), lambda v: None, lambda: 0)
    p.storage = Storage("bool")
    return p


def test_a_switch_is_an_on_off_box_in_both_rows(qapp):
    from apps.scan_builder import BoolBox, FixedRow, SetStepRow
    for cls in (FixedRow, SetStepRow):
        row = cls(_rf_output(), 1.0)
        assert isinstance(row.value_box, BoolBox)
        assert row.value_box.isChecked() and row.value_box.text() == "on"
        assert row.value() == 1                      # the definition still holds 1 / 0
        seen = []
        row.changed.connect(lambda: seen.append(1))
        row.value_box.click()
        assert row.value() == 0 and row.value_box.text() == "off" and seen
        assert "on / off" in row.limits_lbl.text()
    assert SetStepRow(_rf_output(), 0).text() == "RF output off"


def test_a_number_stays_a_number_box(qapp):
    from apps.scan_builder import BoolBox, FixedRow
    p = build_sim_registry().get("field")
    assert not isinstance(FixedRow(p, 3.0).value_box, BoolBox)


def test_routine_with_a_switch_round_trips_through_the_builder(qapp):
    from apps.scan_builder import ScanBuilder
    reg = build_sim_registry()
    reg.add(_rf_output())
    b = ScanBuilder(registry=reg)
    b.add_axis("field")
    b.add_routine_set("before_scan", "dssg.rf_output", 1)
    b.add_routine_set("after_scan", "dssg.rf_output", 0)
    hooks = b.build_recipe().hooks
    sets = [h["args"]["set"]["dssg.rf_output"] for h in hooks if h.get("action") == "call"]
    assert sets == [1, 0]
    b.load_recipe(b.build_recipe())                # and back: still switches
    rows = b.routines["before_scan"].rows
    assert rows[0].value_box.isChecked()
    b.close()
