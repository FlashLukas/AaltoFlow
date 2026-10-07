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


def test_two_tabs_swap_cursor_units_and_splitter(app):
    """Lukas 2026-10-07: two tabs "X(t), Y(t)" and "XY / YX" (with a swap),
    the cursor readout in the plot's own axis units (it printed volts under an
    axis in mV), and a resizable left panel whose Set buttons are not clipped."""
    from PySide6 import QtCore
    from scope.apps.gui import MainWindow
    cfg = Config()
    scope, sim = build_sim_system(cfg, seed=9)
    win = MainWindow(scope, cfg)
    win.resize(1500, 900); win.show()
    try:
        assert [win.tabs.tabText(i) for i in range(win.tabs.count())] == ["X(t), Y(t)", "XY / YX"]
        _pump(app, win, lambda s: s["running_n"] >= 3)
        win.tabs.setCurrentIndex(1)
        win._force_fetch(); win._refresh(); app.processEvents()
        x, y = win.loop_curve.getData()
        assert win.hc_lines[0].angle == 90
        # the loop Y (CH2, ~0.3..0.7 V) is shown in mV: the readout must be too
        left = win.xy.getPlotItem().getAxis("left")
        for _ in range(20):
            app.processEvents(); time.sleep(0.02)
        if left.labelUnitPrefix == "m":
            assert win._axis_text(left, 0.5) == "500 mV"
        # the cursor goes to the XY readout, in view coordinates of THAT plot
        vb = win.xy.getPlotItem().vb
        mid = vb.viewRect().center()
        win._cursor_xy(vb.mapViewToScene(mid))
        assert "cursor" in win.xy_cursor.text() and "V" in win.xy_cursor.text()
        # YX: the signal horizontal, Hc lines horizontal
        win.swap_xy.setChecked(True)
        win._refresh()
        xs, ys = win.loop_curve.getData()
        # horizontal is now the intensity (~0.3..0.7 V), vertical the field (+-1 V)
        assert 0.1 < xs.min() and xs.max() < 0.9 and ys.max() - ys.min() > 1.5
        assert win.hc_lines[0].angle == 0
        # the splitter: three panes, the left one at least as wide as it needs
        assert win.body.count() == 3
        assert win.left_panel.minimumWidth() >= win.left_panel.widget().minimumSizeHint().width()
        win.body.setSizes([520, 700, 300])
        win._remember("splitter_sizes", win.body.sizes())
    finally:
        win.close()
    # remembered for the next window on this PC (the test's own settings file)
    scope2, _ = build_sim_system(Config(), seed=10)
    win2 = MainWindow(scope2, Config())
    try:
        assert win2.tabs.currentIndex() == 1 and win2.swap_xy.isChecked()
        assert win2._settings.value("splitter_sizes") is not None
    finally:
        win2.close()
