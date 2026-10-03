"""Edited settings are not overwritten before Apply (2026-10-03).

The same bug as signalhound's (Lukas, 2026-10-01: "whenever I change any
settings it comes back to the original ones"): the status poll rewrote every
box that did not have keyboard focus, while the values are sent only by Apply
(or Set) -- so a value typed into Integration reverted as soon as the user
clicked into Averages, and Apply then sent the old one.
"""

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from ccs200.config import Config  # noqa: E402
from ccs200.sim_system import build_sim_system  # noqa: E402

from test_gui_smoke import _NoThread  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_scan_edits_survive_the_poll_until_apply(app):
    from ccs200.apps.gui import MainWindow
    cfg = Config()
    spec, _ = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(spec), cfg)
    try:
        win._refresh()
        old_t, old_avg = spec.status().integration_time_s, spec.status().averages
        new_t_ms, new_avg = old_t * 1e3 * 2 + 1.0, old_avg + 3
        # the user types both; neither box keeps focus (offscreen: none has it)
        win.int_spin.setValue(new_t_ms)
        win.avg_spin.setValue(new_avg)
        for _ in range(3):
            win._refresh()                          # the status poll runs meanwhile
        assert win.int_spin.value() == pytest.approx(new_t_ms)
        assert win.avg_spin.value() == new_avg
        assert win.int_spin in win._dirty and "border" in win.int_spin.styleSheet()
        assert spec.status().integration_time_s == pytest.approx(old_t)   # nothing sent yet

        win._apply_scan()                           # BOTH reach the spectrometer
        assert spec.status().integration_time_s == pytest.approx(new_t_ms / 1e3)
        assert spec.status().averages == new_avg
        assert not win._dirty and win.int_spin.styleSheet() == ""

        # after Apply the boxes follow changes made elsewhere again
        spec.set_averages(old_avg)
        win._refresh()
        assert win.avg_spin.value() == old_avg
    finally:
        win.close()


def test_window_edits_survive_the_poll_until_set(app):
    from ccs200.apps.gui import MainWindow
    cfg = Config()
    spec, _ = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(spec), cfg)
    try:
        win._refresh()
        win.wmin_spin.setValue(500.0)
        win.wmax_spin.setValue(600.0)
        win._refresh()
        assert win.wmin_spin.value() == pytest.approx(500.0)
        win._apply_window()
        st = spec.status()
        assert st.window_min_nm == pytest.approx(500.0) and st.window_max_nm == pytest.approx(600.0)
        assert not win._dirty

        # Enter in a box sends its group; the Settings dialog's force sync
        # puts an unsent edit back to the instrument's value
        win.avg_spin.setValue(spec.status().averages + 1)
        win.avg_spin.lineEdit().returnPressed.emit()
        assert spec.status().averages == win.avg_spin.value() and not win._dirty
        win.wmin_spin.setValue(450.0)
        win._sync_inputs(force=True)
        assert win.wmin_spin.value() == pytest.approx(500.0) and not win._dirty
    finally:
        win.close()
