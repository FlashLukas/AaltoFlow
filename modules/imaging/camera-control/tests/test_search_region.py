"""The pattern SEARCH REGION on the Pattern tracking card (Lukas, 2026-10-12).

A poor pattern can make the match jump around; after refocusing, the user
enlarges the search region -- or sets 0 = the whole frame -- to catch the
pattern back where it now is. The region is `pattern.safety_area_px`.
"""

from __future__ import annotations

import pytest

from camera.backends.sim import SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config

from test_backup_patterns import ScrollingCamera, _wait


@pytest.fixture
def rig():
    cfg = Config()
    cfg.camera.frame_rate = 100.0
    cfg.hardware.use_z = False
    cfg.pattern.autosave = False
    cam = ScrollingCamera()
    brain = Camera(cam, SimXYStage(), SimZFocus(), cfg)
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    yield brain, cam
    brain.shutdown()


def _jump(brain, cam, ox):
    """Move the scene in ONE frame, far beyond a small search box."""
    cam.ox = ox
    n = brain.status().frame_number
    assert _wait(lambda: brain.status().frame_number >= n + 3)


def test_a_small_region_does_not_follow_a_jump_but_zero_finds_it(rig):
    brain, cam = rig
    brain.cfg.pattern.safety_area_px = 30
    brain.capture_reference((200, 240, 60, 60))
    brain.set_tracking(True)
    assert _wait(lambda: brain.status().match_found)
    x0 = brain.status().template_x

    _jump(brain, cam, cam.ox + 150)          # pattern moves 150 px LEFT in the image
    assert not brain.status().match_found    # outside a 30 px region: not followed

    brain.set_config({"pattern": {"safety_area_px": 0}})     # the card's "whole frame"
    assert _wait(lambda: brain.status().match_found)
    assert brain.status().template_x == pytest.approx(x0 - 150, abs=2)


def test_an_enlarged_region_catches_it_too(rig):
    brain, cam = rig
    brain.cfg.pattern.safety_area_px = 30
    brain.capture_reference((200, 240, 60, 60))
    brain.set_tracking(True)
    assert _wait(lambda: brain.status().match_found)
    x0 = brain.status().template_x
    _jump(brain, cam, cam.ox + 120)
    assert not brain.status().match_found
    brain.set_config({"pattern": {"safety_area_px": 200}})
    assert _wait(lambda: brain.status().match_found)
    assert brain.status().template_x == pytest.approx(x0 - 120, abs=2)


def test_the_card_box_sends_the_region(qtbot=None):
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    from camera.apps.gui import MainWindow
    from camera.sim_system import build_sim_system

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg = Config()
    cfg.pattern.autosave = False
    built = build_sim_system(cfg)
    brain = built[0] if isinstance(built, tuple) else built
    win = MainWindow(brain, cfg)
    try:
        assert win.sp_search.specialValueText() == "whole frame"
        win.sp_search.setValue(0)
        win._apply_search_region()            # what the 300 ms timer calls
        assert brain.cfg.pattern.safety_area_px == 0
        win.sp_search.setValue(250)
        win._apply_search_region()
        assert brain.cfg.pattern.safety_area_px == 250
    finally:
        win.close()
        app.processEvents()
