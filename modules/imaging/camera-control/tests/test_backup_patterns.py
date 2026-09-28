"""Backup patterns: tracking survives the main template leaving the screen.

A camera that looks at a window of a big textured "sample" and scrolls it --
exactly what a long scan does to the image -- so the main template can be pushed
off the frame while a backup, captured earlier with both in view, carries on.
"""

from __future__ import annotations

import time

import cv2
import numpy as np
import pytest

from camera.backends.sim import SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config
from camera.template_io import BackupPattern, Reference, load_template, save_template

W, H = 640, 480


class ScrollingCamera:
    def __init__(self):
        rng = np.random.default_rng(7)
        world = rng.integers(0, 255, (1200, 2400)).astype(np.float32)
        world = cv2.GaussianBlur(world, (0, 0), 3)
        world = cv2.normalize(world, None, 0, 255, cv2.NORM_MINMAX)
        self.world = world.astype(np.uint8)
        self.ox, self.oy = 300, 200

    def open(self): pass
    def close(self): pass
    def idn(self): return "scrolling"
    def features(self): return []
    def get_feature(self, name): return None
    def set_feature(self, name, value): pass

    def grab(self):
        return self.world[self.oy:self.oy + H, self.ox:self.ox + W].copy()


def _wait(cond, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def rig():
    cfg = Config()
    cfg.camera.frame_rate = 100.0
    cfg.hardware.use_z = False
    cam = ScrollingCamera()
    brain = Camera(cam, SimXYStage(), SimZFocus(), cfg)
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    yield brain, cam
    brain.shutdown()


def _scroll_to(brain, cam, ox, step=15):
    """Scroll in steps well inside the search box, a couple of frames each."""
    while cam.ox != ox:
        cam.ox += max(-step, min(step, ox - cam.ox))
        n = brain.status().frame_number
        assert _wait(lambda: brain.status().frame_number >= n + 2)


def test_backup_takes_over_and_keeps_the_array_anchor(rig):
    brain, cam = rig
    # main template at frame (200, 240) = world (500, 440); backup at frame
    # (480, 240) = world (780, 440): offset (+280, 0)
    brain.capture_reference((200, 240, 60, 60))
    brain.set_tracking(True)
    assert _wait(lambda: brain.status().match_found)
    with pytest.raises(RuntimeError, match="main template first"):
        Camera(ScrollingCamera(), SimXYStage(), SimZFocus()).capture_backup((1, 1, 20, 20))
    brain.capture_backup((480, 240, 60, 60))
    assert brain.list_backups()[0]["offset_px"] == pytest.approx([280.0, 0.0], abs=0.5)

    # scroll right: the image moves LEFT, the main template walks off the frame
    _scroll_to(brain, cam, 300 + 400)
    s = brain.status()
    assert s.match_found and s.pattern_driver == 1
    assert s.backups_n == 1
    # the main template is now at frame x = 500 - 700 = -200: off-screen, but known
    assert s.anchor_x == pytest.approx(-200.0, abs=1.5)
    assert s.anchor_y == pytest.approx(240.0, abs=1.5)
    assert s.template_x == pytest.approx(80.0, abs=1.5)      # the driver, in view

    # scroll back: the backup still has room, so it KEEPS driving (no flip-flop)
    _scroll_to(brain, cam, 300)
    s = brain.status()
    assert s.pattern_driver == 1 and s.anchor_x == pytest.approx(200.0, abs=1.5)

    # scroll the other way until the backup leaves: the main one takes over again
    _scroll_to(brain, cam, 300 - 180)
    s = brain.status()
    assert s.pattern_driver == 0 and s.anchor_x == pytest.approx(380.0, abs=1.5)


def test_clear_backups_and_new_template_reset(rig):
    brain, cam = rig
    brain.capture_reference((200, 240, 60, 60))
    brain.set_tracking(True)
    assert _wait(lambda: brain.status().match_found)
    brain.capture_backup((480, 240, 60, 60))
    assert _wait(lambda: brain.status().backups_n == 1)
    brain.clear_backups()
    assert brain.list_backups() == []
    brain.capture_backup((480, 300, 60, 60))
    brain.capture_reference((250, 200, 60, 60))   # a new main template drops old backups
    assert brain.list_backups() == []


def test_backups_travel_in_the_pattern_file(tmp_path):
    rng = np.random.default_rng(1)
    main = rng.integers(0, 255, (40, 50)).astype(np.uint8)
    b1 = rng.integers(0, 255, (30, 20)).astype(np.uint8)
    ref = Reference(main, (5.0, -3.0), {"points_x": 3},
                    [BackupPattern(b1, (120.5, -40.25))])
    path = tmp_path / "pattern.png"
    save_template(str(path), ref)
    back = load_template(str(path))
    assert np.array_equal(back.template, main)
    assert len(back.backups) == 1
    assert np.array_equal(back.backups[0].template, b1)
    assert back.backups[0].offset_px == (120.5, -40.25)
    assert "backups" not in back.meta
