"""The vacuum setup's rig (2026-11-09): motion = "split" -- XY on an Agilis
AG-UC2 (agilis-control), Z on a KIM001 moving the objective (kim001-control) --
and hardware.xy_map, the axis map every stage without its own camera
calibration needs (piezo, agilis; kim does the image maths itself).

Offline: a fake kim-language service on random ports (test_remote_kim's), the
simulated camera for the brain-level checks.
"""

from __future__ import annotations

import time

import pytest

from camera.backends.remote_agilis import AgilisXYStage, agilis_link
from camera.backends.remote_kim import KimLink, KimXYStage, KimZFocus, StepXYStage
from camera.config import Config, image_um_to_stage, load_config, parse_xy_map, save_config
from camera.sim_system import build_sim_system, build_split_motion

from test_remote_kim import FakeKimService


# ---- the axis map -------------------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ("+X+Y", ((0, 1), (1, 1))),
    ("+y-x", ((1, 1), (0, -1))),
    (" -X -Y ", ((0, -1), (1, -1))),
])
def test_parse_xy_map(text, want):
    assert parse_xy_map(text) == want


@pytest.mark.parametrize("bad", ["", "+X+X", "XY", "+Z+Y", "+X+Y+", "*X+Y"])
def test_bad_xy_maps_are_refused(bad):
    assert parse_xy_map(bad) is None


def test_image_move_to_stage_move():
    assert image_um_to_stage(2.0, 3.0, "+X+Y") == (2.0, 3.0)
    # stage +Y moves the image right, stage -X moves it down
    assert image_um_to_stage(2.0, 3.0, "+Y-X") == (-3.0, 2.0)
    assert image_um_to_stage(2.0, 3.0, "-X+Y") == (-2.0, 3.0)


def test_loading_fixes_bad_values_of_the_new_fields(tmp_path):
    cfg = Config()
    cfg.hardware.motion, cfg.hardware.xy_stage, cfg.hardware.z_stage = "bogus", "x", "y"
    cfg.hardware.xy_map = "+X+X"
    p = str(tmp_path / "camera.ini")
    save_config(cfg, p)
    h = load_config(p).hardware
    assert (h.motion, h.xy_stage, h.z_stage, h.xy_map) == ("kim", "agilis", "kim001", "+X+Y")
    cfg = Config()
    cfg.hardware.motion, cfg.hardware.xy_map = "split", "+Y-X"
    cfg.hardware.z_stage, cfg.camera.driver = "zpiezo", "thorlabs"
    save_config(cfg, p)
    back = load_config(p)
    assert (back.hardware.motion, back.hardware.xy_map, back.hardware.z_stage,
            back.camera.driver) == ("split", "+Y-X", "zpiezo", "thorlabs")


# ---- the brain uses the map -------------------------------------------------------

def _wait(cond, timeout=6.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize("xy_map", ["+X+Y", "+Y-X"])
def test_click_to_go_moves_the_stage_axes_the_map_names(xy_map):
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    cfg.hardware.xy_map = xy_map
    brain, cam, xy, z = build_sim_system(cfg)
    brain.start()
    try:
        assert _wait(lambda: brain.status().frame_number > 3)
        brain.calibrate_spot(5)
        spot = brain.spot_position()
        x0, y0 = xy.read_xy()
        px = cfg.image.pixel_size_x_um
        # click 10 px right of the spot: that point must come under the spot,
        # i.e. the image content moves 10 px LEFT
        brain.click_to_go(spot[0] + 10, spot[1])
        x1, y1 = xy.read_xy()
        want = image_um_to_stage(-10 * px, 0.0, xy_map)
        assert (x1 - x0, y1 - y0) == pytest.approx(want, abs=1e-6)
    finally:
        brain.shutdown()


def test_the_default_map_keeps_the_piezo_rig_unchanged():
    assert Config().hardware.xy_map == "+X+Y"


# ---- the remote adapters -------------------------------------------------------------

def test_agilis_xy_has_no_image_frame_moves():
    """The camera tests `hasattr(xy, "move_image_px")` to pick the kim path:
    the agilis adapter must NOT have it (it would call kim's verb on agilis)."""
    assert not hasattr(AgilisXYStage, "move_image_px")
    assert hasattr(KimXYStage, "move_image_px")
    assert issubclass(AgilisXYStage, StepXYStage)


def test_agilis_link_is_keyed_agilis_and_drives_x_and_y():
    svc = FakeKimService()
    try:
        link = agilis_link("127.0.0.1", svc.cmd_port, svc.pub_port, timeout_ms=1000)
        assert link.module == "agilis"
        xy = AgilisXYStage(link)
        xy.open()
        try:
            assert _wait(lambda: xy.available()[0])
            xy.move_xy(1.0, -2.0)
            # wait for the POSITION, not "not moving": right after the command
            # the cached frame can still say "not moving" (gotcha #2)
            assert _wait(lambda: abs(xy.read_xy()[1] + 2.0) < 0.05)
            x, y = xy.read_xy()
            assert x == pytest.approx(1.0, abs=0.05) and y == pytest.approx(-2.0, abs=0.05)
            assert [a for a, _ in svc.moves] == ["X", "Y"]
        finally:
            xy.close()
    finally:
        svc.close()


def test_build_split_motion_wires_agilis_and_kim001():
    cfg = Config()
    cfg.hardware.motion = "split"
    xy, z = build_split_motion(cfg)
    assert isinstance(xy, AgilisXYStage) and xy.link.module == "agilis"
    assert (xy.link.cmd_port, xy.link.pub_port) == (5595, 5596)
    assert isinstance(z, KimZFocus) and z.link.module == "kim001"
    assert (z.link.cmd_port, z.link.pub_port) == (cfg.hardware.kim001_cmd_port,
                                                  cfg.hardware.kim001_pub_port)


def test_split_with_both_on_kim_shares_one_link():
    cfg = Config()
    cfg.hardware.motion = "split"
    cfg.hardware.xy_stage, cfg.hardware.z_stage = "kim", "kim"
    xy, z = build_split_motion(cfg)
    assert xy.link is z.link and xy.link.module == "kim"


def test_split_without_z():
    cfg = Config()
    cfg.hardware.z_stage = "none"
    _xy, z = build_split_motion(cfg)
    assert type(z).__name__ == "SimZFocus"


def test_the_blue_box_example_loads_as_a_thorlabs_split_rig():
    """examples/bluebox_split_rig.ini (the vacuum microscope) must keep loading
    when config keys change: Thorlabs camera, Agilis XY, KIM001 Z."""
    from pathlib import Path

    from camera.config import load_config
    ini = Path(__file__).resolve().parents[1] / "examples" / "bluebox_split_rig.ini"
    cfg = load_config(str(ini))
    assert cfg.camera.driver == "thorlabs"
    assert (cfg.hardware.motion, cfg.hardware.xy_stage, cfg.hardware.z_stage) == (
        "split", "agilis", "kim001")
    assert cfg.hardware.xy_map == "+X-Y"               # measured: +X right, +Y up
    assert cfg.camera.thorlabs_dll_dir.endswith("ThorCam")  # 2.3 crashed there
