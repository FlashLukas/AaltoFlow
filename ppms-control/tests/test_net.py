"""End-to-end over ZeroMQ: a service (simulated DynaCool) and a client on
loopback. Uses non-default ports so it never collides with a running service."""

import time

import pytest

from ppms.config import Config
from ppms.sim_system import build_sim_system
from ppms.net.service import PpmsService
from ppms.net.client import PpmsClient

CMD_PORT = 15690
PUB_PORT = 15691


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.field.stable_time_s = 0.2
    cfg.hardware.poll_s = 0.05
    cryo, sim = build_sim_system(cfg, field_mT=0.0, temperature_K=300.0)
    svc = PpmsService(cryo, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                      status_hz=20.0)
    svc.start()
    cli = PpmsClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _until(cli, pred, timeout=8.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        s = cli.status()
        if pred(s):
            return s
        time.sleep(0.05)
    raise AssertionError("condition not met; last status: " + repr(vars(s)))


def test_info_and_config(service_and_client):
    _, cli = service_and_client
    info = cli.start()
    assert info["field_max_mT"] == Config().limits.field_max_mT
    assert info["simulated"] is True
    assert cli.get_config().hardware.flavor == "DYNACOOL"


def test_set_field_and_wait_like_a_scan(service_and_client):
    """The scan-core rule over the wire: first our setpoint, then the flag."""
    _, cli = service_and_client
    assert cli.set_field(11.0)["ok"]
    s = _until(cli, lambda s: s.setpoint_field_mT == 11.0 and s.field_stable)
    assert abs(s.measured_field_mT - 11.0) <= 0.1
    assert s.field_status == "Holding (driven)"


def test_settings_echo_and_bad_values_are_refused(service_and_client):
    _, cli = service_and_client
    cli.set_field_rate(3.5)
    cli.set_temperature_approach("no_overshoot")
    s = _until(cli, lambda s: s.field_rate_mT_per_s == 3.5
               and s.temperature_approach == "no_overshoot")
    r = cli.set_field_approach("persistent")
    assert r["ok"] is False and "approach" in r["error"]


def test_clamp_over_wire(service_and_client):
    _, cli = service_and_client
    cli.set_temperature(1e4)
    s = _until(cli, lambda s: s.setpoint_temperature_K == Config().limits.temperature_max_K)
    assert s.temperature_stable is False


def test_set_config_over_wire_does_not_move_anything(service_and_client):
    _, cli = service_and_client
    cli.start()
    before = cli.status().setpoint_field_mT
    cli.cfg.limits.field_rate_max_mT_per_s = 2.0
    cli.apply_config()
    cli.get_config()
    assert cli.cfg.field.rate_mT_per_s == 2.0          # re-clamped by the service
    assert cli.status().setpoint_field_mT == before


def test_status_json_has_no_nan(service_and_client):
    """JSON has no NaN; a missing number must travel as null."""
    _, cli = service_and_client
    r = cli._cmd({"cmd": "status"})
    assert r["ok"]
    import json
    json.dumps(r, allow_nan=False)
