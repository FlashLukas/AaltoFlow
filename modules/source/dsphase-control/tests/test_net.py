"""End-to-end over ZeroMQ: a service (simulated phase shifter) and a client on
loopback. Uses this module's private test ports (17100..17119) so it never
collides with a running service or a sibling module's tests."""

import time

import pytest

from dsphase.config import Config
from dsphase.sim_system import build_sim_system
from dsphase.net.service import DsphaseService
from dsphase.net.client import DsphaseClient

CMD_PORT = 17102
PUB_PORT = 17103


@pytest.fixture
def service_and_client():
    cfg = Config()
    brain, backend = build_sim_system(cfg)
    svc = DsphaseService(brain, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                         status_hz=20.0)
    svc.start()
    cli = DsphaseClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli, backend
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _wait(cli, pred, timeout=2.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        s = cli.status()
        if pred(s):
            return s
        time.sleep(0.03)
    return cli.status()


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["phase_step_deg"] == 0.5
    assert info["att_max_dB"] == Config().limits.att_max_dB
    assert cli.get_config().hardware.baud == 115200


def test_commands_take_effect(service_and_client):
    _, cli, backend = service_and_client
    cli.set_phase(270.0)
    cli.set_attenuation(4.3)
    cli.set_frequency(5800.0)
    cli.set_output(True)
    s = _wait(cli, lambda s: s.phase_deg == 270.0 and s.output_on
              and s.attenuation_dB == 4.25 and s.frequency_MHz == 5800.0)
    assert s.phase_deg == 270.0
    assert s.phase_device_deg == -90.0
    assert s.attenuation_dB == 4.25
    assert s.frequency_MHz == 5800.0
    assert s.output_on is True
    assert s.connected is True
    assert backend.read_phase() == -90.0


def test_clamp_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.set_attenuation(999.0)
    s = _wait(cli, lambda s: s.attenuation_dB == Config().limits.att_max_dB)
    assert s.attenuation_dB == Config().limits.att_max_dB


def test_set_config_over_wire_changes_the_step(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    rev0 = cli.describe()["revision"]
    cli.cfg.device.phase_step_deg = 5.625
    cli.apply_config()
    cli.set_phase(10.0)
    s = _wait(cli, lambda s: s.phase_deg == 11.25 and s.describe_rev != rev0)
    assert s.phase_deg == 11.25
    assert s.describe_rev != rev0
    assert cli.describe()["revision"] == s.describe_rev


def test_bad_and_unknown_commands_reply_with_errors(service_and_client):
    _, cli, _ = service_and_client
    r = cli._cmd({"cmd": "set_phase"})
    assert r["ok"] is False and "bad set_phase" in r["error"]
    r = cli._cmd({"cmd": "fly_to_the_moon"})
    assert r["ok"] is False and "unknown" in r["error"]
    # the loop survived
    assert cli._cmd({"cmd": "status"})["ok"] is True


def test_string_off_means_off(service_and_client):
    _, cli, _ = service_and_client
    cli.set_output(True)
    _wait(cli, lambda s: s.output_on)
    cli._cmd({"cmd": "set_output", "on": "off"})
    assert _wait(cli, lambda s: not s.output_on).output_on is False


def test_shutdown_verb_stops_and_switches_output_off():
    cfg = Config()
    brain, backend = build_sim_system(cfg)
    svc = DsphaseService(brain, host="127.0.0.1", cmd_port=17104, pub_port=17105)
    svc.start()
    cli = DsphaseClient(host="127.0.0.1", cmd_port=17104, pub_port=17105, timeout_ms=2000)
    try:
        cli.set_output(True)
        r = cli._cmd({"cmd": "shutdown"})
        assert r == {"ok": True, "stopping": True, "kept_outputs": False}
        assert svc._stop.is_set()
    finally:
        cli.shutdown()
        svc.stop()
    assert backend._output is False


def test_shutdown_verb_keep_outputs_is_a_restart():
    # shutdown{keep_outputs: true}: everything closed, no output command sent
    brain, backend = build_sim_system(Config())
    svc = DsphaseService(brain, host="127.0.0.1", cmd_port=17104, pub_port=17105)
    svc.start()
    cli = DsphaseClient(host="127.0.0.1", cmd_port=17104, pub_port=17105, timeout_ms=2000)
    try:
        cli.set_output(True)
        n = len(backend.write_log)
        r = cli._cmd({"cmd": "shutdown", "keep_outputs": True})
        assert r == {"ok": True, "stopping": True, "kept_outputs": True}
    finally:
        cli.shutdown()
        svc.stop()
    assert backend.write_log[n:] == []
    assert backend._output is True and backend._open is False


def test_shutdown_verb_keep_outputs_text_false_is_false():
    brain, backend = build_sim_system(Config())
    svc = DsphaseService(brain, host="127.0.0.1", cmd_port=17104, pub_port=17105)
    svc.start()
    cli = DsphaseClient(host="127.0.0.1", cmd_port=17104, pub_port=17105, timeout_ms=2000)
    try:
        cli.set_output(True)
        r = cli._cmd({"cmd": "shutdown", "keep_outputs": "false"})   # gotcha #3
        assert r["kept_outputs"] is False
    finally:
        cli.shutdown()
        svc.stop()
    assert backend._output is False
