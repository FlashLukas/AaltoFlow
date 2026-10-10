"""Camera images as a scan detector (recording.py, 2026-10-10).

* an acquisition is NUMBERED and takes a frame grabbed AFTER the request
  (gotchas #17 / #28), at full depth (12 bit in the simulator's 12-bit mode,
  every value 0..4095 possible);
* the recording region: the full frame, a crop centred on the calibrated
  spot (shifted, never shrunk, at the edge), a fixed rectangle; binning sums;
* the frame travels as a BINARY reply part (and as base64 for a plain JSON
  client) -- also encrypted (CurveZMQ);
* auto exposure / gain are switched Off while images are recorded and
  restored afterwards;
* describe tells the truth about the frame (shape, largest value, verbs).

Ports 18970-18973 (non-default).
"""

from __future__ import annotations

import base64
import json
import time

import numpy as np
import pytest

from camera.backends.sim import SimCamera
from camera.config import Config, load_config, save_config
from camera.net.describe import build_manifest
from camera.net.service import CameraService
from camera.sim_system import build_sim_system

zmq = pytest.importorskip("zmq")

CMD, PUB = 18970, 18971
CMD_SEC, PUB_SEC = 18972, 18973


def _wait(cond, timeout=6.0, poll=0.01):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


class CountingCamera(SimCamera):
    """The simulator, with the number of each grab written into pixel (0, 0)
    of its 12-bit frame -- so a test can tell WHICH grab a recorded image is."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.n = 0

    def grab(self):
        f = super().grab()
        self.n += 1
        if self._deep is not None:
            d, b = self._deep
            d[0, 0] = self.n % 4096
        return f


def _brain(bits=12, counting=False, **image):
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.camera.sim_bit_depth = bits
    for k, v in image.items():
        setattr(cfg.image, k, v)
    brain, cam, xy, z = build_sim_system(cfg)
    if counting:
        cam = CountingCamera(xy, z, pixel_size_x_um=cfg.image.pixel_size_x_um,
                             pixel_size_y_um=cfg.image.pixel_size_y_um, bit_depth=bits)
        brain.backend = cam
    return brain, cam


def _take(brain, timeout=5.0):
    n = brain.recorder.acquire()
    assert _wait(lambda: brain.status().image_id == n
                 and not brain.status().image_acquiring, timeout)
    return n, brain.recorder.get("sample")


# ───────────────────────────── acquisitions ──────────────────────────────────

def test_an_acquisition_takes_a_fresh_full_depth_frame():
    brain, cam = _brain(bits=12, counting=True)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 3)
        for _ in range(3):
            started = cam.n                       # grabs started before the request
            n, (meta, frame) = _take(brain)
            assert meta["image_id"] == n and meta["bits"] == 12
            assert frame.dtype == np.uint16 and frame.shape == (480, 640)
            # record_discard_frames = 1: the SECOND grab after the request
            assert int(frame[0, 0]) >= started + 2
            assert brain.status().image_sample_id == n
        # a real 12-bit frame: values above 255 and not only multiples of 16
        assert frame.max() > 255 and frame.max() <= 4095
        assert np.any(frame[1:, 1:] % 16)
    finally:
        brain.shutdown()


def test_without_discards_the_first_grab_after_the_request_is_taken():
    brain, cam = _brain(bits=12, counting=True, record_discard_frames=0)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 3)
        started = cam.n
        _n, (_meta, frame) = _take(brain)
        assert int(frame[0, 0]) >= started + 1
    finally:
        brain.shutdown()


def test_an_eight_bit_camera_records_its_eight_bit_frames():
    brain, cam = _brain(bits=8)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 3)
        _n, (meta, frame) = _take(brain)
        assert meta["bits"] == 8 and frame.dtype == np.uint8
        d = [p for p in build_manifest(brain)["parameters"] if p["id"] == "image"][0]
        assert d["max"] == 255
    finally:
        brain.shutdown()


# ─────────────────────────── region and binning ──────────────────────────────

def test_the_spot_crop_follows_the_calibrated_spot_and_never_shrinks():
    brain, cam = _brain(bits=12, record_roi="spot", record_w=64, record_h=48)
    # not calibrated: refused, with the reason
    with pytest.raises(ValueError, match="not calibrated"):
        brain.recorder.acquire()
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 3)
        sx, sy = cam.spot_px
        brain.set_spot_position(sx, sy)
        _n, (meta, frame) = _take(brain)
        assert frame.shape == (48, 64)
        x0, y0, w, h = meta["roi"]
        assert (w, h) == (64, 48)
        assert abs(x0 + w / 2 - sx) <= 1 and abs(y0 + h / 2 - sy) <= 1
        # the laser is the brightest thing in the crop, near its centre
        r, c = np.unravel_index(np.argmax(frame), frame.shape)
        assert abs(c - 32) <= 3 and abs(r - 24) <= 3
        # a spot calibrated at the frame's edge: shifted, same shape
        brain.set_spot_position(5, 470)
        _n, (meta, frame) = _take(brain)
        assert frame.shape == (48, 64) and meta["roi"][:2] == [0, 480 - 48]
    finally:
        brain.shutdown()


def test_a_fixed_rectangle_and_binning_by_summing():
    brain, cam = _brain(bits=12, record_roi="rect", record_x=100, record_y=50,
                        record_w=41, record_h=30, record_binning=2)
    rec = brain.recorder
    assert rec.box_size() == (30, 40)               # a multiple of the binning
    assert rec.out_shape() == (15, 20)
    src = np.arange(480 * 640, dtype=np.uint16).reshape(480, 640) % 4096
    out = rec.cut(src, 12)
    block = src[50:52, 100:102].astype(np.int64).sum()
    assert out.shape == (15, 20) and int(out[0, 0]) == block
    assert out.dtype == np.uint16 and rec.max_value(12) == 4095 * 4
    c = rec.coords()
    assert c["x"][0] == 100.5 and c["y"][1] == 52.5   # centres of the bins
    assert c["roi"] == [100, 50, 40, 30]
    brain.cfg.image.record_binning = 4
    assert rec.wire_dtype(16) == np.dtype("<u4")     # 16 x 65535 needs 32 bit


def test_describe_tells_the_truth_about_the_image():
    brain, cam = _brain(bits=12, record_roi="rect", record_w=100, record_h=60,
                        record_binning=2)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 3)
        d = [p for p in build_manifest(brain)["parameters"] if p["id"] == "image"][0]
        assert d["type"] == "int" and d["min"] == 0 and d["max"] == 4095 * 4
        assert [dim["length"] for dim in d["dims"]] == [30, 50]
        assert d["read"] == {"verb": "get_image", "key": "image",
                             "args": {"which": "sample"}, "binary": True}
        assert d["acquire"]["trigger_verb"] == "acquire_image"
        assert d["acquire"]["ready"]["setpoint_key"] == "image_id"
        rev = build_manifest(brain)["revision"]
        brain.cfg.image.record_binning = 1
        assert build_manifest(brain)["revision"] != rev   # a new shape: re-fetch
    finally:
        brain.shutdown()


def test_the_recording_settings_round_trip_and_are_kept_valid(tmp_path):
    cfg = Config()
    cfg.image.record_roi, cfg.image.record_binning = "spot", 4
    cfg.image.record_w, cfg.image.record_timeout_s = 96, 4.5
    p = str(tmp_path / "c.ini")
    save_config(cfg, p)
    back = load_config(p)
    assert (back.image.record_roi, back.image.record_binning,
            back.image.record_w, back.image.record_timeout_s) == ("spot", 4, 96, 4.5)
    cfg.image.record_roi, cfg.image.record_binning = "circle", 3
    save_config(cfg, p)
    back = load_config(p)
    assert (back.image.record_roi, back.image.record_binning) == ("full", 1)


# ─────────────────────── auto exposure / gain held off ───────────────────────

def test_auto_gain_is_held_off_while_recording_and_restored():
    brain, cam = _brain(bits=12)
    cam.set_feature("GainAuto", "Continuous")
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 3)
        # a scan asks: Off until the scan's claim ends
        brain.recorder.acquire(in_scan=True)
        assert cam.get_feature("GainAuto") == "Off"
        assert _wait(lambda: "GainAuto (was Continuous)" in brain.status().image_auto_frozen)
        brain.recorder.housekeeping(scan_active=True)
        assert cam.get_feature("GainAuto") == "Off"
        brain.recorder.housekeeping(scan_active=False)
        assert cam.get_feature("GainAuto") == "Continuous"
        # a client without a claim: restored after record_auto_restore_s idle
        brain.cfg.image.record_auto_restore_s = 1.0
        brain.recorder.acquire(in_scan=False)
        assert cam.get_feature("GainAuto") == "Off"
        brain.recorder.housekeeping(scan_active=False)
        assert cam.get_feature("GainAuto") == "Off"            # not idle yet
        time.sleep(1.1)
        brain.recorder.housekeeping(scan_active=False)
        assert cam.get_feature("GainAuto") == "Continuous"
        # and at shutdown, whatever is still held off goes back
        brain.recorder.acquire(in_scan=True)
        assert cam.get_feature("GainAuto") == "Off"
    finally:
        brain.shutdown()
    assert cam.get_feature("GainAuto") == "Continuous"


# ─────────────────────────────── the wire ────────────────────────────────────

def _req(ctx, port, secure_mod=None):
    s = ctx.socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, 4000)
    if secure_mod is not None:
        assert secure_mod.secure_client(s, "127.0.0.1", "camera")
    s.connect(f"tcp://127.0.0.1:{port}")
    return s


def _ask(s, **m):
    s.send_json(m)
    return s.recv_multipart()


def _decode(parts):
    head = json.loads(parts[0])
    out = {}
    for spec, raw in zip(head.get("binary", []), parts[1:]):
        out[spec["key"]] = np.frombuffer(raw, dtype=np.dtype(spec["dtype"])).reshape(spec["shape"])
    return head, out


def _round_trip(port, secure_mod=None):
    ctx = zmq.Context.instance()
    s = _req(ctx, port, secure_mod)
    try:
        head = json.loads(_ask(s, cmd="acquire_image")[0])
        assert head["ok"], head
        n = head["image_id"]

        def done():
            st = json.loads(_ask(s, cmd="status")[0])["status"]
            return st["image_id"] == n and not st["image_acquiring"]
        assert _wait(done)
        parts = _ask(s, cmd="get_image", which="sample", binary=True)
        assert len(parts) == 2                       # header + one raw part
        head, arrays = _decode(parts)
        assert head["ok"] and head["image_meta"]["image_id"] == n
        assert head["binary"] == [{"key": "image", "dtype": "<u2", "shape": [480, 640]}]
        frame = arrays["image"]
        # the same frame for a client that did not ask for binary parts
        plain = _ask(s, cmd="get_image", which="sample")
        assert len(plain) == 1
        img = json.loads(plain[0])["image"]
        again = np.frombuffer(base64.b64decode(img["b64"]),
                              dtype=np.dtype(img["dtype"])).reshape(img["shape"])
        assert np.array_equal(frame, again)
        coords = json.loads(_ask(s, cmd="image_coords")[0])
        assert len(coords["x"]) == 640 and coords["um_per_px"][0] > 0
        return frame
    finally:
        s.close(0)


def test_the_frame_travels_as_a_binary_part():
    brain, cam = _brain(bits=12)
    svc = CameraService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 3)
        frame = _round_trip(CMD)
        assert frame.max() <= 4095 and frame.max() > 255
    finally:
        svc.stop()


def test_the_binary_part_travels_encrypted(tmp_path, monkeypatch):
    """CurveZMQ encrypts every part of a message: with the lab's policy
    securing the camera, a keyed client gets the frame, a plain one nothing."""
    from camera import secure
    me = tmp_path / "pc-a"
    kr = tmp_path / "keyring"
    me.mkdir()
    kr.mkdir()
    public, secret = secure.new_keypair()
    meta = {"pc": "pc-a", "host": "pc-a", "machine": "yes"}
    secure.write_cert(me / secure.OWN_PUBLIC, public, meta=meta)
    secure.write_cert(me / secure.OWN_SECRET, public, secret, meta=meta)
    secure.write_cert(kr / "pc-a.key", public, meta=meta)
    (me / secure.SETTINGS_FILE).write_text(json.dumps({"keyring": str(kr)}),
                                           encoding="utf-8")
    (kr / secure.POLICY_FILE).write_text(
        json.dumps({"mode": "enforce", "modules": ["camera"]}), encoding="utf-8")
    monkeypatch.setenv("AALTOFLOW_SECURITY_DIR", str(me))

    brain, cam = _brain(bits=12)
    svc = CameraService(brain, host="127.0.0.1", cmd_port=CMD_SEC, pub_port=PUB_SEC,
                        status_hz=20)
    svc.start()
    try:
        assert svc._guard is not None                 # really secured
        assert _wait(lambda: brain.status().frame_number > 3)
        frame = _round_trip(CMD_SEC, secure_mod=secure)
        assert frame.shape == (480, 640)
        # a plain client is not answered at all
        ctx = zmq.Context.instance()
        s = ctx.socket(zmq.REQ)
        s.setsockopt(zmq.LINGER, 0)
        s.setsockopt(zmq.RCVTIMEO, 800)
        s.connect(f"tcp://127.0.0.1:{CMD_SEC}")
        s.send_json({"cmd": "image_coords"})
        with pytest.raises(zmq.Again):
            s.recv_multipart()
        s.close(0)
    finally:
        svc.stop()
