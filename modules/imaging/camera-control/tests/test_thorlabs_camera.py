"""The Thorlabs (Zelux CS165MU) backend against a FAKE thorlabs_tsi_sdk: it
adopts the camera's settings (writes only the acquisition set-up), claims the
camera's serial, delivers Mono8 + the 10-bit deep frame from ONE buffer, and
offers exposure / gain / binning through the generic feature model. Offline."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from camera import hwlock
from camera.backends.thorlabs import ThorlabsCamera
from camera.hwlock import HardwareBusy


class FakeFrame:
    def __init__(self, buf):
        self.image_buffer = buf


class FakeTLCamera:
    """The attributes of thorlabs_tsi_sdk's TLCamera the backend uses."""

    def __init__(self, serial):
        self.serial_number = serial
        self.model = "CS165MU"
        self.bit_depth = 10
        self.sensor_width_pixels, self.sensor_height_pixels = 1440, 1080
        self.image_width_pixels, self.image_height_pixels = 64, 48
        # what the camera was LEFT at by a previous session (must be adopted)
        self.exposure_time_us = 2345
        self.exposure_time_range_us = (40, 26843000)
        self.gain = 7
        self.gain_range = (0, 480)
        self.black_level = 12
        self.black_level_range = (0, 100)
        self.binx = self.biny = 1
        self.binx_range = self.biny_range = (1, 16)
        self.writes: list[tuple] = []
        self.armed = False
        self.frames_per_trigger_zero_for_unlimited = 1
        self.image_poll_timeout_ms = 0
        self.operation_mode = "HARDWARE"
        self.disposed = False
        self.pending = 0
        self._ready = True

    def __setattr__(self, name, value):
        if getattr(self, "_ready", False):
            self.writes.append((name, value))
            if name in ("binx", "biny") and self.armed:
                raise RuntimeError("cannot change binning while armed")
        object.__setattr__(self, name, value)

    def arm(self, n):
        object.__setattr__(self, "armed", True)
        self.writes.append(("arm", n))

    def disarm(self):
        object.__setattr__(self, "armed", False)
        self.writes.append(("disarm",))

    def issue_software_trigger(self):
        self.writes.append(("trigger",))

    def get_pending_frame_or_null(self):
        if not self.armed:
            return None
        h, w = self.image_height_pixels, self.image_width_pixels
        buf = (np.arange(h * w, dtype=np.uint16).reshape(h, w) % 1024)
        buf[0, 0] = 1023
        return FakeFrame(buf)

    def dispose(self):
        object.__setattr__(self, "disposed", True)


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    state = {"serials": ["12345"], "cams": []}

    class TLCameraSDK:
        def discover_available_cameras(self):
            return list(state["serials"])

        def open_camera(self, serial):
            cam = FakeTLCamera(serial)
            state["cams"].append(cam)
            return cam

        def dispose(self):
            pass

    mod = types.SimpleNamespace(TLCameraSDK=TLCameraSDK,
                                OPERATION_MODE=types.SimpleNamespace(SOFTWARE_TRIGGERED="SW"))
    pkg = types.ModuleType("thorlabs_tsi_sdk")
    pkg.tl_camera = mod
    monkeypatch.setitem(sys.modules, "thorlabs_tsi_sdk", pkg)
    monkeypatch.setitem(sys.modules, "thorlabs_tsi_sdk.tl_camera", mod)
    return state


def test_open_adopts_settings_and_only_sets_up_acquisition(sdk):
    cam = ThorlabsCamera(dll_dir="Z:/nowhere")
    cam.open()
    try:
        fake = sdk["cams"][0]
        written = {w[0] for w in fake.writes}
        # acquisition set-up only -- never exposure / gain / black level / binning
        assert written <= {"frames_per_trigger_zero_for_unlimited", "image_poll_timeout_ms",
                           "operation_mode", "arm", "trigger"}
        assert cam.get_feature("ExposureTime") == 2345 and cam.get_feature("Gain") == 7
        assert cam.pixel_format == "Mono10"
        assert "CS165MU" in cam.idn() and "12345" in cam.idn()
    finally:
        cam.close()
    assert sdk["cams"][0].disposed


def test_grab_gives_mono8_and_the_10bit_frame_from_one_buffer(sdk):
    cam = ThorlabsCamera()
    cam.open()
    try:
        img = cam.grab()
        deep, bits = cam.last_deep()
        assert img.dtype == np.uint8 and img.shape == (48, 64)
        assert deep.dtype == np.uint16 and bits == 10
        assert int(deep[0, 0]) == 1023 and int(img[0, 0]) == 255
        assert np.array_equal(img, (deep >> 2).astype(np.uint8))
        assert cam.deep_note() == ""
    finally:
        cam.close()


def test_features_and_setting_them(sdk):
    cam = ThorlabsCamera()
    cam.open()
    try:
        feats = {f["name"]: f for f in cam.features()}
        assert feats["ExposureTime"]["unit"] == "us"
        assert (feats["Gain"]["min"], feats["Gain"]["max"]) == (0, 480)
        assert feats["DeviceModelName"]["writable"] is False
        cam.set_feature("ExposureTime", 500.0)
        assert cam.get_feature("ExposureTime") == 500.0
        # binning needs the camera disarmed: done around the write, re-armed after
        cam.set_feature("BinningHorizontal", 2)
        fake = sdk["cams"][0]
        assert fake.binx == 2 and fake.armed
        with pytest.raises(KeyError):
            cam.set_feature("DeviceModelName", "x")
    finally:
        cam.close()


def test_a_second_service_on_the_same_camera_is_refused(sdk):
    a = ThorlabsCamera()
    a.open()
    try:
        with pytest.raises(HardwareBusy, match="camera"):
            ThorlabsCamera(device="12345").open()
        assert len(sdk["cams"]) == 1           # the refused one never opened it
    finally:
        a.close()
    assert hwlock.held() == []


def test_no_camera_or_wrong_serial_says_so(sdk):
    sdk["serials"] = []
    with pytest.raises(RuntimeError, match="no Thorlabs camera"):
        ThorlabsCamera().open()
    sdk["serials"] = ["111"]
    with pytest.raises(RuntimeError, match="found: 111"):
        ThorlabsCamera(device="999").open()
    assert hwlock.held() == []
