"""GUI smoke test (§9): build offscreen, refresh, toggle, advance indicator.

Skipped automatically if PySide6 isn't installed.  Catches import/layout/signal
wiring problems without needing a display.
"""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from stage.config import Config  # noqa: E402
from stage.sim_system import build_sim_system  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_window_builds_and_refreshes(qapp):
    from stage.apps import theme
    from stage.apps.gui import MainWindow

    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()
    qapp.setStyleSheet(theme.STYLESHEET)

    win = MainWindow(brain, cfg, remote=False)
    win._refresh()

    # command a move and let the indicator animate a couple frames
    brain.set_velocity(0, 20)
    brain.move_axis(0, 3.0)
    win._refresh()
    win._indicator._tick()
    win._indicator._tick()

    # store a position and confirm the table reloads
    brain.store_position(0, "gui")
    win._reload_positions()
    assert win._table.rowCount() == len(brain.get_positions())

    # exercise an event into the log (crosses the Bridge signal)
    brain._emit("warn", "hello")

    win.close()
    brain.shutdown()


def test_indicator_paints_to_pixmap(qapp):
    """Actually paint the indicator to a pixmap -> catches paintEvent errors."""
    from PySide6.QtGui import QPixmap

    from stage.apps.gui import StageIndicator

    cfg = Config()
    ind = StageIndicator(cfg)
    ind.resize(300, 240)
    ind.set_state([5.0, 10.0, 2.0], [False, True, False], [True, False, True])
    pm = QPixmap(ind.size())
    ind.render(pm)
    assert not pm.isNull()


def _make_test_image(path, w=240, h=180):
    from PySide6.QtGui import QColor, QImage, QPainter

    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor("#334455"))
    p = QPainter(img)
    p.setPen(QColor("#ffcc66"))
    p.drawRect(10, 10, w - 20, h - 20)
    p.drawLine(0, 0, w, h)
    p.end()
    img.save(path)


def test_image_pane_load_calibrate_navigate(qapp, tmp_path):
    from stage.apps.image_pane import ImagePane

    cfg = Config()
    brain, _ = build_sim_system(cfg)
    brain.start()

    img_path = str(tmp_path / "sample.png")
    _make_test_image(img_path)

    pane = ImagePane(brain, cfg, log=lambda level, msg: None)
    pane.resize(400, 320)
    assert pane.load_image(img_path)

    # rotate (must clear any calibration) then calibrate from a known line
    pane.set_rotation(90)
    # put the stage somewhere, then WAIT for it to settle before pinning anchor
    brain.set_velocity(0, 5); brain.set_velocity(1, 5)
    brain.move_axis(0, 2.0); brain.move_axis(1, 1.0)
    import time
    t0 = time.monotonic()
    while any(brain.status().moving) and time.monotonic() - t0 < 4.0:
        time.sleep(0.02)
    ok = pane.apply_calibration((10, 10), (110, 10), 5.0)  # 100 px = 5 mm
    assert ok
    assert pane._cal.calibrated
    assert pane._cal.scale_mm_per_px == 0.05
    # anchor pinned to the (current) stage position
    assert abs(pane._cal.anchor_x - brain.status().position[0]) < 1e-6

    # clicking the anchor pixel should command a move to the anchor position
    start_x = brain.status().position[0]
    pane._on_nav_click(10, 10)   # the calibration line's start pixel
    # (move commanded; with sim it lands at ~anchor -> no exception is the check)

    # marker + limit overlays paint without error
    pane.update_marker()
    pane._refresh_overlays()
    from PySide6.QtGui import QPixmap
    pm = QPixmap(pane._canvas.size())
    pane._canvas.render(pm)
    assert not pm.isNull()

    brain.shutdown()


def test_settings_dialog_builds(qapp):
    from stage.apps.settings_dialog import SettingsDialog

    cfg = Config()
    dlg = SettingsDialog(cfg)
    # editing is in-place; just confirm it constructs with every group tab
    assert dlg is not None
