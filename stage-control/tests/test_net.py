"""Service <-> client round-trip on NON-default ports (§9).

Non-default ports (15690/15691) so this never collides with a real service.
Covers the two remote-control requirements explicitly: read/set individual
motor positions, and read/set the transform matrix, over the wire.
"""

import time

import pytest

from stage.config import Config
from stage.net.client import StageClient
from stage.net.service import StageService
from stage.sim_system import build_sim_system

CMD, PUB = 15690, 15691


@pytest.fixture()
def service_and_client():
    brain, _ = build_sim_system(Config())
    svc = StageService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = StageClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
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
    assert info["axes"] == ["X", "Y", "Z"]
    cfg = cli.get_config()
    assert "limits" in cfg and "transform" in cfg


def test_set_and_read_individual_motor(service_and_client):
    _brain, cli = service_and_client
    cli.set_velocity(0, 20)
    target = cli.move_axis("X", 6.0)   # accepts axis by name
    assert target == 6.0
    assert _wait(cli, lambda s: abs(s.position[0] - 6.0) < 1e-4 and not s.moving[0])


def test_move_clamped_over_wire(service_and_client):
    _brain, cli = service_and_client
    target = cli.move_axis(0, 999)     # max_x default 25
    assert target == 25.0


def test_read_set_transform_matrix(service_and_client):
    _brain, cli = service_and_client
    assert cli.get_matrix() == [1.0, 0.0, 0.0, 1.0]
    out = cli.set_matrix(0, -1, 1, 0)
    assert out == [0.0, -1.0, 1.0, 0.0]
    assert cli.get_matrix() == [0.0, -1.0, 1.0, 0.0]
    # and it shows up in the published status
    assert _wait(cli, lambda s: s.matrix == [0.0, -1.0, 1.0, 0.0])


def test_singular_matrix_rejected_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_matrix(0, -1, 1, 0)  # valid
    with pytest.raises(RuntimeError):  # {"ok": false} -> client raises
        cli.set_matrix(1, 1, 1, 1)   # singular
    # the valid matrix survived the rejected attempt
    assert cli.get_matrix() == [0.0, -1.0, 1.0, 0.0]


def test_relative_zero_and_move_over_wire(service_and_client):
    _brain, cli = service_and_client
    cli.set_velocity(0, 50)
    cli.move_axis("X", 7.0)
    assert _wait(cli, lambda s: abs(s.position[0] - 7.0) < 1e-4 and not s.moving[0])

    origins = cli.set_zero("X")
    assert abs(origins[0] - 7.0) < 1e-4
    assert _wait(cli, lambda s: abs(s.relative[0]) < 1e-4)

    cli.move_relative("X", 3.0)  # -> device 10
    assert _wait(cli, lambda s: abs(s.position[0] - 10.0) < 1e-4 and abs(s.relative[0] - 3.0) < 1e-4)

    cli.clear_zero("X")
    assert _wait(cli, lambda s: abs(s.relative[0] - s.position[0]) < 1e-4)


def test_position_list_over_wire(service_and_client, tmp_path):
    _brain, cli = service_and_client
    cli.set_velocity(1, 20)
    cli.move_axis("Y", 2.0)
    assert _wait(cli, lambda s: not s.moving[1])
    cli.store_position(0, "wire")
    positions = cli.get_positions()
    assert positions[0]["used"] and positions[0]["name"] == "wire"
