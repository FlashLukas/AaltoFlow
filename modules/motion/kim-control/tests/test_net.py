"""Service <-> client round-trip on NON-default ports (§9).

Non-default ports (15698/15699) so this never collides with a real service.
Covers the two-language remote surface: read/set positions in steps AND in
micrometres, plus the velocity/calibration bridge, over the wire.
"""

import time

import pytest

from kim.config import Config
from kim.net.client import KimClient
from kim.net.service import KimService
from kim.sim_system import build_sim_system

CMD, PUB = 15698, 15699


@pytest.fixture()
def service_and_client():
    cfg = Config()
    cfg.calibration.um_per_step_x = 0.02
    brain, _ = build_sim_system(cfg)
    svc = KimService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = KimClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    time.sleep(0.2)  # let PUB warm up
    try:
        yield brain, cli
    finally:
        cli.close()
        svc.stop()
        time.sleep(0.1)


def test_px_calibration_verbs_over_the_wire(tmp_path):
    """The camera calls move_image_px over ZeroMQ: uncalibrated it must refuse
    with a readable error, and once a table exists it must move and report steps."""
    from kim import pxcal

    cfg = Config()
    cfg.calibration.px_file = str(tmp_path / "px.json")
    brain, backend = build_sim_system(cfg)
    svc = KimService(brain, host="127.0.0.1", cmd_port=CMD + 10, pub_port=PUB + 10, status_hz=20)
    svc.start()
    cli = KimClient(host="127.0.0.1", cmd_port=CMD + 10, pub_port=PUB + 10, timeout_ms=3000)
    cli.start()
    try:
        assert cli.get_px_calibration() is None
        with pytest.raises(RuntimeError, match="no camera px/step calibration"):
            cli.move_image_px(10, 0)
        # install a table: X moves the image up 0.3 px/step, Y right 0.5 px/step
        brain._pxcal = pxcal.PxCalibration(
            table={"85": {"X+": [0, -0.3], "X-": [0, -0.3], "Y+": [0.5, 0], "Y-": [0.5, 0]}},
            context={"objective": "20x"})
        assert _wait(cli, lambda s: s.px_calibrated)
        assert cli.move_image_px(50.0, -30.0, context={"objective": "20x"}) == [100, 100]
        assert _wait(cli, lambda s: s.position_steps[:2] == [100, 100] and not any(s.moving))
        got = cli.get_px_calibration()
        assert got["now"]["geometry"]["X"]["image_dir_deg"] == pytest.approx(-90.0)
        with pytest.raises(RuntimeError, match="geometry differs"):
            cli.move_image_px(1, 0, context={"objective": "50x"})
    finally:
        cli.close()
        svc.stop()
        time.sleep(0.1)


def _wait(cli, pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred(cli.status()):
            return True
        time.sleep(0.02)
    return False


def test_info_and_config(service_and_client):
    _brain, cli = service_and_client
    info = cli.info()
    assert info["axes"] == ["X", "Y", "Z"]
    assert "calibration" in info
    cfg = cli.get_config()
    assert "limits" in cfg and "calibration" in cfg and "motion" in cfg
    # UI theme travels over the wire and round-trips through set_config
    assert cfg["ui"]["theme"] in ("dark", "light")
    cli.set_config({"ui": {"theme": "light"}})
    assert cli.get_config()["ui"]["theme"] == "light"


def test_step_move_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_step_rate(0, 2000)
    target = cli.move_to_step("X", 5000)   # accepts axis by name
    assert target == 5000
    assert _wait(cli, lambda s: s.position_steps[0] == 5000 and not s.moving[0])


def test_um_move_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_step_rate(0, 2000)
    target = cli.move_to_um("X", 100.0)    # 100 um / 0.02 = 5000 steps
    assert target == 5000
    assert _wait(cli, lambda s: abs(s.position_um[0] - 100.0) < 1e-6 and not s.moving[0])


def test_relative_um_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_step_rate(0, 2000)
    cli.move_to_um("X", 20.0)
    assert _wait(cli, lambda s: s.position_steps[0] == 1000 and not s.moving[0])
    cli.move_relative_um("X", 10.0)        # +500 steps
    assert _wait(cli, lambda s: s.position_steps[0] == 1500 and not s.moving[0])


def test_velocity_um_clamped_over_wire(service_and_client):
    _brain, cli = service_and_client
    actual = cli.set_velocity_um("X", 100.0)  # -> 5000 steps/s clamps to 2000 = 40 um/s
    assert abs(actual - 40.0) < 1e-6
    assert _wait(cli, lambda s: abs(s.step_rate[0] - 2000.0) < 1e-6)


def test_move_clamped_over_wire(service_and_client):
    _brain, cli = service_and_client
    target = cli.move_to_step(0, 9_999_999)   # max default 1,250,000
    assert target == 1_250_000


def test_datum_and_display_zero_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_step_rate(0, 2000)
    cli.move_to_step("X", 3000)
    assert _wait(cli, lambda s: s.position_steps[0] == 3000 and not s.moving[0])

    origins = cli.set_zero("X")
    assert origins[0] == 3000
    assert _wait(cli, lambda s: s.rel_steps[0] == 0)

    cli.zero_counter("X")
    assert _wait(cli, lambda s: s.position_steps[0] == 0)


def test_calibration_error_over_wire(service_and_client):
    _brain, cli = service_and_client
    with pytest.raises(RuntimeError):   # {"ok": false} -> client raises
        cli.set_calibration("X", 0.0)


def test_set_leash_over_wire(service_and_client):
    _brain, cli = service_and_client
    state = cli.set_leash(enabled=True, leash_xy=1000, leash_z=400)
    assert state["enabled"] and state["leash_xy"] == 1000 and state["leash_z"] == 400
    assert _wait(cli, lambda s: s.leash and s.limit_hi[0] == 1000 and s.limit_hi[2] == 400)
    # a move now clamps to the leash box, not the absolute limit
    cli.set_step_rate(0, 2000)
    assert cli.move_to_step("X", 99999) == 1000
    assert cli.move_to_step("Z", -99999) == -400


def test_presets_over_wire(service_and_client):
    _brain, cli = service_and_client
    s = cli.set_speed(False)
    assert s["fast"] is False
    assert _wait(cli, lambda st: st.speed_fast is False and abs(st.step_rate[0] - 300.0) < 1e-6)
    st = cli.set_step_size(True)
    assert st["large"] is True
    assert _wait(cli, lambda s2: s2.step_large and abs(s2.voltage[0] - 125.0) < 1e-6)


def test_position_list_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_step_rate(1, 2000)
    cli.move_to_step("Y", 200)
    assert _wait(cli, lambda s: not s.moving[1])
    cli.store_position(0, "wire")
    positions = cli.get_positions()
    assert positions[0]["used"] and positions[0]["name"] == "wire"
