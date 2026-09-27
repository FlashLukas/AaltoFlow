"""Service <-> client round-trip on this module's private test ports.

Ports 17300..17319 belong to elliptec's tests only, so they collide neither
with a running service (5607/5608) nor with the sibling modules' tests.
"""

import time

import pytest
import zmq

from elliptec.config import Config
from elliptec.net.client import ElliptecClient
from elliptec.net.service import ElliptecService
from elliptec.sim_system import build_sim_system

CMD, PUB = 17300, 17301


@pytest.fixture()
def service_and_client():
    cfg = Config()
    cfg.axes.addresses = "0,A"
    cfg.axes.names = "HWP,POL"
    cfg.sim.max_speed_deg_s = 900.0
    brain, _ = build_sim_system(cfg)
    svc = ElliptecService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = ElliptecClient(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    time.sleep(0.2)  # let PUB warm up (ZeroMQ SUB is a slow joiner)
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


def test_info_and_axis_list(service_and_client):
    _brain, cli = service_and_client
    info = cli.info()
    assert info["addresses"] == ["0", "A"] and info["names"] == ["HWP", "POL"]
    assert info["devices"][0]["pulses_per_rev"] == 143360
    assert cli.addresses == ["0", "A"] and cli.n == 2
    assert "limits" in cli.get_config()


def test_move_over_the_wire(service_and_client):
    _brain, cli = service_and_client
    r = cli.move_abs(1, 222.0)
    assert r["target"] == 222.0 and r["move_id"] >= 1
    assert _wait(cli, lambda s: not s.moving[1] and abs(s.angle_deg[1] - 222.0) < 0.01)
    cli.move_rel("@A", -22.0)                # an axis named by its bus address
    assert _wait(cli, lambda s: not s.moving[1] and abs(s.angle_deg[1] - 200.0) < 0.01)


def test_velocity_clamped_over_the_wire(service_and_client):
    _brain, cli = service_and_client
    assert cli.set_velocity(0, 1000) == 100
    assert _wait(cli, lambda s: s.velocity_pct[0] == 100)


def test_home_zero_and_stop(service_and_client):
    _brain, cli = service_and_client
    cli.home(0)
    assert _wait(cli, lambda s: s.homed[0] and not s.moving[0])
    cli.move_abs(0, 30.0)
    assert _wait(cli, lambda s: not s.moving[0] and abs(s.angle_deg[0] - 30.0) < 0.01)
    assert abs(cli.set_zero(0) - 30.0) < 0.01
    assert _wait(cli, lambda s: s.angle_deg[0] < 0.01 or s.angle_deg[0] > 359.99)
    cli.clear_zero(0)
    cli.stop_all()


def test_errors_are_ok_false(service_and_client):
    _brain, cli = service_and_client
    with pytest.raises(RuntimeError):
        cli.move_abs(5, 10.0)                 # no such axis
    with pytest.raises(RuntimeError):
        cli._rpc(cmd="no_such_verb")
    # the service survived both
    assert cli.info()["addresses"] == ["0", "A"]


def test_action_verbs_by_address(service_and_client):
    """describe's per-axis actions are sent as bare verbs: home_a, set_zero_0."""
    _brain, cli = service_and_client
    r = cli._rpc(cmd="home_a")
    assert "move_id" in r
    assert _wait(cli, lambda s: s.move_id[1] == r["move_id"] and not s.moving[1])
    assert cli.status().homed[1]
    assert "offset_deg" in cli._rpc(cmd="set_zero_0")


def test_set_config_roundtrip(service_and_client):
    brain, cli = service_and_client
    cfg = cli.get_config()
    cfg["limits"]["max_angle_deg"] = 180.0
    cli.set_config(cfg)
    assert brain.cfg.limits.max_angle_deg == 180.0
    assert cli.move_abs(0, 270.0)["target"] == 180.0


def test_shutdown_verb_stops_the_service():
    brain, _ = build_sim_system(Config())
    svc = ElliptecService(brain, host="127.0.0.1", cmd_port=17302, pub_port=17303)
    svc.start()
    ctx = zmq.Context.instance()
    s = ctx.socket(zmq.REQ)
    s.setsockopt(zmq.RCVTIMEO, 3000)
    s.setsockopt(zmq.LINGER, 0)
    s.connect("tcp://127.0.0.1:17302")
    try:
        s.send_json({"cmd": "shutdown"})
        assert s.recv_json()["ok"]
        assert svc._stop.wait(2.0)
    finally:
        s.close(0)
        svc.stop()
    assert not brain.status().connected
