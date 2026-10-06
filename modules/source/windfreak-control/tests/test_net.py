"""End-to-end over ZeroMQ: a service (simulated synthesizer) and a client on
loopback. Uses this module's own test ports (17020..17039) so it never collides
with a running service or with a sibling module's tests."""

import time

import pytest

from windfreak.config import Config
from windfreak.sim_system import build_sim_system
from windfreak.net.service import WindfreakService
from windfreak.net.client import WindfreakClient

CMD_PORT = 17020
PUB_PORT = 17021


def wait_for(pred, cli, timeout=3.0):
    t_end = time.monotonic() + timeout
    s = {}
    while time.monotonic() < t_end:
        s = cli.fresh_status()
        if s and pred(s):
            return s
        time.sleep(0.02)
    raise AssertionError(f"condition not reached; last status {s}")


@pytest.fixture
def service_and_client():
    cfg = Config()
    synth, backend = build_sim_system(cfg)
    svc = WindfreakService(synth, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                           status_hz=20.0)
    svc.start()
    cli = WindfreakClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                          timeout_ms=2000)
    time.sleep(0.2)
    yield svc, cli, backend
    cli.shutdown()
    svc.stop()
    time.sleep(0.1)


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["power_max_dBm"] == Config().limits.power_max_dBm
    assert info["channels"] == ["a", "b"]
    assert cli.get_config().hardware.port == Config().hardware.port


def test_commands_take_effect_per_channel(service_and_client):
    _, cli, backend = service_and_client
    assert cli.set_frequency("a", 2.5e9)["ok"]
    assert cli.set_power("b", -12.0)["ok"]
    assert cli.set_phase("b", 45.0)["ok"]
    assert cli.set_rf("a", True)["ok"]
    s = wait_for(lambda s: s["a_frequency_Hz"] == 2.5e9 and s["a_rf_on"]
                 and s["a_settled"] and s["b_phase_deg"] == 45.0, cli)
    assert s["b_power_dBm"] == -12.0 and s["b_rf_on"] is False
    assert backend.output_on(0) and not backend.output_on(1)
    assert "describe_rev" in s


def test_bad_requests_answer_ok_false(service_and_client):
    _, cli, _ = service_and_client
    r = cli._cmd({"cmd": "set_power", "channel": "c", "power_dBm": 0})
    assert r["ok"] is False and "channel" in r["error"]
    assert cli._cmd({"cmd": "set_frequency", "channel": "a"})["ok"] is False
    assert cli._cmd({"cmd": "nonsense"})["ok"] is False
    assert cli.set_reference("gps")["ok"] is False
    # the service is still alive afterwards
    assert cli._cmd({"cmd": "status"})["ok"] is True


def test_text_false_is_false(service_and_client):
    """A hand-typed "false" must not switch RF ON (bool("false") is True)."""
    _, cli, backend = service_and_client
    cli._cmd({"cmd": "set_rf", "channel": "a", "on": "false"})
    time.sleep(0.2)
    assert not backend.output_on(0)


def test_clamp_and_set_config_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    cli.cfg.limits.power_max_dBm = 5.0
    cli.apply_config()
    cli.set_power("a", 50.0)
    wait_for(lambda s: s["a_power_dBm"] == 5.0, cli)


def test_all_rf_off_and_shutdown_verb(service_and_client):
    svc, cli, backend = service_and_client
    cli.set_rf("a", True); cli.set_rf("b", True)
    wait_for(lambda s: s["a_rf_on"] and s["b_rf_on"], cli)
    assert cli.all_rf_off()["ok"]
    wait_for(lambda s: not s["a_rf_on"] and not s["b_rf_on"], cli)
    cli.set_rf("b", True)
    wait_for(lambda s: s["b_rf_on"], cli)
    r = cli._cmd({"cmd": "shutdown"})
    assert r["ok"] and r["stopping"]
    svc.stop()                                   # what serve_forever's finally does
    assert not backend.output_on(1)


def test_shutdown_keep_outputs_is_a_restart(service_and_client):
    svc, cli, backend = service_and_client
    cli.set_rf("a", True); cli.set_rf("b", True)
    wait_for(lambda s: s["a_rf_on"] and s["b_rf_on"], cli)
    n = len(backend.writes)
    r = cli._cmd({"cmd": "shutdown", "keep_outputs": True})
    assert r["ok"] and r["stopping"] and r["kept_outputs"] is True
    svc.stop()
    assert not [w for w in backend.writes[n:] if w[0] == "set_output"]
    assert backend.output_on(0) and backend.output_on(1) and not backend._open


def test_shutdown_keep_outputs_text_false_is_false(service_and_client):
    svc, cli, backend = service_and_client
    cli.set_rf("b", True)
    wait_for(lambda s: s["b_rf_on"], cli)
    r = cli._cmd({"cmd": "shutdown", "keep_outputs": "false"})   # gotcha #3
    assert r["kept_outputs"] is False
    svc.stop()
    assert not backend.output_on(1)


def test_pub_stream_delivers_status(service_and_client):
    _, cli, _ = service_and_client
    t_end = time.monotonic() + 2.0
    while time.monotonic() < t_end and not cli._latest:
        time.sleep(0.02)
    assert "a_locked" in cli.status()
