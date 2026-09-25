"""Service <-> client round-trip over ZeroMQ on NON-default ports (§9)."""

import time

import pytest

from camera.config import Config
from camera.net.client import CameraClient
from camera.net.service import CameraService
from camera.sim_system import build_sim_system

CMD, PUB = 15694, 15695   # non-default so it never collides with a real service


def _wait(cond, timeout=6.0, poll=0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(poll)
    return False


@pytest.fixture
def wired():
    cfg = Config()
    cfg.camera.frame_rate = 200.0
    brain, cam, xy, z = build_sim_system(cfg)
    svc = CameraService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = CameraClient("127.0.0.1", CMD, PUB, timeout_ms=3000)
    cli.start()
    assert _wait(lambda: cli.status().frame_number > 2)
    cli.calibrate_spot(10)          # the spot position is a user step now, over the wire too
    yield brain, cam, cli
    cli.close()
    svc.stop()


def test_info_and_config_over_wire(wired):
    _brain, _cam, cli = wired
    info = cli.info()
    assert "idn" in info and "limits" in info
    cfg = cli.get_config()
    assert "scanning" in cfg and "limits" in cfg
    # the UI/theme group round-trips over the wire
    assert cfg["ui"]["theme"] in ("dark", "light")
    cli.set_config({"ui": {"theme": "light"}})
    assert cli.get_config()["ui"]["theme"] == "light"


def test_status_pub_frame_arrives(wired):
    _brain, _cam, cli = wired
    n0 = cli.status().frame_number
    assert _wait(lambda: cli.status().frame_number > n0)


def test_tracking_and_stabilise_over_wire(wired):
    brain, cam, cli = wired
    tcx, tcy = cam.template_center_px()
    cli.capture_reference((tcx, tcy, 60, 60))
    cli.set_tracking(True)
    assert _wait(lambda: cli.status().match_found)
    cli.set_selected_index(2, 2)
    cli.set_stabilize(True)
    assert _wait(lambda: cli.status().stable, timeout=6.0)


def test_move_clamps_over_wire(wired):
    _brain, _cam, cli = wired
    tgt = cli.move_xy(9999, -9999)
    assert tgt == [130.0, 0.0]


def test_objective_and_frame_over_wire(wired):
    _brain, _cam, cli = wired
    res = cli.set_objective("50x - Zeiss NA 0.8")
    assert abs(res["pixel_size_x_um"] - 0.1652) < 1e-6
    frame = cli.get_frame()
    assert frame is not None and frame.shape == (480, 640)


def test_scan_area_and_analysis_over_wire(wired):
    brain, cam, cli = wired
    tcx, tcy = cam.template_center_px()
    cli.capture_reference((tcx, tcy, 60, 60))
    cli.set_tracking(True)
    assert _wait(lambda: cli.status().match_found)
    res = cli.set_scan_area(tcx, tcy, 100, 80)
    assert res["dx_um"] > 0 and res["repinned"] is True
    cli.set_accuracy_logging(True)
    assert _wait(lambda: len(cli.get_accuracy()["dx"]) > 2)
    cli.set_accuracy_logging(False)


def test_camera_features_over_wire(wired):
    _brain, _cam, cli = wired
    feats = cli.camera_features()
    assert any(f["name"] == "ExposureTime" for f in feats)
    assert cli.set_camera_feature("Gain", 4.0) == 4.0
    assert cli.get_camera_feature("Gain") == 4.0
