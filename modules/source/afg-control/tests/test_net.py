"""End-to-end over ZeroMQ: a service (simulated AFG1062) and a client on
loopback. Uses this module's own test ports (17620..17639) so it never collides
with a running service or with a sibling module's tests."""

import time

import pytest

from afg.config import Config
from afg.sim_system import build_sim_system
from afg.net.service import AfgService
from afg.net.client import AfgClient

CMD_PORT = 17620
PUB_PORT = 17621


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
    gen, backend = build_sim_system(cfg)
    svc = AfgService(gen, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                     status_hz=20.0)
    svc.start()
    cli = AfgClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                    timeout_ms=2000)
    time.sleep(0.2)
    yield svc, cli, backend
    cli.shutdown()
    svc.stop()
    time.sleep(0.1)


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["channels"] == ["ch1", "ch2"] and "sine" in info["waveforms"]
    assert info["envelope"]["ch1"]["amp_max_Vpp"] == 10.0
    assert cli.channels == ("ch1", "ch2")
    assert cli.envelope("ch1")["peak_max_V"] == 5.0
    assert cli.get_config().hardware.visa == Config().hardware.visa


def test_commands_take_effect_per_channel(service_and_client):
    _, cli, backend = service_and_client
    assert cli.set_waveform("ch2", "pulse")["ok"]
    assert cli.set_frequency("ch2", 2500.0)["ok"]
    assert cli.set_duty("ch2", 20.0)["ok"]
    assert cli.set_amplitude("ch1", 0.5)["ok"]
    assert cli.set_offset("ch1", 0.1)["ok"]
    assert cli.set_output("ch2", True)["ok"]
    s = wait_for(lambda s: s["ch2_frequency_Hz"] == 2500.0 and s["ch2_output"]
                 and s["ch2_settled"] and s["ch1_offset_V"] == 0.1, cli)
    assert s["ch2_duty_pct"] == 20.0 and backend.ch[1]["duty_pct"] == 20.0
    assert backend.ch[0]["amplitude_Vpp"] == 0.5
    assert "describe_rev" in s


def test_follow_over_the_wire(service_and_client):
    _, cli, backend = service_and_client
    assert cli.set_follow(True, 90.0)["ok"]
    assert cli.set_frequency("ch1", 300.0)["ok"]
    wait_for(lambda s: s["ch2_frequency_Hz"] == 300.0 and s["ch2_phase_deg"] == 90.0
             and s["ch2_settled"], cli)
    r = cli.set_frequency("ch2", 1.0)
    assert r["ok"] is False and "follows" in r["error"]
    assert cli.set_phase_offset(-45.0)["ok"]
    wait_for(lambda s: s["ch2_phase_deg"] == -45.0 and s["phase_offset_deg"] == -45.0, cli)


def test_bad_requests_answer_ok_false(service_and_client):
    _, cli, _ = service_and_client
    r = cli._cmd({"cmd": "set_amplitude", "channel": "ch3", "amplitude_Vpp": 1})
    assert r["ok"] is False and "channel" in r["error"]
    assert cli._cmd({"cmd": "set_frequency", "channel": "ch1"})["ok"] is False
    assert cli._cmd({"cmd": "nonsense"})["ok"] is False
    assert cli.set_waveform("ch1", "triangle")["ok"] is False
    assert cli.set_load("ch1", "0")["ok"] is False
    # the service is still alive afterwards
    assert cli._cmd({"cmd": "status"})["ok"] is True


def test_text_false_is_false(service_and_client):
    """A hand-typed "false" must not switch an output ON (bool("false") is True)."""
    _, cli, backend = service_and_client
    cli._cmd({"cmd": "set_output", "channel": "ch2", "on": "false"})
    time.sleep(0.2)
    assert not backend.ch[1]["output"]


def test_clamp_and_set_config_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    cli.cfg.limits_1.amplitude_max_Vpp = 0.8
    cli.apply_config()
    cli.set_amplitude("ch1", 5.0)
    wait_for(lambda s: s["ch1_amplitude_Vpp"] == 0.8, cli)


def test_outputs_off_and_shutdown_verb(service_and_client):
    svc, cli, backend = service_and_client
    cli.set_output("ch2", True)
    wait_for(lambda s: s["ch1_output"] and s["ch2_output"], cli)
    r = cli.outputs_off()
    assert r["ok"]
    wait_for(lambda s: s["op_id"] == r["op_id"] and s["all_off"], cli)
    cli.set_output("ch2", True)
    wait_for(lambda s: s["ch2_output"], cli)
    r = cli._cmd({"cmd": "shutdown"})
    assert r["ok"] and r["stopping"]
    svc.stop()                                   # what serve_forever's finally does
    assert not backend.ch[1]["output"]


def test_pub_stream_delivers_status(service_and_client):
    _, cli, _ = service_and_client
    t_end = time.monotonic() + 2.0
    while time.monotonic() < t_end and not cli._latest:
        time.sleep(0.02)
    assert "ch1_settled" in cli.status()
