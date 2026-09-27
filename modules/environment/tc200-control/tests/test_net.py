"""End-to-end over ZeroMQ: a service (simulated TC200) and a client on
loopback. Uses ports 17360..17379 only (this module's test range) so it never
collides with a running service or a sibling module's tests."""

import json
import time

import pytest

from tc200.config import Config
from tc200.sim_system import build_sim_system
from tc200.net.service import Tc200Service
from tc200.net.client import Tc200Client

CMD_PORT = 17370
PUB_PORT = 17371


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.temperature.stable_time_s = 0.3
    cfg.temperature.tolerance_C = 0.3
    cfg.hardware.poll_s = 0.05
    # a block already sitting at its setpoint, so "reached" comes quickly in
    # wall-clock time (the real plant takes minutes)
    heater, sim = build_sim_system(cfg, temperature_C=30.0, setpoint_C=30.0, enabled=True,
                                   seed=2)
    svc = Tc200Service(heater, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                       status_hz=20.0)
    svc.start()
    cli = Tc200Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli, sim
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _until(cli, pred, timeout=8.0):
    t0 = time.monotonic()
    s = None
    while time.monotonic() - t0 < timeout:
        s = cli.status()
        if pred(s):
            return s
        time.sleep(0.05)
    raise AssertionError("condition not met; last status: " + repr(vars(s)))


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["simulated"] is True
    assert info["temperature_min_C"] == 20.0
    assert info["sensor"] == "ptc100" and info["expected_sensor"] == "ptc100"
    assert cli.get_config().hardware.baud == 115200


def test_set_temperature_and_wait_like_a_scan(service_and_client):
    """The scan-core rule over the wire: first our setpoint, then the flag."""
    _, cli, _ = service_and_client
    assert cli.set_temperature(30.1)["ok"]
    s = _until(cli, lambda s: s.setpoint_C == 30.1 and s.temperature_stable)
    assert abs(s.temperature_C - 30.1) <= 0.3


def test_enable_disable_and_heater_off_action(service_and_client):
    _, cli, sim = service_and_client
    assert cli.set_enabled(False)["ok"]
    _until(cli, lambda s: s.enabled is False)
    assert sim.enabled is False
    assert cli.set_enabled(True)["ok"]
    _until(cli, lambda s: s.enabled is True)
    r = cli._cmd({"cmd": "heater_off"})
    assert r["ok"] and sim.enabled is False


def test_refusals_come_back_as_errors(service_and_client):
    _, cli, sim = service_and_client
    r = cli.set_sensor("ptc1000")            # heater is on: refused
    assert r["ok"] is False and "off" in r["error"]
    r = cli._cmd({"cmd": "set_temperature"})
    assert r["ok"] is False and "bad" in r["error"]
    r = cli._cmd({"cmd": "no_such_verb"})
    assert r["ok"] is False and "unknown" in r["error"]


def test_clamp_and_settings_over_wire(service_and_client):
    _, cli, sim = service_and_client
    cli.set_temperature(1e4)
    s = _until(cli, lambda s: s.setpoint_C == s.temperature_max_C)
    assert s.temperature_stable is False
    cli.set_pid(100, 2, 0)
    cli.set_pmax(6.5)
    cli.set_tmax(80.0)
    s = _until(cli, lambda s: (s.p_gain, s.i_gain, s.pmax_W, s.tmax_C) == (100, 2, 6.5, 80.0))
    assert s.temperature_max_C == 75.0
    assert (sim.p, sim.i, sim.pmax, sim.tmax) == (100, 2, 6.5, 80.0)


def test_set_config_over_wire_pushes_only_the_device_change(service_and_client):
    _, cli, sim = service_and_client
    cli.start()
    before = cli.status().setpoint_C
    cli.cfg.device.p_gain = 77
    cli.apply_config()
    cli.get_config()
    assert cli.cfg.device.p_gain == 77 and sim.p == 77
    assert cli.status().setpoint_C == before and sim.enabled is True


def test_status_json_has_no_nan(service_and_client):
    """JSON has no NaN; a missing number must travel as null."""
    _, cli, _ = service_and_client
    r = cli._cmd({"cmd": "status"})
    assert r["ok"]
    json.dumps(r, allow_nan=False)


def test_shutdown_verb_switches_heater_off():
    """The launcher's clean stop: the service exits its loop and the brain
    switches the heater off (disable_on_shutdown)."""
    import threading
    cfg = Config()
    cfg.hardware.poll_s = 0.05
    heater, sim = build_sim_system(cfg, enabled=True)
    svc = Tc200Service(heater, host="127.0.0.1", cmd_port=17374, pub_port=17375)
    t = threading.Thread(target=svc.serve_forever, daemon=True)
    t.start()
    time.sleep(0.4)
    cli = Tc200Client(host="127.0.0.1", cmd_port=17374, pub_port=17375, timeout_ms=2000)
    try:
        r = cli._cmd({"cmd": "shutdown"})
        assert r["ok"] and r.get("stopping")
        t.join(timeout=5.0)
        assert not t.is_alive()
        assert sim.enabled is False
    finally:
        cli.shutdown()
