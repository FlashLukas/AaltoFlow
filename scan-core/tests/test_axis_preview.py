"""Double-click an axis row -> the setpoints it will actually send.

From the rig, 2026-09-24: "i would like to be able to double click on the axis
and preview the actual scanning points". The preview must show what the
INSTRUMENT receives, not what was typed: the engine's compile step, then the
Settable clamp, then the int rounding manifest.py applies.
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

from scan_core import build_sim_registry                        # noqa: E402
from scan_core.preview import preview_axis, step_summary         # noqa: E402
from scan_core.registry import Registry, Settable                 # noqa: E402


def _reg(integer=False, limits=(-10, 10)):
    reg = Registry()
    p = reg.add(Settable("p", "Knob", "mT", limits,
                         set_fn=lambda v: None, get_fn=lambda: 0))
    p.integer = integer
    return reg


# ─────────────────────────────── the values ───────────────────────────────────

def test_a_linear_axis_previews_its_linspace():
    (d,) = preview_axis({"type": "linear", "param": "p",
                         "start": -3, "stop": 3, "num": 5}, _reg())
    m = d.members[0]
    assert np.allclose(m.sent, [-3, -1.5, 0, 1.5, 3])
    assert m.n_changed == 0 and m.n_repeats == 0
    assert (m.label, m.unit) == ("Knob", "mT")
    assert step_summary(m.sent) == "step 1.5"


def test_points_outside_the_limits_are_shown_clamped():
    (d,) = preview_axis({"type": "linear", "param": "p",
                         "start": 0, "stop": 20, "num": 3}, _reg())
    m = d.members[0]
    assert list(m.sent) == [0, 10, 10]
    assert "clamped from 20" in m.notes[2]
    assert m.n_changed == 1 and m.n_repeats == 1


def test_an_int_axis_is_shown_rounded_and_repeats_are_counted():
    (d,) = preview_axis({"type": "linear", "param": "p",
                         "start": 0, "stop": 2, "num": 5}, _reg(integer=True))
    m = d.members[0]
    assert list(m.sent) == [0, 0, 1, 2, 2]      # 0.5 -> 0 and 1.5 -> 2 (banker's), like round()
    assert m.n_changed == 2 and m.n_repeats == 2


def test_a_raster_previews_both_of_its_dims():
    reg = build_sim_registry()
    dims = preview_axis({"type": "raster",
                         "x": {"param": "x", "start": 0, "stop": 1, "num": 3},
                         "y": {"param": "y", "start": 0, "stop": 2, "num": 2}}, reg)
    assert [d.size for d in dims] == [2, 3]      # fast x is the inner dim
    assert step_summary(np.array([0, 1, 3])) == "step 1 .. 2 (uneven)"


# ─────────────────────────────── the window ───────────────────────────────────

def test_double_click_opens_a_preview_that_follows_the_row():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtTest import QTest
    from apps.scan_builder import AxisPreviewDialog, ScanBuilder

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ScanBuilder(build_sim_registry())
    try:
        win.add_axis("field")
        row = win.rows[0]
        row.start.setValue(-3); row.stop.setValue(3); row.num.setValue(5)

        QTest.mouseDClick(row, QtCore.Qt.LeftButton, pos=QtCore.QPoint(4, 4))
        dlg = win._previews.get(row)
        assert isinstance(dlg, AxisPreviewDialog)
        assert dlg.table.rowCount() == 5
        assert dlg.table.item(4, 1).text() == "3"

        row.num.setValue(7)                      # edit the row: the list follows
        assert dlg.table.rowCount() == 7
        assert dlg.values_text().splitlines()[0] == "#\tfield"

        assert win.preview_row(row) is dlg       # one window per row
        win._remove_row(row)
        assert row not in win._previews
    finally:
        win.close(); win.deleteLater()
