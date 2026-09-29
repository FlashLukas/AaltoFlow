"""A SEPARATE EXPOSURE for autofocus, and "Auto exposure (once)" (2026-09-29).

Lukas: "if I lower the exposure I don't see the main image" -- the working
exposure stays bright (pattern tracking needs the sample), the laser spot
saturates. autofocus.exposure_us (0 = off) switches the camera to a shorter
exposure for an autofocus run (and a Z step calibration, and optionally the
spot calibration) and ALWAYS puts the camera's own exposure back: normal end,
failure, kill, crash, shutdown. Frames already exposed with the other value
(buffers in flight) are thrown away on both switches, and the image loops
(pattern tracking, stabiliser) stand down meanwhile.

"Auto exposure (once)": the camera's own ExposureAuto = Once when it has it,
else a few software steps on the image (the spot's region left out).
"""

import math
import time

import numpy as np
import pytest

from camera import vision as V
from camera.backends.sim import _F, SimCamera, SimSlipStickZ, SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config, load_config, save_config

ZF = 30.0
WORK = 5000.0            # the simulator's working exposure (its identity value)
AFX = 1500.0             # the autofocus exposure: 0.3 x the light


def _wait(cond, timeout=30.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


class LagCam(SimCamera):
    """An ExposureTime write reaches the frames only ``lag`` grabs later -- like
    buffers already queued (exposed) on a real camera. Every grab records the
    exposure it was REALLY taken with."""

    def __init__(self, *a, lag=2, **k):
        super().__init__(*a, **k)
        self.lag = lag
        self._pending = []          # [grabs left, value]
        self.taken = []             # the real exposure of every grab

    def set_feature(self, name, value):
        if name == "ExposureTime" and self.lag > 0:
            self._pending.append([self.lag, float(value)])
            self.requested = float(value)
            return
        super().set_feature(name, value)

    def get_feature(self, name):
        if name == "ExposureTime" and self._pending:
            return self._pending[-1][1]          # the camera reports the new value at once
        return super().get_feature(name)

    def grab(self):
        for p in list(self._pending):
            p[0] -= 1
            if p[0] < 0:
                super().set_feature("ExposureTime", p[1])
                self._pending.remove(p)
        f = super().grab()
        self.taken.append(float(super().get_feature("ExposureTime")))
        return f


def _rig(lag=0, peak=700.0, slip=True, step_ms=0.0):
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.hardware.z_step_time_ms = step_ms
    cfg.autofocus.averages_per_level = 2
    cfg.spot.lookup_region_px = 100
    cfg.spot.thr_lower = 150
    cfg.autofocus.mechanism = "spot_d4sigma"
    cfg.autofocus.routine = "one_way"
    xy = SimXYStage(x0=65.0, y0=65.0)
    z = (SimSlipStickZ if slip else SimZFocus)(z0=ZF, z_focus=ZF, vmin=cfg.limits.z_min_v,
                                               vmax=cfg.limits.z_max_v)
    cam = LagCam(xy, z, pixel_size_x_um=cfg.image.pixel_size_x_um,
                 pixel_size_y_um=cfg.image.pixel_size_y_um, spot_model="coherent",
                 noise=1.5, coherent_peak=peak, lag=lag)
    brain = Camera(cam, xy, z, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    return brain, cam, z, events


def _calibrate(brain):
    brain.cfg.spot.calib_mode = "saturated"
    brain.calibrate_spot(6)


def _run_af(brain):
    brain.autofocus()
    assert _wait(lambda: not brain.status().af_running, 120)
    return brain.status()


def _spy_metric(brain, cam, seen):
    """Record, for every frame the autofocus SCORES, the exposure it was taken
    with and whether the spot was saturated on it."""
    orig = brain._focus_metric

    def spy(gray, *a, **k):
        sp = brain.cfg.spot
        m = V.spot_second_moment(gray, (sp.ref_x, sp.ref_y), sp)
        seen.append((cam.taken[-1], bool(m.saturated)))
        return orig(gray, *a, **k)
    brain._focus_metric = spy


# --------------------------------------------------------------------------- #
def test_switched_for_the_run_unsaturated_there_and_restored_after():
    brain, cam, z, events = _rig()
    try:
        _calibrate(brain)
        assert brain.status().spot_saturated                 # the working exposure saturates
        brain.cfg.autofocus.exposure_us = AFX
        z.set_z(z.read_z() - 3.0 / z.down_gain)
        seen = []
        _spy_metric(brain, cam, seen)
        s = _run_af(brain)
        assert s.af_error == "OK", s.af_error
        assert seen and all(e == AFX for e, _sat in seen)       # every scored frame at AF exposure
        assert not any(sat for _e, sat in seen)                  # ... and unsaturated there
        assert cam.get_feature("ExposureTime") == WORK           # the camera's own is back
        assert not brain.status().af_exposure_active
        assert brain.cfg.camera.exposure_us == WORK              # cfg never saw the AF value
        assert any(f"{WORK:g} -> {AFX:g} us" in m for _l, m in events)
        assert any("restored" in m for _l, m in events)
    finally:
        brain.shutdown()


def test_off_by_default_nothing_is_switched():
    brain, cam, z, events = _rig()
    try:
        _calibrate(brain)
        assert brain.cfg.autofocus.exposure_us == 0.0
        z.set_z(z.read_z() - 2.0 / z.down_gain)
        seen = []
        _spy_metric(brain, cam, seen)
        _run_af(brain)
        assert all(e == WORK for e, _sat in seen)
        assert not any("autofocus exposure" in m for _l, m in events)
    finally:
        brain.shutdown()


def test_restored_after_a_failed_run():
    brain, cam, z, events = _rig()
    try:
        brain.cfg.autofocus.exposure_us = AFX              # no calibrated spot: the run fails
        s = _run_af(brain)
        assert s.af_error != "OK"
        assert cam.get_feature("ExposureTime") == WORK
        assert not brain.status().af_exposure_active
    finally:
        brain.shutdown()


def test_restored_after_a_kill():
    brain, cam, z, events = _rig(step_ms=40.0)
    try:
        _calibrate(brain)
        brain.cfg.autofocus.exposure_us = AFX
        z.set_z(z.read_z() - 6.0 / z.down_gain)
        brain.autofocus()
        assert _wait(lambda: brain.status().af_exposure_active, 10)
        brain.kill_af()
        assert _wait(lambda: not brain.status().af_running, 30)
        assert brain.status().af_error == "killed"
        assert cam.get_feature("ExposureTime") == WORK
    finally:
        brain.shutdown()


def test_restored_after_a_crash_and_the_engine_survives():
    brain, cam, z, events = _rig()
    try:
        _calibrate(brain)
        brain.cfg.autofocus.exposure_us = AFX

        def boom(req):
            raise ValueError("simulated crash inside the run")
        brain._run_autofocus = boom
        brain.autofocus()
        assert _wait(lambda: not brain.status().af_running, 30)
        assert cam.get_feature("ExposureTime") == WORK
        f0 = brain.status().frame_number
        assert _wait(lambda: brain.status().frame_number > f0 + 3)   # still grabbing
    finally:
        brain.shutdown()


def test_restored_at_shutdown_during_a_run():
    brain, cam, z, events = _rig(step_ms=60.0)
    _calibrate(brain)
    brain.cfg.autofocus.exposure_us = AFX
    z.set_z(z.read_z() - 6.0 / z.down_gain)
    brain.autofocus()
    assert _wait(lambda: brain.status().af_exposure_active, 10)
    brain.shutdown()
    assert cam.get_feature("ExposureTime") == WORK


def test_frames_in_flight_are_not_scored_and_the_pattern_is_not_matched_on_them():
    """A camera whose exposure change arrives 2 frames late: with
    exposure_discard_frames = 2 no scored frame has the old exposure, and after
    the run no pattern match runs on a frame still exposed short."""
    brain, cam, z, events = _rig(lag=2)
    try:
        _calibrate(brain)
        brain.capture_reference((440.0, 260.0, 60, 60))
        brain.set_tracking(True)
        assert _wait(lambda: brain.status().match_found, 10)
        matched_on = []
        orig_track = brain._track_patterns

        def spy_track(gray, *a, **k):
            matched_on.append(cam.taken[-1])
            return orig_track(gray, *a, **k)
        brain._track_patterns = spy_track
        brain.cfg.autofocus.exposure_us = AFX
        brain.cfg.autofocus.exposure_discard_frames = 2
        z.set_z(z.read_z() - 2.0 / z.down_gain)
        seen = []
        _spy_metric(brain, cam, seen)
        n_before = len(matched_on)
        s = _run_af(brain)
        assert s.af_error == "OK"
        assert seen and all(e == AFX for e, _sat in seen), [e for e, _s in seen][:4]
        f0 = brain.status().frame_number
        assert _wait(lambda: brain.status().frame_number > f0 + 10)
        assert all(e == WORK for e in matched_on[n_before:]), matched_on[n_before:][:6]
        assert brain.status().match_found
    finally:
        brain.shutdown()


def test_the_z_step_calibration_runs_at_the_af_exposure_too():
    brain, cam, z, events = _rig()
    try:
        _calibrate(brain)
        brain.cfg.autofocus.exposure_us = AFX
        seen = []
        orig = brain._zcal_metric

        def spy(gray):
            seen.append(cam.taken[-1])
            return orig(gray)
        brain._zcal_metric = spy
        brain.calibrate_z_steps()
        assert _wait(lambda: not brain.status().zcal_running, 120)
        assert seen and all(e == AFX for e in seen)
        assert cam.get_feature("ExposureTime") == WORK
    finally:
        brain.shutdown()


def test_one_saturation_line_across_an_af_exposure_switch():
    """Rig 2026-09-29: "the laser spot is SATURATED" came twice per AF run --
    the unsaturated frames at the AF exposure ended the episode, and the
    restored working exposure started a new one. Those frames say nothing
    about the WORKING image: one line per episode of the working image."""
    brain, cam, z, events = _rig()
    try:
        _calibrate(brain)
        assert _wait(lambda: brain.status().spot_saturated)
        f0 = brain.status().frame_number
        assert _wait(lambda: brain.status().frame_number > f0 + 5)
        brain.cfg.autofocus.exposure_us = AFX
        for _ in range(2):
            z.set_z(z.read_z() - 3.0 / z.down_gain)
            s = _run_af(brain)
            assert s.af_error == "OK", s.af_error
            f0 = brain.status().frame_number
            assert _wait(lambda: brain.status().frame_number > f0 + 25)
            assert brain.status().spot_saturated
        said = [m for _l, m in events if "SATURATED" in m]
        assert len(said) == 1, said
    finally:
        brain.shutdown()


def test_calibrate_the_spot_at_the_af_exposure():
    brain, cam, z, events = _rig(peak=900.0)
    try:
        brain.cfg.autofocus.exposure_us = 1000.0           # 0.2 x: peak ~180, unsaturated
        brain.cfg.spot.calib_mode = "unsaturated"
        brain.cfg.spot.calib_at_af_exposure = True
        res = brain.calibrate_spot(6)
        assert res["at_af_exposure"] is True
        assert (res["x"], res["y"]) == pytest.approx(cam.spot_px, abs=0.5)
        assert res["gauss_sigma2"] > 0                      # fitted on UNSATURATED frames
        assert cam.get_feature("ExposureTime") == WORK
        assert not brain.status().af_exposure_active
    finally:
        brain.shutdown()


def test_config_round_trip(tmp_path):
    cfg = Config()
    assert cfg.autofocus.exposure_us == 0.0 and cfg.autofocus.exposure_discard_frames == 2
    cfg.autofocus.exposure_us, cfg.autofocus.exposure_discard_frames = 65.0, 3
    cfg.camera.auto_exposure_target = 0.6
    save_config(cfg, str(tmp_path / "c.ini"))
    back = load_config(str(tmp_path / "c.ini"))
    assert back.autofocus.exposure_us == 65.0 and back.autofocus.exposure_discard_frames == 3
    assert back.camera.auto_exposure_target == 0.6


# --------------------------------------------------------------------------- #
# Auto exposure (once)
# --------------------------------------------------------------------------- #
def test_auto_exposure_in_software_brings_the_image_to_the_target():
    brain, cam, z, events = _rig(peak=220.0)
    try:
        brain.cfg.spot.calib_mode = "unsaturated"
        brain.calibrate_spot(6)
        brain.set_camera_feature("ExposureTime", 2000.0)     # a dark image
        cam_cfg = brain.cfg.camera
        cam_cfg.auto_exposure_percentile = 99.9              # the sim's sample is one glyph
        res = brain.auto_exposure_once()
        assert res["method"] == "software" and res["old_us"] == 2000.0
        new = cam.get_feature("ExposureTime")
        assert res["new_us"] == new and brain.cfg.camera.exposure_us == new
        assert brain._exposure_known == new
        f0 = brain.status().frame_number
        assert _wait(lambda: brain.status().frame_number > f0 + 3)
        level = brain._image_level(brain.latest_frame(), 99.9)
        assert level == pytest.approx(0.7 * 255, rel=0.1)
        assert any("auto exposure" in m and "2000 ->" in m for _l, m in events)
    finally:
        brain.shutdown()


class AutoCam(LagCam):
    """A camera with its own ExposureAuto (GenICam SFNC): "Once" adjusts the
    exposure, then the node reads "Off" again after a few polls."""

    def __init__(self, *a, **k):
        super().__init__(*a, lag=0, **k)
        self._feat["ExposureAuto"] = _F("Exposure Auto", "enum", "Off",
                                        options=["Off", "Once", "Continuous"])
        self._polls = 0

    def set_feature(self, name, value):
        super().set_feature(name, value)
        if name == "ExposureAuto" and value == "Once":
            super().set_feature("ExposureTime", 1234.0)
            self._polls = 3

    def get_feature(self, name):
        if name == "ExposureAuto" and self._polls > 0:
            self._polls -= 1
            if self._polls == 0:
                super().set_feature("ExposureAuto", "Off")
            return "Once"
        return super().get_feature(name)


def test_auto_exposure_uses_the_cameras_own_once_when_it_has_it():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    xy = SimXYStage()
    z = SimZFocus(z0=ZF, z_focus=ZF, vmin=0.0, vmax=75.0)
    cam = AutoCam(xy, z)
    brain = Camera(cam, xy, z, cfg)
    events = []
    brain._on_event = lambda level, msg: events.append((level, msg))
    brain.start()
    try:
        res = brain.auto_exposure_once()
        assert res["method"].startswith("camera") and res["new_us"] == 1234.0
        assert cam.get_feature("ExposureAuto") == "Off"      # left as found: Off
        assert brain.cfg.camera.exposure_us == 1234.0 and brain._exposure_known == 1234.0
        assert any("5000 -> 1234" in m for _l, m in events)
    finally:
        brain.shutdown()
