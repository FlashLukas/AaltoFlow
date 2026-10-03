"""Edited settings are not overwritten before Set / Apply (2026-10-03).

The same bug as signalhound's (Lukas, 2026-10-01: "whenever I change any
settings it comes back to the original ones"): the status poll rewrote every
box that did not have keyboard focus, while the values are sent only by the
Set / Apply buttons -- so a value typed into Center reverted as soon as the
user clicked into Span, and the button then sent the old one.
"""

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from gsp818.config import Config  # noqa: E402
from gsp818.sim_system import build_sim_system  # noqa: E402

from test_gui_smoke import _NoThread  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_frequency_edits_survive_the_poll_until_set(app):
    from gsp818.apps.gui import MainWindow
    cfg = Config()
    sa, _ = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sa), cfg)
    try:
        win._refresh()
        old_center = sa.status().center_Hz
        win.center_spin.setValue(433.92)
        win.span_spin.setValue(2.0)
        win.points_spin.setValue(201)
        for _ in range(3):
            win._refresh()                          # the status poll runs meanwhile
        assert win.center_spin.value() == pytest.approx(433.92)
        assert win.span_spin.value() == pytest.approx(2.0)
        assert win.points_spin.value() == 201
        assert win.center_spin in win._dirty and "border" in win.center_spin.styleSheet()
        assert sa.status().center_Hz == pytest.approx(old_center)   # nothing sent yet

        win._apply_center_span()                    # ALL reach the analyser
        st = sa.status()
        assert st.center_Hz == pytest.approx(433.92e6) and st.span_Hz == pytest.approx(2e6)
        assert st.points == 201
        assert not win._dirty and win.center_spin.styleSheet() == ""

        # after Set the boxes follow changes made elsewhere again (start/stop
        # follow the new centre/span at once: they were never edited)
        win._refresh()
        assert win.start_spin.value() == pytest.approx(432.92)
        sa.set_points(401)
        win._refresh()
        assert win.points_spin.value() == 401
    finally:
        win.close()


def test_bandwidth_and_tg_edits_survive_until_apply(app):
    from gsp818.apps.gui import MainWindow
    cfg = Config()
    sa, _ = build_sim_system(cfg, realtime=False, seed=3)
    win = MainWindow(_NoThread(sa), cfg)
    try:
        win._refresh()
        old_ref = sa.status().ref_level_dBm
        win.ref_spin.setValue(old_ref - 20.0)
        win.avg_spin.setValue(sa.status().averages + 2)
        win.tg_level_spin.setValue(-25.0)
        for _ in range(3):
            win._refresh()
        assert win.ref_spin.value() == pytest.approx(old_ref - 20.0)
        assert win.tg_level_spin.value() == pytest.approx(-25.0)
        assert sa.status().ref_level_dBm == pytest.approx(old_ref)

        win._apply_bandwidth()
        assert sa.status().ref_level_dBm == pytest.approx(old_ref - 20.0)
        assert sa.status().averages == win.avg_spin.value()
        # Apply sends ITS card only: the TG level is still waiting for Set level
        assert win.tg_level_spin in win._dirty and win.ref_spin not in win._dirty
        win._apply_tg_level()
        assert sa.status().tg_level_dBm == pytest.approx(-25.0) and not win._dirty

        # Enter applies its card; the force sync (after Settings) drops an unsent edit
        win.ref_spin.setValue(old_ref - 10.0)
        win.ref_spin.lineEdit().returnPressed.emit()
        assert sa.status().ref_level_dBm == pytest.approx(old_ref - 10.0) and not win._dirty
        win.ref_spin.setValue(old_ref)
        win._sync_inputs(force=True)
        assert win.ref_spin.value() == pytest.approx(old_ref - 10.0) and not win._dirty
    finally:
        win.close()
