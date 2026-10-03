"""Edited sweep settings are not overwritten before Apply (2026-10-03).

The same bug as signalhound's (Lukas, 2026-10-01: "whenever I change any
settings it comes back to the original ones"): the status poll rewrote every
box that did not have keyboard focus, while the values are sent only by Apply
-- so a value typed into Start reverted as soon as the user clicked into Stop,
and Apply then sent the old one.
"""

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from shsna.config import Config  # noqa: E402
from shsna.sim_system import build_sim_system  # noqa: E402

from test_gui_smoke import _NoThread  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_edits_survive_the_poll_until_apply(app):
    from shsna.apps.gui import MainWindow
    cfg = Config()
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 700e6, 1300e6, 401
    sna, _ = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sna), cfg)
    try:
        win._refresh()
        win.start_spin.setValue(800.0)
        win.stop_spin.setValue(1200.0)
        win.points_spin.setValue(201)
        for _ in range(3):
            win._refresh()                          # the status poll runs meanwhile
        assert win.start_spin.value() == pytest.approx(800.0)
        assert win.stop_spin.value() == pytest.approx(1200.0)
        assert win.points_spin.value() == 201
        assert win.start_spin in win._dirty and "border" in win.start_spin.styleSheet()
        assert sna.status().start_Hz == pytest.approx(700e6)    # nothing sent yet

        win._apply_sweep()                          # ALL reach the analyser
        st = sna.status()
        assert st.start_Hz == pytest.approx(800e6) and st.stop_Hz == pytest.approx(1200e6)
        assert st.points == 201
        assert not win._dirty and win.start_spin.styleSheet() == ""

        # after Apply the boxes follow changes made elsewhere again
        sna.set_points(401)
        win._refresh()
        assert win.points_spin.value() == 401

        # Enter applies; the force sync (after Settings) drops an unsent edit
        win.avg_spin.setValue(sna.status().averages + 2)
        win.avg_spin.lineEdit().returnPressed.emit()
        assert sna.status().averages == win.avg_spin.value() and not win._dirty
        win.start_spin.setValue(900.0)
        win._sync_inputs(force=True)
        assert win.start_spin.value() == pytest.approx(800.0) and not win._dirty
    finally:
        win.close()
