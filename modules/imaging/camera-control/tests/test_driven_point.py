"""The window shows which point an external client is moving to (ROADMAP 2026-10-02).

When scan-core (or any other machine client) picks the camera's scan point,
the Stabiliser's Index X / Y boxes follow the service -- unless the user is
editing them (focus, or changed and not sent with Select yet) -- a line under
them says who drives it and where ("scan 'map' (scan-core): point (3, 5) of
10 x 10, moving"), and the image marks the target point while the stage
moves, plus the points visited so far. View only: it works in a viewer window.

The refresh is driven BY HAND with made-up status frames and a made-up clock,
so nothing depends on timing.
"""

import dataclasses
import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from camera.apps import gui as G  # noqa: E402
from camera.apps.gui import MainWindow  # noqa: E402
from camera.config import Config  # noqa: E402
from camera.sim_system import build_sim_system  # noqa: E402


def _app():
    return QApplication.instance() or QApplication([])


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def win():
    app = _app()
    cfg = Config()
    cfg.scanning.points_x, cfg.scanning.points_y = 10, 10
    brain, *_ = build_sim_system(cfg)
    brain.start()
    w = MainWindow(brain, brain.cfg, remote=False)
    w.resize(1600, 1000)
    w.show()
    app.processEvents()
    w._timer.stop()                         # the test drives the refresh
    w._clock = Clock()
    w._control_info = lambda: None          # a local brain: no control status
    w.view.set_frame(np.zeros((600, 800), np.uint8))
    yield w, brain, app
    w.close()
    brain.shutdown()
    app.processEvents()


def _st(brain, ix, iy, **kw):
    base = dict(selected_index_x=ix, selected_index_y=iy, stabilize_on=True,
                point_settled=False, stable=False)
    base.update(kw)
    return dataclasses.replace(brain.status(), **base)


def test_the_boxes_follow_a_point_chosen_elsewhere(win):
    w, brain, _app_ = win
    w._refresh_index(_st(brain, 0, 0))
    w._refresh_index(_st(brain, 3, 5))      # a machine client chose (3, 5)
    assert (w.sp_ix.value(), w.sp_iy.value()) == (3, 5)
    w._refresh_index(_st(brain, 4, 5))
    assert (w.sp_ix.value(), w.sp_iy.value()) == (4, 5)


def test_a_box_being_edited_is_not_overwritten(win):
    w, brain, app = win
    w._refresh_index(_st(brain, 0, 0))
    w.sp_ix.setValue(7)                     # typed, not sent yet (dirty)
    w._refresh_index(_st(brain, 3, 5))
    assert w.sp_ix.value() == 7             # the user's edit is kept...
    assert w.sp_iy.value() == 5             # ...the other box still follows
    assert w.sp_ix in w._idx_dirty
    # Select sends it; from then on the box follows the service again
    w._b_idx.click()
    assert w.sp_ix not in w._idx_dirty
    assert brain.cfg.scanning.selected_index_x == 7
    w._refresh_index(_st(brain, 2, 2))
    assert (w.sp_ix.value(), w.sp_iy.value()) == (2, 2)


def test_a_focused_box_is_not_overwritten(win):
    w, brain, app = win
    w._refresh_index(_st(brain, 0, 0))
    w.sp_iy.setFocus()
    app.processEvents()
    if not w.sp_iy.hasFocus():
        pytest.skip("offscreen platform refused focus")
    w._refresh_index(_st(brain, 3, 5))
    assert w.sp_iy.value() == 0 and w.sp_ix.value() == 3


def test_the_poll_itself_does_not_mark_a_box_dirty(win):
    w, brain, _app_ = win
    w._refresh_index(_st(brain, 0, 0))
    w._refresh_index(_st(brain, 3, 5))
    assert not w._idx_dirty


def test_a_note_says_who_drives_and_where(win):
    w, brain, _app_ = win
    w._refresh_index(_st(brain, 0, 0))
    assert w.lab_drive.isHidden()           # nothing drives it yet
    w._refresh_index(_st(brain, 3, 5))
    assert not w.lab_drive.isHidden()
    assert w.lab_drive.text() == "another client: point (3, 5) of 10 x 10, moving"
    w._refresh_index(_st(brain, 3, 5, point_settled=True, stable=True))
    assert w.lab_drive.text() == "another client: point (3, 5) of 10 x 10, stable"
    w._refresh_index(_st(brain, 3, 5, stabilize_on=False))
    assert w.lab_drive.text().endswith("stabiliser off")
    # quiet for longer than the driving window: the note goes
    w._clock.t += G.DRIVE_NOTE_S + 1
    w._refresh_index(_st(brain, 3, 5))
    assert w.lab_drive.isHidden()


def test_the_note_names_the_scan_from_the_control_status(win):
    w, brain, _app_ = win
    scan = {"id": "s1", "name": "scan-core", "label": "map", "kind": "machine"}
    ctl = {"holder": None, "clients": [], "scan": scan}
    w._control_info = lambda: ctl
    w._refresh_index(_st(brain, 0, 0))
    w._refresh_index(_st(brain, 1, 0))
    assert w.lab_drive.text() == "scan-core ('map'): point (1, 0) of 10 x 10, moving"
    # a slow scan point: the note stays while that scan holds the camera
    w._clock.t += G.DRIVE_NOTE_S * 5
    w._refresh_index(_st(brain, 1, 0))
    assert not w.lab_drive.isHidden()
    ctl["scan"] = None                      # the scan ended
    w._refresh_index(_st(brain, 1, 0))
    assert w.lab_drive.isHidden()


def test_the_note_names_a_machine_also_driving(win):
    w, brain, _app_ = win
    ctl = {"holder": None, "scan": None,
           "clients": [{"id": "m1", "kind": "machine", "name": "my script",
                        "driving": True}]}
    w._control_info = lambda: ctl
    w._refresh_index(_st(brain, 0, 0))
    w._refresh_index(_st(brain, 2, 2))
    assert w.lab_drive.text().startswith("my script: point (2, 2)")


def test_a_point_chosen_here_is_not_called_external(win):
    w, brain, _app_ = win
    w._refresh_index(_st(brain, 0, 0))
    w.sp_ix.setValue(4); w.sp_iy.setValue(6)
    w._b_idx.click()                        # Select, in this window
    w._refresh_index(_st(brain, 4, 6))
    assert w.lab_drive.isHidden()
    assert w.view.drive_marks() == (None, [])


def test_the_image_marks_the_target_while_moving_and_the_visited_points(win):
    w, brain, _app_ = win
    w._refresh_index(_st(brain, 0, 0))
    w._refresh_index(_st(brain, 1, 0))
    assert w.view.drive_marks() == ((1, 0), [])
    w._refresh_index(_st(brain, 2, 0))
    assert w.view.drive_marks() == ((2, 0), [(1, 0)])
    # settled: no target mark (the stable ring says it), the visited stay
    w._refresh_index(_st(brain, 2, 0, point_settled=True, stable=True))
    assert w.view.drive_marks() == (None, [(1, 0)])
    # drawn without errors over a matched pattern
    s = _st(brain, 2, 0, match_found=True, template_x=400.0, template_y=300.0,
            selected_point_x=420.0, selected_point_y=300.0)
    w._refresh_index(dataclasses.replace(s, point_settled=False))
    w.view.set_overlay(s, w.cfg)
    w.view.grab()
    # a new driving session (after a quiet spell) starts a new trail
    w._clock.t += G.DRIVE_NOTE_S + 1
    w._refresh_index(_st(brain, 2, 0))
    assert w.view.drive_marks() == (None, [])
    w._refresh_index(_st(brain, 5, 5))
    assert w.view.drive_marks() == ((5, 5), [])


def test_the_view_places_the_marks_on_the_array_points():
    """The target mark sits on the array point, through the same transform as
    the scan points (so zoom and pan move it with them)."""
    from camera.apps.camera_view import CameraView
    from camera import vision as V
    app = _app()
    v = CameraView()
    v.resize(800, 600)
    v.set_frame(np.zeros((600, 800), np.uint8))
    cfg = Config()
    cfg.scanning.points_x, cfg.scanning.points_y = 3, 3
    cfg.scanning.selected_index_x = cfg.scanning.selected_index_y = 1
    s = type("S", (), {"match_found": True, "selected_point_x": 400.0,
                       "selected_point_y": 300.0, "template_x": 400.0,
                       "template_y": 300.0, "spot_calibrated": False,
                       "spot_found": False})()
    v.set_overlay(s, cfg)
    pos = v.array_point_image(2, 0)
    offs = V.scanning_array_pixel_offsets(3, 3, cfg.scanning.dx_um, cfg.scanning.dy_um,
                                          cfg.scanning.angle_deg,
                                          cfg.image.pixel_size_x_um,
                                          cfg.image.pixel_size_y_um)
    assert pos == pytest.approx((400.0 + offs[0, 2, 0] - offs[1, 1, 0],
                                 300.0 + offs[0, 2, 1] - offs[1, 1, 1]))
    v.set_drive_marks((2, 0), [(1, 1)])
    v.grab()
    v.close()


def test_it_works_in_a_viewer_window(win):
    """The note and the boxes are display: nothing is sent to the service."""
    w, brain, _app_ = win
    sent = []
    orig = brain.set_selected_index
    brain.set_selected_index = lambda *a: sent.append(a) or orig(*a)
    try:
        w._refresh_index(_st(brain, 0, 0))
        w._refresh_index(_st(brain, 3, 5))
        w._refresh_index(_st(brain, 4, 5))
    finally:
        brain.set_selected_index = orig
    assert sent == []
    assert not w.lab_drive.isHidden()


def test_the_real_refresh_follows_the_brain(win):
    """End to end: a client sets the point on the brain, the window's own
    refresh (status from the brain) moves the boxes."""
    import time
    w, brain, app = win
    w._refresh()
    brain.set_selected_index(6, 2)
    t0 = time.monotonic()
    while (brain.status().selected_index_x, brain.status().selected_index_y) != (6, 2):
        assert time.monotonic() - t0 < 5
        time.sleep(0.02)
    w._refresh()
    assert (w.sp_ix.value(), w.sp_iy.value()) == (6, 2)
