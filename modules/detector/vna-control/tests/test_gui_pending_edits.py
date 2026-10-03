"""Edited sweep settings are not overwritten before Apply (2026-10-03).

The same bug as signalhound's (Lukas, 2026-10-01: "whenever I change any
settings it comes back to the original ones"): the status poll rewrote every
box that did not have keyboard focus, while the values are sent only by Apply
(or Set) -- so a value typed into Start reverted as soon as the user clicked
into Stop, and Apply then sent the old one.
"""

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from vna.config import Config  # noqa: E402
from vna.sim_system import build_sim_system  # noqa: E402

from test_gui_smoke import _NoThreadVna  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_edits_survive_the_poll_until_apply(app):
    from vna.apps.gui import MainWindow
    cfg = Config()
    cfg.field.source = "manual"
    vna, _ = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThreadVna(vna), cfg)
    try:
        win._refresh()
        old = vna.status()
        new_start, new_stop = old.start_Hz / 1e9 + 0.5, old.stop_Hz / 1e9 - 0.5
        win.start_spin.setValue(new_start)
        win.stop_spin.setValue(new_stop)
        win.power_spin.setValue(old.power_dBm - 5.0)
        for _ in range(3):
            win._refresh()                          # the status poll runs meanwhile
        assert win.start_spin.value() == pytest.approx(new_start)
        assert win.stop_spin.value() == pytest.approx(new_stop)
        assert win.power_spin.value() == pytest.approx(old.power_dBm - 5.0)
        assert win.start_spin in win._dirty and "border" in win.start_spin.styleSheet()
        assert vna.status().start_Hz == pytest.approx(old.start_Hz)   # nothing sent yet

        win._apply_sweep()                          # ALL reach the analyser
        st = vna.status()
        assert st.start_Hz == pytest.approx(new_start * 1e9)
        assert st.stop_Hz == pytest.approx(new_stop * 1e9)
        assert st.power_dBm == pytest.approx(old.power_dBm - 5.0)
        assert not win._dirty and win.start_spin.styleSheet() == ""

        # after Apply the boxes follow changes made elsewhere again
        vna.set_power(old.power_dBm)
        win._refresh()
        assert win.power_spin.value() == pytest.approx(old.power_dBm)
    finally:
        win.close()


def test_manual_field_edits_survive_until_set(app):
    from vna.apps.gui import MainWindow
    cfg = Config()
    cfg.field.source = "manual"
    vna, _ = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThreadVna(vna), cfg)
    try:
        win._refresh()
        win.manual_spin.setValue(42.0)
        win.manual_angle_spin.setValue(30.0)
        win._refresh()
        assert win.manual_spin.value() == pytest.approx(42.0)
        win._apply_manual_field()
        st = vna.status()
        assert st.manual_field_mT == pytest.approx(42.0)
        assert st.manual_angle_deg == pytest.approx(30.0)
        assert not win._dirty

        # Enter applies its group; the force sync drops an unsent edit
        win.avg_spin.setValue(vna.status().averages + 1)
        win.avg_spin.lineEdit().returnPressed.emit()
        assert vna.status().averages == win.avg_spin.value() and not win._dirty
        win.manual_spin.setValue(10.0)
        win._sync_inputs(force=True)
        assert win.manual_spin.value() == pytest.approx(42.0) and not win._dirty
    finally:
        win.close()
