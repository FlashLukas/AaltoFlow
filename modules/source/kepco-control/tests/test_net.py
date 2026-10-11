"""End-to-end over ZeroMQ: a service (simulated BOP + coil) and a client on
loopback. Uses this module's own test ports (17000..17019) so it never
collides with a running service or a sibling module's tests."""

import time

import pytest

from kepco.config import Config
from kepco.sim_system import build_sim_system
from kepco.net.service import KepcoService
from kepco.net.client import KepcoClient

CMD_PORT = 17000
PUB_PORT = 17001


def wait_for(pred, timeout=8.0):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if pred():
            return True
        time.sleep(0.03)
    return False


@pytest.fixture
def svc_cli():
    cfg = Config()
    cfg.ramp.rate_A_per_s = 5.0                   # quick ramps for the tests
    supply, sim = build_sim_system(cfg, seed=0)
    svc = KepcoService(supply, host="127.0.0.1", cmd_port=CMD_PORT,
                       pub_port=PUB_PORT, status_hz=20.0)
    svc.start()
    cli = KepcoClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                      timeout_ms=2000)
    time.sleep(0.3)                               # let PUB/SUB connect
    yield svc, cli, sim
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def test_info_and_config(svc_cli):
    _, cli, _ = svc_cli
    info = cli.start()
    assert info["current_max_A"] == Config().limits.current_max_A
    assert info["mode"] == "current"
    assert cli.get_config().hardware.visa == "GPIB0::6::INSTR"


def test_ramp_up_measure_and_ramp_down(svc_cli):
    _, cli, sim = svc_cli
    cli.set_current(1.0)
    cli.set_output(True)
    assert wait_for(lambda: cli.status().output and cli.status().current_set_A == 1.0
                    and not cli.status().ramping)
    n = cli.acquire()
    assert wait_for(lambda: cli.status().acq_id == n and not cli.status().acquiring)
    sample = cli.status().sample
    assert sample["acq_id"] == n
    assert sample["current_A"] == pytest.approx(1.0, abs=0.01)
    cli.set_output(False)
    assert wait_for(lambda: not cli.status().output)
    assert sim.output_on is False


def test_refusals_come_back_as_value_errors(svc_cli):
    _, cli, _ = svc_cli
    with pytest.raises(ValueError, match="current mode"):
        cli.set_voltage(1.0)
    cli.set_output(True)
    with pytest.raises(ValueError, match="output off"):
        cli.set_mode("voltage")
    r = cli._cmd({"cmd": "no_such_verb"})
    assert r["ok"] is False and "unknown" in r["error"]


def test_clamp_over_wire(svc_cli):
    _, cli, _ = svc_cli
    cli.set_current(999.0)
    assert wait_for(lambda: cli.status().current_set_A == Config().limits.current_max_A)


def test_set_config_over_wire(svc_cli):
    _, cli, _ = svc_cli
    cli.start()
    cli.cfg.limits.current_max_A = 2.0
    cli.cfg.ramp.enabled = False
    cli.apply_config()
    cli.set_current(5.0)
    assert wait_for(lambda: cli.status().current_set_A == 2.0)
    assert cli.get_config().ramp.enabled is False
    assert cli.status().ramp_enabled is False


def test_shutdown_verb_ramps_down_and_stops():
    cfg = Config()
    cfg.ramp.rate_A_per_s = 5.0
    supply, sim = build_sim_system(cfg, seed=0)
    svc = KepcoService(supply, host="127.0.0.1", cmd_port=CMD_PORT + 2,
                       pub_port=PUB_PORT + 2, status_hz=20.0)
    svc.start()
    cli = KepcoClient(host="127.0.0.1", cmd_port=CMD_PORT + 2, pub_port=PUB_PORT + 2)
    try:
        cli.set_current(2.0)
        cli.set_output(True)
        assert wait_for(lambda: not cli.status().ramping and cli.status().output)
        assert cli._cmd({"cmd": "shutdown"})["ok"] is True
        svc.stop()                                # what serve_forever's finally does
        assert sim.output_on is False
        assert supply.status().connected is False
    finally:
        cli.shutdown()


def test_shutdown_verb_keep_outputs_is_refused():
    cfg = Config()
    cfg.ramp.rate_A_per_s = 5.0
    supply, sim = build_sim_system(cfg, seed=0)
    svc = KepcoService(supply, host="127.0.0.1", cmd_port=CMD_PORT + 2,
                       pub_port=PUB_PORT + 2, status_hz=20.0)
    svc.start()
    cli = KepcoClient(host="127.0.0.1", cmd_port=CMD_PORT + 2, pub_port=PUB_PORT + 2)
    try:
        cli.set_current(2.0)
        cli.set_output(True)
        assert wait_for(lambda: not cli.status().ramping and cli.status().output)
        # Lukas 2026-10-11: the BOP drives magnets -- a restart ramps down too
        r = cli._cmd({"cmd": "shutdown", "keep_outputs": True})
        assert r["ok"] is True and r["kept_outputs"] is False
        svc.stop()
        assert sim.output_on is False
        assert supply.status().connected is False
    finally:
        cli.shutdown()


def test_shutdown_verb_keep_outputs_text_false_is_false():
    cfg = Config()
    cfg.ramp.rate_A_per_s = 5.0
    supply, sim = build_sim_system(cfg, seed=0)
    svc = KepcoService(supply, host="127.0.0.1", cmd_port=CMD_PORT + 2,
                       pub_port=PUB_PORT + 2, status_hz=20.0)
    svc.start()
    cli = KepcoClient(host="127.0.0.1", cmd_port=CMD_PORT + 2, pub_port=PUB_PORT + 2)
    try:
        cli.set_current(2.0)
        cli.set_output(True)
        assert wait_for(lambda: not cli.status().ramping and cli.status().output)
        r = cli._cmd({"cmd": "shutdown", "keep_outputs": "false"})   # gotcha #3
        assert r["kept_outputs"] is False
        svc.stop()
        assert sim.output_on is False
    finally:
        cli.shutdown()
