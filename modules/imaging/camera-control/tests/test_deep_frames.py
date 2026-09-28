"""12-BIT frames for the spot metrics (Lukas, 2026-09-28: "yes").

Why: on the rig the far-defocused sigma^2 read up to 50 % LOW because the
spot's peak was only 11-16 grey levels in 8 bit -- its faint wings, which
carry most of r^2, are below one grey level and round away. The camera itself
delivers 10/12 bit (it was left in Mono12g24IDS). So: grab ONCE and hand the
brain two frames of that one buffer -- the 8-bit one for everything that has
always used it (display, template matching, the fixed threshold, the GUI) and
the full-depth one for the spot's SIZE (second moment, relative area).

Proved here: the simulated camera renders 12 bit on request; in 12 bit the
far-wing sigma^2 is within a few % of the exact value where 8 bit reads
15-65 % low; the brain uses the deep frame (status spot_bit_depth) and still
finds saturation at the deep full scale; the IDS and GenICam backends deliver
both frames from one buffer (fake SDKs), and never write the PixelFormat.
"""

import sys
import time
import types

import numpy as np
import pytest

from camera import vision as V
from camera.backends.sim import SimCamera, SimXYStage, SimZFocus
from camera.camera import Camera
from camera.config import Config, load_config, save_config

ZF = 30.0


def _cam(bits, noise=0.3):
    xy = SimXYStage()
    z = SimZFocus(z0=ZF, z_focus=ZF, vmin=0.0, vmax=75.0)
    return SimCamera(xy, z, spot_model="coherent", noise=noise, bit_depth=bits), z


def test_the_sim_camera_renders_both_depths_from_one_scene():
    cam, z = _cam(12)
    g8 = cam.grab()
    deep, bits = cam.last_deep()
    assert g8.dtype == np.uint8 and deep.dtype == np.uint16 and bits == 12
    assert deep.shape == g8.shape
    # the same photons: the 12-bit frame / 16 is the 8-bit one within rounding
    assert np.abs(deep.astype(float) / 16.0 - g8).max() <= 0.51
    assert cam.get_feature("PixelFormat") == "Mono12"
    cam8, _ = _cam(8)
    cam8.grab()
    assert cam8.last_deep() is None


def test_far_wings_twelve_bit_is_right_where_eight_bit_reads_low():
    """The rig's problem, in the simulator: a quiet camera (0.3 grey levels of
    noise), far out of focus on the side where the light sits in a faint outer
    ring. Measured 2026-09-28: 8 bit -63 % at -7.5 and -17 % at -6; 12 bit
    -3 % and -1 %."""
    sp = Config().spot
    sp.lookup_region_px = 150
    cam, z = _cam(12)
    for dz, bad in ((-7.5, 0.30), (-6.0, 0.12)):
        z.set_z(ZF + dz)
        r8, r12 = [], []
        for _ in range(4):
            g8 = cam.grab()
            deep, bits = cam.last_deep()
            r8.append(V.spot_second_moment(g8, cam.spot_px, sp).sigma2)
            r12.append(V.spot_second_moment(deep, cam.spot_px, sp,
                                            max_value=(1 << bits) - 1).sigma2)
        true = cam.coherent.true_sigma2(dz)
        assert np.mean(r8) < (1.0 - bad) * true, (dz, np.mean(r8) / true)
        assert np.mean(r12) == pytest.approx(true, rel=0.05), (dz, np.mean(r12) / true)


def test_saturation_is_judged_at_the_deep_full_scale():
    f = np.full((200, 200), 100, np.uint16)
    f[95:105, 95:105] = 4095
    m = V.spot_second_moment(f, (100.0, 100.0), {"lookup_region_px": 60}, max_value=4095)
    assert m.saturated                             # 4095 is the top of 12 bit...
    m = V.spot_second_moment(f, (100.0, 100.0), {"lookup_region_px": 60})
    assert not m.saturated                         # ...not of the uint16 container
    r = V.spot_relative_area(f, (100.0, 100.0), {"lookup_region_px": 60}, max_value=4095)
    assert r.saturated


def _wait(cond, timeout=30.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


def _brain(bits, dz):
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.spot.lookup_region_px = 150
    cfg.spot.thr_lower = 150
    cam, z = _cam(bits)
    brain = Camera(cam, cam.stage, z, cfg)
    brain.start()
    assert _wait(lambda: brain.status().frame_number > 2)
    brain.calibrate_spot(8)
    z.set_z(ZF + dz)
    f0 = brain.status().frame_number
    assert _wait(lambda: brain.status().frame_number > f0 + 3)
    return brain, cam


def test_the_brain_measures_the_size_on_the_deep_frame():
    vals = {}
    for bits in (8, 12):
        brain, cam = _brain(bits, -7.5)
        try:
            s = []
            for _ in range(6):
                f0 = brain.status().frame_number
                assert _wait(lambda: brain.status().frame_number > f0)
                s.append(brain.status().spot_sigma2_px2)
            st = brain.status()
            assert st.spot_bit_depth == bits
            vals[bits] = float(np.nanmean(s)) / cam.coherent.true_sigma2(-7.5)
            # the autofocus metric uses the same frame (engine stopped first:
            # only one thread may grab, as in the real brain)
            brain._stop.set()
            brain._engine.join(2.0)
            brain.cfg.autofocus.mechanism = "spot_d4sigma"
            g = brain._grab_gray()
            m = brain._focus_metric(g)
            assert m == pytest.approx(cam.coherent.true_sigma2(-7.5),
                                      rel=0.08 if bits == 12 else 0.9)
        finally:
            brain.shutdown()
    assert vals[12] == pytest.approx(1.0, abs=0.06), vals
    assert vals[8] < 0.75, vals


def test_the_eight_bit_pipeline_is_unchanged_by_a_deep_camera():
    """Display, template matching and the fixed threshold keep the 8-bit frame."""
    brain, cam = _brain(12, 0.0)
    try:
        g = brain.latest_frame()
        assert g.dtype == np.uint8
        assert brain.status().spot_found                    # the 8-bit threshold still works
    finally:
        brain.shutdown()


def test_sim_bit_depth_is_a_setting(tmp_path):
    cfg = Config()
    assert cfg.camera.sim_bit_depth == 8                    # default: nothing changes
    cfg.camera.sim_bit_depth = 12
    save_config(cfg, str(tmp_path / "c.ini"))
    assert load_config(str(tmp_path / "c.ini")).camera.sim_bit_depth == 12
    cfg.camera.sim_bit_depth = 9                            # not a camera depth
    save_config(cfg, str(tmp_path / "c.ini"))
    assert load_config(str(tmp_path / "c.ini")).camera.sim_bit_depth == 8
    from camera.sim_system import build_sim_system
    cfg.camera.sim_bit_depth = 12
    brain, cam, xy, z = build_sim_system(cfg)
    assert cam.bit_depth == 12


# --------------------------------------------------------------------------- #
# the real drivers, against fake SDKs
# --------------------------------------------------------------------------- #
class _Img:
    def __init__(self, arr, log):
        self.arr, self.log = arr, log

    def ConvertTo(self, fmt):
        self.log.append(fmt)
        if fmt == "Mono8":
            return _Np((self.arr >> 4).astype(np.uint8))
        if fmt == "Mono12":
            return _Np(self.arr.astype(np.uint16))
        raise RuntimeError(f"cannot convert to {fmt}")


class _Np:
    def __init__(self, a):
        self.a = a

    def get_numpy_2D(self):
        return self.a


def _ids(pixel_format, arr, log, msb=False):
    from camera.backends.ids import IDSCamera
    cam = IDSCamera()
    cam.pixel_format = pixel_format
    queued = []
    cam._stream = types.SimpleNamespace(WaitForFinishedBuffer=lambda t: "buf",
                                        QueueBuffer=queued.append)
    src = (arr << 4) if msb else arr

    class _Img16(_Img):
        def ConvertTo(self, fmt):
            if msb and fmt == "Mono12":
                self.log.append(fmt)
                return _Np(src.astype(np.uint16))          # left-aligned 16-bit data
            return super().ConvertTo(fmt)
    cam._ext = types.SimpleNamespace(BufferToImage=lambda b: _Img16(arr, log))
    cam._ipl = types.SimpleNamespace(PixelFormatName_Mono8="Mono8",
                                     PixelFormatName_Mono10="Mono10",
                                     PixelFormatName_Mono12="Mono12",
                                     PixelFormatName_Mono16="Mono16")
    return cam, queued


@pytest.mark.parametrize("msb", [False, True])
def test_ids_delivers_eight_bit_and_twelve_bit_from_one_buffer(msb):
    arr = (np.arange(64 * 48, dtype=np.uint16).reshape(48, 64) % 4096)
    log = []
    cam, queued = _ids("Mono12g24IDS", arr, log, msb=msb)
    g8 = cam.grab()
    assert g8.dtype == np.uint8 and np.array_equal(g8, (arr >> 4).astype(np.uint8))
    deep, bits = cam.last_deep()
    assert bits == 12 and deep.dtype == np.uint16
    assert np.array_equal(deep, arr)                        # 0..4095, also if MSB-aligned
    assert log == ["Mono8", "Mono12"] and queued == ["buf"]  # one buffer, handed back once


def test_ids_mono8_camera_has_no_deep_frame_and_a_failed_deep_conversion_is_harmless():
    arr = np.full((48, 64), 1000, np.uint16)
    log = []
    cam, queued = _ids("Mono8", arr, log)
    cam.grab()
    assert cam.last_deep() is None and log == ["Mono8"]
    log.clear()
    cam, queued = _ids("Mono10", arr, log)          # the fake cannot make Mono10
    g8 = cam.grab()
    assert g8.dtype == np.uint8 and cam.last_deep() is None
    assert queued == ["buf"]


def test_ids_bit_depth_from_the_pixel_format_name():
    from camera.backends.ids import bit_depth
    assert [bit_depth(n) for n in ("Mono8", "Mono10", "Mono10p", "Mono10g40IDS",
                                   "Mono12", "Mono12p", "Mono12g24IDS", "Mono16", "", "RGB8")] \
        == [8, 10, 10, 10, 12, 12, 12, 16, 8, 8]


def test_genicam_scales_a_sixteen_bit_buffer_instead_of_stretching_it():
    from camera.backends.genicam import GenICamCamera
    arr = np.full((48, 64), 800, np.uint16)
    arr[10, 10] = 4095
    comp = types.SimpleNamespace(data=arr.ravel(), height=48, width=64)
    buf = types.SimpleNamespace(payload=types.SimpleNamespace(components=[comp]))

    class _Fetch:
        def __enter__(self):
            return buf

        def __exit__(self, *a):
            return False
    cam = GenICamCamera()
    cam.pixel_format = "Mono12"
    cam._ia = types.SimpleNamespace(fetch=lambda: _Fetch())
    g8 = cam.grab()
    assert g8.dtype == np.uint8
    assert g8[0, 0] == 800 >> 4 and g8[10, 10] == 255      # scaled by the bit depth, not min-max
    deep, bits = cam.last_deep()
    assert bits == 12 and deep[10, 10] == 4095
