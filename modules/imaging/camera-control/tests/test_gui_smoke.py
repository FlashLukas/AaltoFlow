"""Offscreen GUI smoke test -- builds the window, refreshes, paints overlays.

Catches import/layout/signal-wiring breakage without a display.  Skipped if
PySide6 is not installed.
"""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time  # noqa: E402

from camera.config import Config              # noqa: E402
from camera.sim_system import build_sim_system  # noqa: E402


def test_gui_builds_and_refreshes(tmp_path):
    from PySide6.QtWidgets import QApplication
    from camera.apps.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    time.sleep(0.1)
    # give the window something to draw
    tcx, tcy = cam.template_center_px()
    brain.capture_reference((tcx, tcy, 60, 60))
    brain.set_tracking(True)
    brain.set_selected_index(2, 2)
    time.sleep(0.05)

    win = MainWindow(brain, cfg, remote=False)
    win.resize(1000, 700)
    win._refresh()                      # pull a frame + status into the widgets
    app.processEvents()

    # render to a pixmap -> exercises CameraView.paintEvent + overlays
    pm = win.grab()
    out = tmp_path / "gui.png"
    assert pm.save(str(out))
    assert out.stat().st_size > 0

    win.close()
    brain.shutdown()


def test_scan_area_draw_then_apply_settings_keeps_angle_and_pitch():
    """Lukas, 2026-09-14: draw a rotated scan area, press Apply settings under
    Scanning -> the angle went back to 0 (the form still held the old values)."""
    from PySide6.QtWidgets import QApplication
    from camera.apps.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    cfg = Config()
    brain, *_ = build_sim_system(cfg)
    win = MainWindow(brain, cfg, remote=False)
    try:
        group = [("Scanning", cfg.scanning)]
        win._on_scan_area(320.0, 240.0, 100.0, 60.0, 30.0)       # draw: rotated 30 deg
        dx, dy = brain.cfg.scanning.dx_um, brain.cfg.scanning.dy_um
        form = win._form_widgets["scanning"]
        assert form["angle_deg"].value() == pytest.approx(30.0)
        assert form["dx_um"].value() == pytest.approx(dx, abs=1e-4)

        win._apply_settings(group)                                 # the bug: angle -> 0
        assert brain.cfg.scanning.angle_deg == pytest.approx(30.0)
        assert brain.cfg.scanning.dx_um == pytest.approx(dx, abs=1e-4)

        # Apply size -> new dx/dy shown in the form (and kept by Apply settings)
        win.sp_sizex.setValue(10.0); win.sp_sizey.setValue(4.0)
        win._apply_scan_size()
        n = brain.cfg.scanning.points_x - 1
        assert form["dx_um"].value() == pytest.approx(10.0 / n, abs=1e-4)
        win._apply_settings(group)
        assert brain.cfg.scanning.dx_um == pytest.approx(10.0 / n, abs=1e-4)

        # Apply settings with a typed pitch -> the size fields follow
        form["dx_um"].setValue(2.0)
        win._apply_settings(group)
        assert win.sp_sizex.value() == pytest.approx(2.0 * n)
        assert brain.cfg.scanning.angle_deg == pytest.approx(30.0)

        # Change only the number of points -> the size stays, the pitch follows
        size_x = win.sp_sizex.value()
        form["points_x"].setValue(n + 1 + 10)
        win._apply_settings(group)
        assert brain.cfg.scanning.points_x == n + 11
        assert brain.cfg.scanning.dx_um == pytest.approx(size_x / (n + 10), abs=1e-6)
        assert win.sp_sizex.value() == pytest.approx(size_x, abs=1e-2)
        assert form["dx_um"].value() == pytest.approx(size_x / (n + 10), abs=1e-4)
    finally:
        win.close()
        app.processEvents()


def test_window_fits_a_1080p_screen():
    """The lab screen has ~1081 px usable height; a taller minimum made Qt spam
    'Unable to set geometry' warnings. Tabs scroll instead."""
    from PySide6.QtWidgets import QApplication
    from camera.apps.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    brain, *_ = build_sim_system(Config())
    win = MainWindow(brain, brain.cfg, remote=False)
    h = max(win.minimumSizeHint().height(), win.minimumSize().height())
    assert h < 900, h
    win.close()
    app.processEvents()


def test_spot_tab_thresholds_a_snapshot_and_calibrates():
    """Grab -> threshold (applied to the brain) -> Calibrate spot -> the brain's
    spot position is the calibrated one."""
    from PySide6.QtWidgets import QApplication
    from camera.apps.gui import MainWindow
    from camera.apps.spot_tab import analyse, suggest_threshold

    app = QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    try:
        time.sleep(0.15)
        win = MainWindow(brain, cfg, remote=False)
        tab = win.spot_tab
        win.tabs.setCurrentWidget(win.spot_page)
        assert win.tabs.currentWidget() is win.spot_page
        tab.grab_frame()
        assert tab._gray is not None
        assert "spot found" in tab.lab_det.text()

        # a threshold that selects everything -> background warning, many pixels
        tab.sp_lo.setValue(0)
        assert "background" in tab.lab_warn.text()
        # (a LOCAL window shares the brain's cfg object, so the edit is already
        # live; a remote window relies on this push -- see test_net)
        tab._push_threshold()
        assert brain.cfg.spot.thr_lower == 0

        # the suggestion separates the spot from the background again
        thr, _why = suggest_threshold(tab._gray, True)
        assert thr > 0
        tab._suggest()
        assert tab.sp_lo.value() == thr
        a = analyse(tab._gray, cfg.spot)
        assert a["spot"].found and a["blobs"] == 1

        tab._calibrate()
        assert brain.cfg.spot.ref_set and cfg.spot.ref_set
        assert abs(brain.cfg.spot.ref_x - 320) < 2
        assert "calibrated" in tab.lab_ref.text()
        win._refresh()                                   # spot tab visible: numbers only
        assert "seen" in tab.lab_live.text() or "NOT seen" in tab.lab_live.text()
        assert abs(tab.sp_x.value() - brain.cfg.spot.ref_x) < 0.01   # boxes show the result

        # enter it by hand: a click only fills the boxes while picking...
        tab.view.clicked.emit(200.0, 150.0)
        assert abs(tab.sp_x.value() - 200.0) > 1                     # not picking -> ignored
        tab.chk_pick.setChecked(True)
        tab.view.clicked.emit(200.0, 150.0)
        assert (tab.sp_x.value(), tab.sp_y.value()) == (200.0, 150.0)
        assert abs(brain.cfg.spot.ref_x - 200.0) > 1                 # ...nothing set yet
        tab._set_manual()
        assert brain.spot_position() == (200.0, 150.0)
        assert "set by hand" in tab.lab_ref.text() and not tab.chk_pick.isChecked()
        tab._clear_position()
        assert brain.spot_position() is None and "not calibrated" in tab.lab_ref.text()
        win.close()
    finally:
        brain.shutdown()
        app.processEvents()


def test_spot_area_trace():
    """One sample per NEW frame, 0 when the spot is not seen, a 30 s window,
    pause and clear -- driven by synthetic status frames."""
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication
    from camera.apps.spot_tab import AREA_WINDOW_S, SpotTab

    app = QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.spot.ref_set, cfg.spot.ref_area = True, 20.0
    tab = SpotTab(ctrl=None, cfg=cfg, log=lambda *a: None, frame_source=lambda: None)

    def st(fn, found, area):
        return SimpleNamespace(frame_number=fn, spot_found=found, spot_area=area,
                               spot_live_x=0.0, spot_live_y=0.0)

    t0 = 1000.0
    tab.record(st(1, True, 20.0), now=t0)
    tab.record(st(1, True, 99.0), now=t0 + 0.05)     # same frame polled again -> ignored
    tab.record(st(2, False, 0.0), now=t0 + 0.1)      # not seen -> 0
    tab.record(st(3, True, 22.0), now=t0 + 0.2)
    assert [a for _t, a in tab._area] == [20.0, 0.0, 22.0]

    tab._draw_area(now=t0 + 0.3)
    xs, ys, _c, _l = tab.area_plot._series[-1]                     # data drawn last (on top)
    assert ys == [20.0, 0.0, 22.0] and xs[-1] == pytest.approx(-0.1)
    assert tab.area_plot._series[0][1] == [20.0, 20.0]            # calibrated reference line
    assert "seen in 67 %" in tab.lab_area.text()

    tab._draw_area(now=t0 + AREA_WINDOW_S + 5)                     # everything aged out
    assert "no samples" in tab.lab_area.text()

    tab.chk_area_pause.setChecked(True)
    tab.record(st(4, True, 30.0), now=t0 + 1)
    assert len(tab._area) == 3                                     # paused
    tab.chk_area_pause.setChecked(False)
    tab._clear_area()
    assert len(tab._area) == 0
    tab.close()
    app.processEvents()


def test_z_target_survives_status_refresh():
    """Typing a Z target and pressing Set must send THAT value. The refresh
    timer used to rewrite the box from live status whenever it lacked focus --
    and the Set button takes focus -- so Set sent the current Z instead."""
    from PySide6.QtWidgets import QApplication
    from camera.apps.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    try:
        time.sleep(0.1)
        win = MainWindow(brain, cfg, remote=False)
        win._refresh()                          # first refresh: target <- live Z
        start = brain.status().z_voltage
        assert abs(win.z_spin.value() - start) < 0.01

        win.z_spin.setValue(start + 5.0)        # the user types a new target
        win.z_spin.clearFocus()                 # ...and clicks Set
        for _ in range(5):
            win._refresh()                      # refresh ticks before the click lands
        assert abs(win.z_spin.value() - (start + 5.0)) < 0.01

        sent = []
        brain_set_z = brain.set_z
        brain.set_z = lambda v: sent.append(v) or brain_set_z(v)
        for b in win.findChildren(type(win.b_af)):   # the Set button next to the box
            if b.text() == "Set" and b.parent() is win.z_spin.parent():
                b.click()
                break
        assert sent and abs(sent[0] - (start + 5.0)) < 0.01

        # focus step buttons: two ups and a down by the step
        win.z_step.setValue(0.5)
        win.b_z_up.click(); win.b_z_up.click(); win.b_z_dn.click()
        assert brain._z_target == pytest.approx(start + 5.0 + 0.5)
        win.close()
    finally:
        brain.shutdown()
        app.processEvents()


def test_real_ids_feature_list_builds_every_widget():
    """The lab camera's real GenICam list (U3-386xCP-M, 237 features, dumped
    2026-09-13) includes uint32 and int64 ranges that a QSpinBox cannot hold;
    the first one crashed the GUI at startup with OverflowError."""
    import json
    from pathlib import Path

    from PySide6.QtWidgets import QApplication, QLabel
    from camera.apps.gui import MainWindow

    feats = json.loads((Path(__file__).parent / "data" / "ids_U3-386xCP-M_features.json")
                       .read_text(encoding="utf-8"))
    assert any(f["type"] == "int" and f["max"] > 2**31 - 1 for f in feats)

    app = QApplication.instance() or QApplication([])
    brain, *_ = build_sim_system(Config())
    brain.camera_features = lambda: feats        # the sim brain, the real camera's list
    win = MainWindow(brain, brain.cfg, remote=False)
    form = win._cam_param_form
    not_shown = [form.itemAt(i).widget().text() for i in range(form.count())
                 if isinstance(form.itemAt(i).widget(), QLabel)
                 and form.itemAt(i).widget().text().startswith("(not shown")]
    assert not_shown == []
    assert form.rowCount() == len(feats)
    win.close()
    app.processEvents()


def test_xy_subtab_shows_position_and_jogs():
    """Control XY stage: live position, jog pad, datum only where the stage has one,
    and the limits envelope rows only where the camera's own limits apply."""
    from PySide6.QtWidgets import QApplication
    from camera.apps.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    try:
        brain.move_xy(50.0, 60.0)
        time.sleep(0.1)
        win = MainWindow(brain, cfg, remote=False)
        win._refresh()
        assert win.lab_stage_x.text().startswith("X  50.00")
        assert not win.b_datum.isEnabled()              # the sim piezo has no datum
        assert not win.lab_limits_note.isVisibleTo(win) # cfg limits are in use
        win.xy_step.setValue(2.0)
        win.b_x_up.click(); win.b_y_dn.click()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3 and xy.read_xy() != pytest.approx((52.0, 58.0)):
            time.sleep(0.02)
        assert xy.read_xy() == pytest.approx((52.0, 58.0))
        t0 = time.monotonic()               # the status snapshot follows a frame later
        while time.monotonic() - t0 < 3 and abs(brain.status().stage_y - 58.0) > 1e-3:
            time.sleep(0.02)
        win._refresh()
        assert win.lab_stage_y.text().startswith("Y  58.00")
        win.close()
    finally:
        brain.shutdown()
