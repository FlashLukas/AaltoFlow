"""End-to-end over ZeroMQ: a service (simulated 8648D) and a client on loopback.

Ports 17420..17439 are reserved for this module's tests, so they never collide
with a running service or with a sibling module's tests running in parallel.
"""

import time

import pytest

from hp8648.config import Config
from hp8648.sim_system import build_sim_system
from hp8648.net.service import Hp8648Service
from hp8648.net.client import Hp8648Client

CMD_PORT = 17420
PUB_PORT = 17421


def _until(cli, pred, timeout=3.0):
    end = time.monotonic() + timeout
    s = cli.status()
    while time.monotonic() < end:
        s = cli.status()
        if pred(s):
            return s
        time.sleep(0.03)
    return s


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.hardware.switch_settle_s = 0.01
    src, sim = build_sim_system(cfg)
    svc = Hp8648Service(src, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                        status_hz=20.0)
    svc.start()
    cli = Hp8648Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli, sim
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["model"] == "HP 8648D"
    assert info["power_ceiling_dBm"] == 13.0
    assert cli.get_config().hardware.visa_resource == "GPIB0::19::INSTR"


def test_commands_take_effect(service_and_client):
    _, cli, _ = service_and_client
    assert cli.set_frequency(2.5e9)["ok"]
    assert cli.set_power(-12.0)["ok"]
    assert cli.set_rf(True)["ok"]
    s = _until(cli, lambda s: s.frequency_Hz == 2.5e9 and s.rf_on and s.power_dBm == -12.0)
    assert (s.frequency_Hz, s.power_dBm, s.rf_on, s.connected) == (2.5e9, -12.0, True, True)


def test_clamp_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.set_frequency(3.0e9)
    cli.set_power(999.0)
    s = _until(cli, lambda s: s.power_dBm == 10.0)
    assert s.power_dBm == 10.0 and s.power_ceiling_dBm == 10.0


def test_bad_and_unknown_requests_are_refused(service_and_client):
    _, cli, _ = service_and_client
    r = cli._cmd({"cmd": "set_power"})
    assert r["ok"] is False and "bad" in r["error"]
    r = cli._cmd({"cmd": "set_phase", "phase_deg": 1.0})     # the 8648 has no phase
    assert r["ok"] is False and "unknown" in r["error"]
    assert cli._cmd({"cmd": "status"})["ok"]                  # the loop survived


def test_string_off_is_off(service_and_client):
    """A hand-typed "off" must not turn RF on (gotcha #3)."""
    _, cli, sim = service_and_client
    cli._cmd({"cmd": "set_rf", "on": "off"})
    time.sleep(0.2)
    assert sim.read_output() is False


def test_set_config_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    cli.cfg.limits.power_max_dBm = 5.0
    cli.apply_config()
    cli.set_frequency(1e9)
    cli.set_power(50.0)
    s = _until(cli, lambda s: s.power_dBm == 5.0)
    assert s.power_dBm == 5.0


def test_events_are_published(service_and_client):
    _, cli, _ = service_and_client
    got = []
    cli._on_event = lambda lvl, msg: got.append((lvl, msg))
    time.sleep(0.2)
    cli.set_power(999.0)
    end = time.monotonic() + 2.0
    while time.monotonic() < end and not any("clamped" in m for _, m in got):
        time.sleep(0.02)
    assert any(lvl == "warn" and "clamped" in m for lvl, m in got)


def test_shutdown_verb_stops_the_service_and_rf(service_and_client):
    svc, cli, sim = service_and_client
    cli.set_rf(True)
    _until(cli, lambda s: s.rf_on)
    r = cli._cmd({"cmd": "shutdown"})
    assert r["ok"] and r["stopping"]
    svc.stop()                      # what serve_forever's finally does
    assert sim.read_output() is False


def test_shutdown_verb_keep_outputs_is_a_restart(service_and_client):
    svc, cli, sim = service_and_client
    cli.set_rf(True)
    _until(cli, lambda s: s.rf_on)
    r = cli._cmd({"cmd": "shutdown", "keep_outputs": True})
    assert r["ok"] and r["stopping"] and r["kept_outputs"] is True
    calls = []
    sim.set_output = lambda on: calls.append(on)     # spy: any RF switch
    svc.stop()
    assert calls == [] and sim.read_output() is True and sim._open is False


def test_shutdown_verb_keep_outputs_text_false_is_false(service_and_client):
    svc, cli, sim = service_and_client
    cli.set_rf(True)
    _until(cli, lambda s: s.rf_on)
    r = cli._cmd({"cmd": "shutdown", "keep_outputs": "false"})   # gotcha #3
    assert r["kept_outputs"] is False
    svc.stop()
    assert sim.read_output() is False
