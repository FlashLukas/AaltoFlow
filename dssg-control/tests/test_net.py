"""End-to-end over ZeroMQ: a service (simulated SG12000L) and a client on
loopback. Uses this module's private test ports (17120..17139) so it never
collides with a running service or a sibling module's tests."""

import time

import pytest

from dssg.config import Config
from dssg.sim_system import build_sim_system
from dssg.net.service import DssgService
from dssg.net.client import DssgClient

CMD_PORT = 17120
PUB_PORT = 17121


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.hardware.poll_hz = 20.0
    synth, backend = build_sim_system(cfg)
    svc = DssgService(synth, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                      status_hz=20.0)
    svc.start()
    cli = DssgClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli, backend
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _wait(cli, pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = cli.status()
        if pred(s):
            return s
        time.sleep(0.05)
    return cli.status()


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["power_max_dBm"] == Config().limits.power_max_dBm
    assert info["freq_max_Hz"] == Config().sim.freq_max_Hz       # the unit's own range
    assert info["has_phase"] is True
    assert cli.get_config().hardware.tcp_port == 10001


def test_commands_take_effect(service_and_client):
    _, cli, _ = service_and_client
    cli.set_frequency(2.5e9)
    cli.set_power(-12.0)
    cli.set_phase(33.0)
    cli.set_reference("external")
    cli.set_rf(True)
    s = _wait(cli, lambda s: s.frequency_Hz == 2.5e9 and s.rf_on and s.reference == "external")
    assert s.frequency_Hz == 2.5e9
    assert s.power_dBm == -12.0
    assert s.phase_deg == 33.0
    assert s.rf_on is True
    assert s.connected is True
    assert s.usb_volts > 4.0


def test_clamp_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.set_power(999.0)
    s = _wait(cli, lambda s: s.power_dBm == Config().limits.power_max_dBm)
    assert s.power_dBm == Config().limits.power_max_dBm


def test_refused_command_raises_in_the_client(service_and_client):
    _, cli, _ = service_and_client
    with pytest.raises(RuntimeError):
        cli.set_reference("gps")
    r = cli._cmd({"cmd": "no_such_verb"})
    assert r["ok"] is False and "unknown" in r["error"]
    r = cli._cmd({"cmd": "set_power"})                  # missing argument
    assert r["ok"] is False


def test_set_config_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    cli.cfg.limits.power_max_dBm = 2.0
    cli.apply_config()
    cli.set_power(50.0)
    s = _wait(cli, lambda s: s.power_dBm == 2.0)
    assert s.power_dBm == 2.0
    assert s.power_max_dBm == 2.0


def test_shutdown_verb_turns_rf_off(service_and_client):
    svc, cli, backend = service_and_client
    cli.set_rf(True)
    _wait(cli, lambda s: s.rf_on)
    r = cli._cmd({"cmd": "shutdown"})
    assert r["ok"] is True
    svc.stop()                      # what serve_forever's finally: does
    assert backend.read_output() is False
