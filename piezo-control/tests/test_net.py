"""Service <-> client round-trip on NON-default ports (§9).

Non-default ports (15692/15693) so this never collides with a real service.
Covers the remote-control requirements explicitly: read/set individual axis
positions, switch closed/open loop, and set velocity -- all over the wire.
"""

import time

import pytest

from piezo.config import Config
from piezo.net.client import PiezoClient
from piezo.net.service import PiezoService
from piezo.sim_system import build_sim_system

CMD, PUB = 15692, 15693


@pytest.fixture()
def service_and_client():
    cfg = Config()
    cfg.motion.ramp_mode = "off"   # moves settle fast for assertions
    brain, _ = build_sim_system(cfg)
    svc = PiezoService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = PiezoClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    time.sleep(0.2)  # let PUB warm up
    try:
        yield brain, cli
    finally:
        cli.close()
        svc.stop()
        time.sleep(0.1)


def _wait(cli, pred, timeout=4.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred(cli.status()):
            return True
        time.sleep(0.02)
    return False


def test_info_and_config(service_and_client):
    _brain, cli = service_and_client
    info = cli.info()
    assert info["axes"] == ["X", "Y"]
    cfg = cli.get_config()
    assert "limits" in cfg and "motion" in cfg


def test_set_and_read_individual_axis(service_and_client):
    _brain, cli = service_and_client
    target = cli.move_axis("X", 60.0)   # accepts axis by name
    assert target == 60.0
    assert _wait(cli, lambda s: abs(s.position[0] - 60.0) < 0.1 and not s.moving[0])


def test_move_clamped_over_wire(service_and_client):
    _brain, cli = service_and_client
    # default start is closed loop -> CL travel 160 um
    target = cli.move_axis(0, 999)
    assert target == 160.0


def test_toggle_loop_over_wire(service_and_client):
    _brain, cli = service_and_client
    assert cli.set_closed_loop("X", False) is False
    assert _wait(cli, lambda s: s.closed_loop[0] is False and s.travel_max[0] == 200.0)
    assert cli.set_closed_loop("X", True) is True
    assert _wait(cli, lambda s: s.closed_loop[0] is True and s.travel_max[0] == 160.0)


def test_set_velocity_and_ramp_mode_over_wire(service_and_client):
    _brain, cli = service_and_client
    assert cli.set_velocity("Y", 321.0) == 321.0
    assert _wait(cli, lambda s: abs(s.velocity[1] - 321.0) < 1e-6)
    assert cli.set_ramp_mode("software") == "software"
    assert _wait(cli, lambda s: s.ramp_mode == "software")


def test_move_xy_over_wire(service_and_client):
    _brain, cli = service_and_client
    targets = cli.move_xy(50.0, 70.0)
    assert targets == [50.0, 70.0]
    assert _wait(cli, lambda s: abs(s.position[0] - 50.0) < 0.1 and abs(s.position[1] - 70.0) < 0.1)


def test_relative_zero_and_move_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.move_axis("X", 70.0)
    assert _wait(cli, lambda s: abs(s.position[0] - 70.0) < 0.1 and not s.moving[0])
    origins = cli.set_zero("X")
    assert abs(origins[0] - 70.0) < 0.1
    assert _wait(cli, lambda s: abs(s.relative[0]) < 0.1)
    cli.move_relative("X", 20.0)   # -> device 90
    assert _wait(cli, lambda s: abs(s.position[0] - 90.0) < 0.1 and abs(s.relative[0] - 20.0) < 0.1)
    cli.clear_zero("X")
    assert _wait(cli, lambda s: abs(s.relative[0] - s.position[0]) < 0.1)


def test_position_list_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.move_axis("Y", 25.0)
    assert _wait(cli, lambda s: not s.moving[1])
    cli.store_position(0, "wire")
    positions = cli.get_positions()
    assert positions[0]["used"] and positions[0]["name"] == "wire"
