"""End-to-end over ZeroMQ: a service (simulated amplifier) and a client on
loopback. Ports 17140..17149 are this module's test range, so the tests never
collide with a running service or with a sibling module's tests."""

import time

import pytest

pytest.importorskip("zmq")

from dsamp.config import Config
from dsamp.net.client import DsampClient
from dsamp.net.service import DsampService
from dsamp.sim_system import build_sim_system

CMD_PORT = 17140
PUB_PORT = 17141


@pytest.fixture
def service_and_client():
    cfg = Config()
    amp, backend = build_sim_system(cfg)
    svc = DsampService(amp, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                       status_hz=20.0)
    svc.start()
    cli = DsampClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli, backend
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _wait(cli, pred, timeout=3.0):
    """Poll the client's status until pred(status) -- a reply means accepted,
    not done, so the effect shows after the brain's next hardware poll."""
    t0 = time.monotonic()
    s = cli.status()
    while not pred(s) and time.monotonic() - t0 < timeout:
        time.sleep(0.05)
        s = cli.status()
    return s


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["gain_max_dB"] == Config().limits.gain_max_dB
    assert info["gain_step_dB"] == 0.5
    assert "SIMULATED" in info["idn"]
    assert cli.get_config().hardware.baud == 115200


def test_commands_take_effect(service_and_client):
    _, cli, _ = service_and_client
    cli.set_frequency(2.5e9)
    cli.set_input_power(-15.0)
    cli.set_gain(6.0)
    cli.set_amp(True)
    s = _wait(cli, lambda s: s.gain_dB == 6.0 and s.amp_on and s.frequency_Hz == 2.5e9)
    assert s.gain_dB == 6.0
    assert s.amp_on is True
    assert s.input_dBm == -15.0
    assert s.connected is True
    assert s.est_output_dBm == pytest.approx(-15.0 + s.est_gain_dB, abs=0.1)


def test_amp_off_verb(service_and_client):
    _, cli, backend = service_and_client
    cli.set_amp(True)
    _wait(cli, lambda s: s.amp_on)
    cli.amp_off()
    assert _wait(cli, lambda s: not s.amp_on).amp_on is False
    assert backend.read_output() is False


def test_string_false_does_not_switch_on(service_and_client):
    """bool("false") is True in Python; the service must parse, not cast."""
    _, cli, backend = service_and_client
    r = cli._cmd({"cmd": "set_amp", "on": "false"})
    assert r["ok"] is True
    assert backend.read_output() is False


def test_clamp_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.set_gain(99.0)
    ceiling = Config().limits.gain_max_dB
    assert _wait(cli, lambda s: s.gain_dB == ceiling).gain_dB == ceiling


def test_set_config_moves_the_envelope(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    cli.cfg.limits.gain_max_dB = 4.0
    cli.apply_config()
    cli.set_gain(20.0)
    s = _wait(cli, lambda s: s.gain_dB == 4.0)
    assert s.gain_dB == 4.0
    assert cli.gain_range() == (0.0, 4.0)


def test_bad_set_config_is_refused_and_telemetry_keeps_flowing(service_and_client):
    """A string where a number belongs used to be stored as-is and then break
    every status frame. Now it is refused and the service carries on."""
    _, cli, _ = service_and_client
    r = cli._cmd({"cmd": "set_config", "config": {"limits": {"gain_max_dB": "lots"}}})
    assert r["ok"] is False and "gain_max_dB" in r["error"]
    r = cli._cmd({"cmd": "set_config", "config": {"limits": {"gain_max_dB": "6"}}})
    assert r["ok"] is True
    cli.set_gain(20.0)
    assert _wait(cli, lambda s: s.gain_dB == 6.0).gain_max_dB == 6.0


def test_bad_and_unknown_requests_reply_with_an_error(service_and_client):
    _, cli, _ = service_and_client
    r = cli._cmd({"cmd": "set_gain"})
    assert r["ok"] is False and "set_gain" in r["error"]
    r = cli._cmd({"cmd": "fly_to_the_moon"})
    assert r["ok"] is False
    # the loop survived
    assert cli._cmd({"cmd": "status"})["ok"] is True


def test_shutdown_verb_switches_the_stage_off():
    cfg = Config()
    amp, backend = build_sim_system(cfg)
    svc = DsampService(amp, host="127.0.0.1", cmd_port=17142, pub_port=17143)
    svc.start()
    cli = DsampClient(host="127.0.0.1", cmd_port=17142, pub_port=17143, timeout_ms=2000)
    try:
        cli.set_amp(True)
        assert backend.read_output() is True
        r = cli._cmd({"cmd": "shutdown"})
        assert r["ok"] and r["stopping"]
    finally:
        cli.shutdown()
        svc.stop()                  # what serve_forever's finally does
    assert backend._output is False
