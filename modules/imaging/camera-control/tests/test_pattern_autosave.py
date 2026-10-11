"""The live pattern autosave (cfg.pattern.autosave, Lukas 2026-10-11).

A camera restart on the rig lost a 63x template (and its array and selected
point) that had never been saved by hand. Now the live pattern is written to
autosave/pattern_live.png whenever it changes and restored at the next start,
with tracking and the stabiliser left OFF.
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

from camera import camera as camera_mod
from camera.backends.sim import SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config
from camera.template_io import load_template

W, H = 320, 240


class StillCamera:
    """A fixed textured frame: enough for capture_reference."""

    def __init__(self):
        rng = np.random.default_rng(3)
        self.frame = rng.integers(0, 255, (H, W)).astype(np.uint8)

    def open(self): pass
    def close(self): pass
    def idn(self): return "still"
    def features(self): return []
    def get_feature(self, name): return None
    def set_feature(self, name, value): pass
    def grab(self): return self.frame.copy()


def _wait(cond, timeout=15.0):
    # generous: under a loaded full-suite run the autosave thread is slow, and
    # on Windows a file read at the moment it is replaced raises -> try again
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if cond():
                return True
        except OSError:
            pass
        time.sleep(0.02)
    return False


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_CAMERA_AUTOSAVE_DIR", str(tmp_path))
    monkeypatch.setattr(camera_mod, "AUTOSAVE_EVERY_S", 0.05)
    return tmp_path


def _brain(autosave=True):
    cfg = Config()
    cfg.camera.frame_rate = 50.0
    cfg.hardware.use_z = False
    cfg.pattern.autosave = autosave
    return Camera(StillCamera(), SimXYStage(), SimZFocus(), cfg)


def test_a_captured_pattern_is_saved_and_restored_at_the_next_start(folder, capsys):
    b = _brain()
    b.start()
    try:
        assert _wait(lambda: b._last_frame is not None)
        b.cfg.scanning.points_x, b.cfg.scanning.points_y = 30, 28
        b.cfg.scanning.selected_index_x, b.cfg.scanning.selected_index_y = 27, 23
        b.capture_reference((160, 120, 60, 40))
        live = folder / camera_mod.AUTOSAVE_NAME
        assert _wait(live.exists)
    finally:
        b.shutdown()
    saved = load_template(str(live))
    assert saved.template.shape == (40, 60)

    b2 = _brain()
    events = []
    b2._on_event = lambda level, msg: events.append((level, msg))
    b2.start()
    try:
        assert b2.reference is not None
        assert b2.reference.template.shape == (40, 60)
        sc = b2.cfg.scanning
        assert (sc.points_x, sc.points_y) == (30, 28)
        assert (sc.selected_index_x, sc.selected_index_y) == (27, 23)
        assert any("RESTORED" in m for _l, m in events)
        # printed too: at start no client listens to the events yet
        out = capsys.readouterr().out
        assert "live pattern RESTORED" in out and out.isascii()
        st = b2.status()
        assert not getattr(st, "stabilize", False)       # left OFF
    finally:
        b2.shutdown()


def test_a_change_writes_a_new_file_and_keeps_the_previous(folder):
    b = _brain()
    b.start()
    try:
        assert _wait(lambda: b._last_frame is not None)
        b.capture_reference((160, 120, 60, 40))
        live = folder / camera_mod.AUTOSAVE_NAME
        assert _wait(live.exists)
        b.capture_reference((100, 100, 30, 30))           # a new template
        assert _wait(lambda: load_template(str(live)).template.shape == (30, 30))
        prev = folder / camera_mod.AUTOSAVE_PREV_NAME
        assert _wait(lambda: load_template(str(prev)).template.shape == (40, 60))
    finally:
        b.shutdown()


def test_nothing_unchanged_is_rewritten(folder):
    b = _brain()
    b.start()
    try:
        assert _wait(lambda: b._last_frame is not None)
        b.capture_reference((160, 120, 60, 40))
        live = folder / camera_mod.AUTOSAVE_NAME
        assert _wait(live.exists)
        assert b._autosave_pattern_if_changed() is False
    finally:
        b.shutdown()


def test_autosave_off_neither_writes_nor_restores(folder):
    b = _brain(autosave=False)
    b.start()
    try:
        assert _wait(lambda: b._last_frame is not None)
        b.capture_reference((160, 120, 60, 40))
        time.sleep(0.3)
    finally:
        b.shutdown()
    assert not os.listdir(folder)


def test_a_broken_autosave_file_does_not_stop_the_start(folder):
    (folder / camera_mod.AUTOSAVE_NAME).write_bytes(b"not a png")
    b = _brain()
    events = []
    b._on_event = lambda level, msg: events.append((level, msg))
    b.start()
    try:
        assert b.reference is None
        assert any("not restored" in m for _l, m in events)
    finally:
        b.shutdown()
