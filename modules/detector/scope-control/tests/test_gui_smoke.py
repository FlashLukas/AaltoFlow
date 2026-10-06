"""GUI smoke test (skipped without the gui extra): the window builds against
the simulated bench offscreen, refreshes, draws, and its controls send."""

import os
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from scope.config import Config
from scope.sim_system import build_sim_system


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(app, win, pred=None, timeout=5.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        app.processEvents()
        win._refresh()
        if pred is None or pred(win.ctrl.status()):
            return
        time.sleep(0.02)
    raise AssertionError(f"not reached: {win.ctrl.status()}")


def test_window_builds_draws_and_sends(app):
    from scope.apps.gui import MainWindow
    cfg = Config()
    scope, sim = build_sim_system(cfg, seed=8)
    win = MainWindow(scope, cfg)
    try:
        _pump(app, win, lambda s: s["running_n"] >= 3)
        win._force_fetch(); win._refresh()
        x, y = win.loop_curve.getData()
        assert x is not None and len(x) == cfg.acquisition.points       # the loop is drawn
        assert sim.writes == []                                         # start only read
        assert win.inp["ch2"]["vdiv"].value() == pytest.approx(0.1)     # boxes = the scope
        # a scope setting: edit + Set
        win.tdiv.setValue(2.0)
        assert win.tdiv in win._dirty                                   # outlined until sent
        win.tdiv.lineEdit().returnPressed.emit()
        _pump(app, win, lambda s: s["tdiv_s_set"] == pytest.approx(2e-3) and s["settings_settled"])
        assert win.tdiv not in win._dirty
        # module settings and the quantity
        win.inp["ch1"]["unit"].setText("mT"); win.inp["ch1"]["scale"].setValue(50.0)
        win._send_quantity("ch1")
        _pump(app, win, lambda s: s["ch1_unit"] == "mT")
        # acquire through the button; the table shows the latched numbers
        win.acq_btn.click()
        _pump(app, win, lambda s: s["acq_id"] == 1 and not s["acquiring"], timeout=10)
        win.src.setCurrentIndex(1); win._refresh()
        assert win.table.item(len(win._value_rows) + 1, 1).text().endswith("mT")
        assert not win.gen_card.isEnabled()                              # no generator
        assert "," not in win.inp["ch1"]["scale"].text()                # C locale
    finally:
        win.close()


def test_settings_dialog_applies_without_touching_the_scope(app):
    from scope.apps.settings_dialog import SettingsDialog
    cfg = Config()
    scope, sim = build_sim_system(cfg)
    scope.start()
    try:
        SettingsDialog(scope, cfg, lambda: None)._apply_and_close()
        time.sleep(0.2)
        assert sim.writes == []
    finally:
        scope.shutdown()
