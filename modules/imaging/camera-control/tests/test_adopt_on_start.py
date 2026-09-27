"""Adopt-on-start (Lukas, 2026-09-27): "all modules should read the instrument
state on startup, not to change anything".

For the camera that means: starting the service must not write a single camera
parameter (no Default UserSet, no PixelFormat, no exposure from the .ini). It
READS the exposure and shows that. The .ini value reaches the camera only when
the user changes it explicitly.
"""

from __future__ import annotations

import sys
import types

import pytest

from camera.config import Config
from camera.sim_system import build_sim_system


def _recording(cam):
    """Wrap cam.set_feature so every write is recorded (and can be forbidden)."""
    writes = []
    real = cam.set_feature

    def set_feature(name, value):
        writes.append((name, value))
        return real(name, value)

    cam.set_feature = set_feature
    return writes


@pytest.fixture
def started_on_a_used_camera():
    """A sim camera that someone already tuned (NOT the sim defaults), and an
    .ini that remembers a DIFFERENT exposure -- the case where pushing the .ini
    would visibly change the camera."""
    cfg = Config()
    cfg.camera.exposure_us = 1434.27          # what camera.ini says
    brain, cam, xy, z = build_sim_system(cfg)
    cam.set_feature("ExposureTime", 2500.0)   # what the camera is really at
    cam.set_feature("Gain", 3.0)
    cam.set_feature("Gamma", 0.8)
    writes = _recording(cam)
    brain.start()
    try:
        yield brain, cam, writes
    finally:
        brain.shutdown()


def test_start_writes_no_camera_parameter(started_on_a_used_camera):
    brain, cam, writes = started_on_a_used_camera
    assert writes == []
    # the camera kept everything it had
    assert cam.get_feature("ExposureTime") == 2500.0
    assert cam.get_feature("Gain") == 3.0
    assert cam.get_feature("Gamma") == 0.8


def test_start_adopts_the_cameras_exposure(started_on_a_used_camera):
    brain, cam, writes = started_on_a_used_camera
    # the config (GUI, get_config, a later Save) now shows what the camera does
    assert brain.cfg.camera.exposure_us == pytest.approx(2500.0)
    feats = {f["name"]: f["value"] for f in brain.camera_features()}
    assert feats["ExposureTime"] == 2500.0 and feats["Gain"] == 3.0


def test_set_config_with_the_adopted_exposure_writes_nothing(started_on_a_used_camera):
    brain, cam, writes = started_on_a_used_camera
    # the Settings dialog sends the whole config back unchanged: not a request
    brain.set_config({"camera": {"exposure_us": 2500.0, "frame_rate": 12.0}})
    assert writes == []
    assert cam.get_feature("ExposureTime") == 2500.0


def test_set_config_with_a_new_exposure_is_applied(started_on_a_used_camera):
    brain, cam, writes = started_on_a_used_camera
    brain.set_config({"camera": {"exposure_us": 800.0}})
    assert writes == [("ExposureTime", 800.0)]
    assert cam.get_feature("ExposureTime") == 800.0
    # and the same value again is not a second request
    brain.set_config({"camera": {"exposure_us": 800.0}})
    assert len(writes) == 1


def test_live_panel_change_keeps_config_in_step(started_on_a_used_camera):
    brain, cam, writes = started_on_a_used_camera
    brain.set_camera_feature("ExposureTime", 1200.0)
    assert brain.cfg.camera.exposure_us == pytest.approx(1200.0)
    brain.set_config({"camera": {"exposure_us": 1200.0}})   # round trip
    assert writes == [("ExposureTime", 1200.0)]


# --------------------------------------------------------------------------- #
# the real IDS driver, against a fake IDS peak SDK
# --------------------------------------------------------------------------- #
class _Entry:
    def __init__(self, v):
        self._v = v

    def SymbolicValue(self):
        return self._v


class _Node:
    """A GenICam node that records every write."""

    def __init__(self, name, log, value=None):
        self.name, self.log, self._value = name, log, value

    def Value(self):
        return self._value

    def CurrentEntry(self):
        return _Entry(self._value)

    def SetValue(self, v):
        self.log.append(("SetValue", self.name, v))

    def SetCurrentEntry(self, v):
        self.log.append(("SetCurrentEntry", self.name, v))

    def Execute(self):
        self.log.append(("Execute", self.name))

    def WaitUntilDone(self):
        pass


class _NodeMap:
    def __init__(self, log):
        self.log = log
        self.values = {"PixelFormat": "Mono12g24IDS", "PayloadSize": 1024,
                       "DeviceModelName": "U3-386xCP-M", "ExposureTime": 900.0}

    def FindNode(self, name):
        return _Node(name, self.log, self.values.get(name))


class _Stream:
    def NumBuffersAnnouncedMinRequired(self):
        return 2

    def AllocAndAnnounceBuffer(self, n):
        return object()

    def QueueBuffer(self, b):
        pass

    def StartAcquisition(self):
        pass


def _fake_sdk(log):
    nodemap = _NodeMap(log)
    remote = types.SimpleNamespace(NodeMaps=lambda: [nodemap])
    stream_descr = types.SimpleNamespace(OpenDataStream=lambda: _Stream())
    dev = types.SimpleNamespace(RemoteDevice=lambda: remote,
                                DataStreams=lambda: [stream_descr])
    descr = types.SimpleNamespace(OpenDevice=lambda access: dev,
                                  SerialNumber=lambda: "1", DisplayName=lambda: "cam")
    dm = types.SimpleNamespace(Update=lambda: None, Devices=lambda: [descr])
    peak = types.SimpleNamespace(
        Library=types.SimpleNamespace(Initialize=lambda: None, Close=lambda: None),
        DeviceManager=types.SimpleNamespace(Instance=lambda: dm),
        DeviceAccessType_Control=1)
    mod_peak = types.ModuleType("ids_peak")
    mod_peak.ids_peak = peak
    mod_peak.ids_peak_ipl_extension = types.SimpleNamespace()
    mod_ipl = types.ModuleType("ids_peak_ipl")
    mod_ipl.ids_peak_ipl = types.SimpleNamespace()
    return mod_peak, mod_ipl


def test_ids_open_writes_only_acquisition_start(monkeypatch):
    from camera.backends.ids import IDSCamera

    log = []
    mod_peak, mod_ipl = _fake_sdk(log)
    monkeypatch.setitem(sys.modules, "ids_peak", mod_peak)
    monkeypatch.setitem(sys.modules, "ids_peak_ipl", mod_ipl)
    cam = IDSCamera()
    cam.open()
    # no UserSetLoad, no UserSetSelector, no PixelFormat: the ONLY node write is
    # starting acquisition, without which no frame arrives at all
    assert log == [("Execute", "AcquisitionStart")]
    # the format the camera was left in is adopted, not forced to Mono8
    assert cam.pixel_format == "Mono12g24IDS"
