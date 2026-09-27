"""End-to-end over ZeroMQ: a service (simulated 2450) and a client on loopback.
Uses this module's private test ports (17040..17059) so it never collides with
a running service or with a sibling module's tests."""

import time

import pytest

from k2450.config import Config
from k2450.sim_system import build_sim_system
from k2450.net.service import K2450Service
from k2450.net.client import K2450Client

CMD_PORT = 17040
PUB_PORT = 17041


@pytest.fixture
def service_and_client():
    cfg = Config()
    smu, sim = build_sim_system(cfg, seed=0)
    # fast readings: start-up ADOPTS the instrument's NPLC (a cfg value would
    # not be pushed), so set it on the pretend instrument itself
    sim.preset(nplc=0.1)
    svc = K2450Service(smu, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                       status_hz=20.0)
    svc.start()
    cli = K2450Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=3000)
    time.sleep(0.3)                       # let PUB/SUB connect so status frames flow
    yield svc, cli, sim
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _wait(cli, pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        s = cli.status()
        if pred(s):
            return s
        time.sleep(0.03)
    raise AssertionError("condition not reached")


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["voltage_max_V"] == Config().limits.voltage_max_V
    assert info["box_current_A"] == Config().limits.box_current_A
    assert cli.get_config().hardware.visa_resource == Config().hardware.visa_resource


def test_iv_point_over_the_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    cli.set_current_limit(0.01)
    cli.set_output(True)
    cli.set_voltage_blocking(2.0)
    s = cli.acquire_blocking(timeout_s=10)
    assert s["current_A"] == pytest.approx(2.0 / 1000.5, rel=1e-3)
    assert s["resistance_ohm"] == pytest.approx(1000.5, rel=1e-3)
    st = _wait(cli, lambda s: s.output and s.settled)
    assert st.connected is True


def test_current_in_uA_on_the_wire(service_and_client):
    """The describe control sends uA; the verb must accept it."""
    svc, cli, _ = service_and_client
    cli.set_source_function("current")
    r = cli._cmd({"cmd": "set_current", "current_uA": 12.5})
    assert r["ok"]
    s = _wait(cli, lambda s: s.source_current_set_uA == pytest.approx(12.5))
    assert s.source_current_set_A == pytest.approx(12.5e-6)


def test_clamp_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.set_voltage(999.0)
    _wait(cli, lambda s: s.source_voltage_set_V == Config().limits.voltage_max_V)


def test_acquire_refused_when_output_off(service_and_client):
    _, cli, _ = service_and_client
    with pytest.raises(ValueError, match="OFF"):
        cli.acquire()


def test_set_config_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    cli.cfg.limits.voltage_max_V = 5.0
    cli.apply_config()
    cli.set_voltage(50.0)
    _wait(cli, lambda s: s.source_voltage_set_V == 5.0)


def test_shutdown_verb_turns_output_off(service_and_client):
    svc, cli, sim = service_and_client
    cli.set_output(True)
    _wait(cli, lambda s: s.output)
    r = cli._cmd({"cmd": "shutdown"})
    assert r["ok"] and r["stopping"]
    svc.stop()                              # what serve_forever's finally does
    assert sim.get_output() is False


def test_unknown_and_bad_requests_answer_errors(service_and_client):
    _, cli, _ = service_and_client
    assert cli._cmd({"cmd": "fly_to_the_moon"})["ok"] is False
    r = cli._cmd({"cmd": "set_voltage"})
    assert r["ok"] is False and "bad" in r["error"]
