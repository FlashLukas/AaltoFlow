"""Service <-> client round-trip on this module's test ports (section 9).

Ports 17280/17281 (17280..17299 are reserved for ddr25's tests) so this never
collides with a running service or with a sibling module's tests.
"""

import time

import pytest

from ddr25.config import Config
from ddr25.net.client import Ddr25Client
from ddr25.net.describe import build_manifest
from ddr25.net.service import Ddr25Service
from ddr25.sim_system import build_sim_system

CMD, PUB = 17280, 17281


@pytest.fixture()
def service_and_client():
    cfg = Config()
    cfg.motion.velocity = 720.0
    cfg.motion.acceleration = 3600.0
    brain, _ = build_sim_system(cfg)
    svc = Ddr25Service(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    svc.start()
    cli = Ddr25Client(host="127.0.0.1", cmd_port=CMD, pub_port=PUB, timeout_ms=3000)
    cli.start()
    time.sleep(0.3)  # let PUB warm up (slow joiner)
    try:
        yield brain, cli
    finally:
        cli.close()
        svc.stop()
        time.sleep(0.1)


def _wait(cli, pred, timeout=8.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred(cli.status()):
            return True
        time.sleep(0.02)
    return False


def test_info_and_config(service_and_client):
    _brain, cli = service_and_client
    info = cli.info()
    assert info["axes"] == ["angle"] and info["units"] == "deg"
    cfg = cli.get_config()
    assert set(cfg) == {"motion", "limits", "frame", "hardware", "ui"}


def test_home_then_move_over_the_wire(service_and_client):
    _brain, cli = service_and_client
    with pytest.raises(RuntimeError, match="not homed"):   # {"ok": false} -> raises
        cli.move_to(45.0)
    hid = cli.home()
    assert _wait(cli, lambda s: s.home_id == hid and not s.homing and s.homed)
    assert cli.move_to(45.0) == 45.0
    assert _wait(cli, lambda s: s.target_deg == 45.0 and not s.moving
                 and abs(s.angle_deg - 45.0) < 0.01)
    assert cli.move_by(-5.0) == pytest.approx(40.0)
    assert _wait(cli, lambda s: not s.moving and abs(s.angle_deg - 40.0) < 0.01)


def test_clamp_and_wrap_over_the_wire(service_and_client):
    _brain, cli = service_and_client
    cli.home()
    assert _wait(cli, lambda s: s.homed and not s.moving)
    assert cli.move_to(99999.0) == 720.0
    cli.stop(immediate=True)
    assert cli.set_wrap("shortest") == "shortest"
    assert _wait(cli, lambda s: s.wrap == "shortest")
    with pytest.raises(RuntimeError):
        cli.set_wrap("sideways")


def test_velocity_echo_and_set_config(service_and_client):
    brain, cli = service_and_client
    assert cli.set_velocity(90.0) == 90.0
    assert _wait(cli, lambda s: s.velocity == 90.0)
    cli.set_config({"motion": {"acceleration": 500.0}})
    assert _wait(cli, lambda s: s.acceleration == 500.0)
    assert brain.cfg.motion.acceleration == 500.0


def test_stored_angles_and_zero_over_the_wire(service_and_client, tmp_path):
    _brain, cli = service_and_client
    cli.move_by(25.0)
    assert _wait(cli, lambda s: not s.moving and abs(s.raw_deg - 25.0) < 0.01)
    assert cli.store_angle(0, "wire")["name"] == "wire"
    assert cli.get_angles()[0]["used"]
    path = str(tmp_path / "angles.json")
    cli.save_angles(path)
    cli.clear_angle(0)
    assert cli.load_angles(path)[0]["name"] == "wire"
    assert cli.set_zero() == pytest.approx(25.0, abs=0.01)
    assert _wait(cli, lambda s: abs(s.angle_deg) < 0.01)
    cli.clear_zero()
    assert _wait(cli, lambda s: abs(s.zero_deg) < 1e-9)


def test_stream_verbs(service_and_client):
    _brain, cli = service_and_client
    sid = cli.stream_start(50)
    cli.move_by(30.0)
    time.sleep(0.3)
    chunk = cli.stream_read()
    rest = cli.stream_stop()
    assert chunk["id"] == sid == rest["id"]
    assert len(chunk["values"]["angle"]) == len(chunk["t"]) > 0


def test_every_action_id_is_a_verb(service_and_client):
    """scan-core and the control screen send an action BY ITS ID."""
    brain, cli = service_and_client
    brain.store_angle(0, "x")
    for d in build_manifest(brain)["parameters"]:
        if d["kind"] != "action":
            continue
        args = {a["name"]: a["default"] for a in d.get("args", [])}
        try:
            reply = cli._rpc(cmd=d["id"], **args)
        except RuntimeError as exc:
            # refused for a REASON (goto before homing) is fine; unknown is not
            assert "unknown command" not in str(exc), d["id"]
            reply = None
        if reply is not None and d.get("wait", {}).get("target_key"):
            assert d["wait"]["target_key"] in reply, d["id"]
        cli.stop(immediate=True)


def test_unknown_command_is_an_error_not_a_crash(service_and_client):
    _brain, cli = service_and_client
    with pytest.raises(RuntimeError, match="unknown command"):
        cli._rpc(cmd="fly_to_the_moon")
    assert cli.info()["units"] == "deg"      # still alive
