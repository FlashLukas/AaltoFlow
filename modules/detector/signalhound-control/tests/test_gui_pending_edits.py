"""Edited sweep settings are not overwritten before Apply (2026-10-01).

Lukas: "on the spectrum analyser whenever I change any settings it comes back
to the original ones". The status poll rewrote every box that did not have
keyboard focus, so a value typed into Centre reverted as soon as the user
clicked into Span, and Apply then sent the old value.
"""

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from signalhound.config import Config  # noqa: E402
from signalhound.sim_system import build_sim_system  # noqa: E402

from test_gui_smoke import _NoThread  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_edits_survive_the_poll_until_apply(app):
    from signalhound.apps.gui import MainWindow
    cfg = Config()
    sa, _sim = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sa), cfg)
    try:
        win._refresh()
        old_centre = sa.status().center_Hz
        # the user types a new centre and a new span; neither box keeps focus
        win.center_spin.setValue(old_centre / 1e9 + 0.1)
        win.span_spin.setValue(5.0)
        for _ in range(3):
            win._refresh()                         # the status poll runs meanwhile
        assert win.center_spin.value() == pytest.approx(old_centre / 1e9 + 0.1)
        assert win.span_spin.value() == pytest.approx(5.0)
        assert win.center_spin in win._dirty and "border" in win.center_spin.styleSheet()
        assert sa.status().center_Hz == pytest.approx(old_centre)   # nothing sent yet

        win._apply_sweep()                         # BOTH reach the analyser
        assert sa.status().center_Hz == pytest.approx(old_centre + 0.1e9)
        assert sa.status().span_Hz == pytest.approx(5e6)
        assert not win._dirty and win.center_spin.styleSheet() == ""

        # after Apply the boxes follow changes made elsewhere again
        sa.set_center(old_centre)
        win._refresh()
        assert win.center_spin.value() == pytest.approx(old_centre / 1e9)
    finally:
        win.close()


def test_start_and_stop_set_the_band(app):
    """Lukas: set START and STOP instead of centre/span."""
    from signalhound.apps.gui import MainWindow
    cfg = Config()
    sa, _sim = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sa), cfg)
    try:
        win._refresh()
        win.start_spin.setValue(1.0)
        win.stop_spin.setValue(1.2)
        win._refresh()                             # the poll does not undo them
        assert win.start_spin.value() == pytest.approx(1.0)
        win._apply_sweep()
        st = sa.status()
        assert st.start_Hz == pytest.approx(1.0e9) and st.stop_Hz == pytest.approx(1.2e9)
        win._refresh()                             # centre / span follow the new band
        assert win.center_spin.value() == pytest.approx(1.1)
        assert win.span_spin.value() == pytest.approx(200.0)
        # a reversed band is refused, nothing sent
        win.start_spin.setValue(1.5)
        win.stop_spin.setValue(1.4)
        win._apply_sweep()
        assert sa.status().start_Hz == pytest.approx(1.0e9)
    finally:
        win.close()


def test_points_choose_the_rbw_and_show_the_real_bins(app):
    """Lukas's choice: N points = the RBW that gives about N bins; the box then
    shows the bin count the analyser really made."""
    from signalhound.apps.gui import MainWindow
    cfg = Config()
    cfg.sweep.span_Hz = 20e6
    cfg.acquisition.sweep_on_start = True
    sa, _sim = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sa), cfg)
    try:
        sa.step()
        win._refresh()
        before = sa.status().points
        assert before >= 2 and win.points_spin.value() == before
        win.points_spin.setValue(before * 4)       # four times finer
        win._apply_sweep()
        sa.step()
        win._refresh()
        after = sa.status().points
        assert after > before * 2                  # the RBW really went down
        assert sa.status().vbw_Hz <= sa.status().rbw_Hz
        assert win.points_spin.value() == after    # shows what the analyser made
    finally:
        win.close()


def test_enter_in_a_box_applies(app):
    from signalhound.apps.gui import MainWindow
    cfg = Config()
    sa, _sim = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sa), cfg)
    try:
        win._refresh()
        win.ref_spin.setValue(-30.0)
        win.ref_spin.lineEdit().returnPressed.emit()
        assert sa.status().ref_level_dBm == pytest.approx(-30.0)
        assert not win._dirty
    finally:
        win.close()
