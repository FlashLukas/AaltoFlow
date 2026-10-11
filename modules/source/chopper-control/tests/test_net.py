"""End-to-end over ZeroMQ: a service (simulated chopper) and a client on
loopback. Uses this module's own test ports (17320..17339), never the default
ones, so it cannot collide with a running service or a sibling module's tests."""

import time

import pytest

from chopper.config import Config
from chopper.sim_system import build_sim_system
from chopper.net.service import ChopperService
from chopper.net.client import ChopperClient

CMD_PORT = 17320
PUB_PORT = 17321


def _wait(pred, timeout=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    return pred()


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.sim.spinup_tau_s = 0.1          # a quick wheel keeps the test short
    cfg.settle.hold_s = 0.2
    cfg.hardware.poll_hz = 20.0
    ch, _ = build_sim_system(cfg)
    svc = ChopperService(ch, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                         status_hz=20.0)
    svc.start()
    cli = ChopperClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def test_shutdown_verb_keep_outputs(service_and_client):
    # Lukas 2026-10-11: a restart stops the wheel exactly like a plain shutdown
    svc, cli = service_and_client
    svc.ch.cfg.hardware.stop_on_exit = True
    be = svc.ch.backend
    be.set_enable(True)
    r = cli._cmd({"cmd": "shutdown", "keep_outputs": True})
    assert r["ok"] and r["stopping"] and r["kept_outputs"] is False
    svc.stop()                      # what serve_forever's finally does
    assert be.get_enable() is False and svc.ch.status().connected is False


def test_shutdown_verb_text_false_is_false(service_and_client):
    svc, cli = service_and_client
    svc.ch.cfg.hardware.stop_on_exit = True
    be = svc.ch.backend
    be.set_enable(True)
    r = cli._cmd({"cmd": "shutdown", "keep_outputs": "false"})   # gotcha #3
    assert r["kept_outputs"] is False
    svc.stop()
    assert be.get_enable() is False


def test_info_and_config(service_and_client):
    _, cli = service_and_client
    info = cli.start()
    assert info["blade"] == "MC1F10HP"
    assert info["freq_min_Hz"] == 20.0 and info["freq_max_Hz"] == 1000.0
    assert "MC1F60" in info["blades"]
    assert cli.get_config().hardware.baud == 115200


def test_frequency_step_is_accepted_then_locks(service_and_client):
    _, cli = service_and_client
    assert cli.set_frequency(300.0) == 300.0
    s = _wait(lambda: (lambda st: st if st.setpoint_frequency_Hz == 300.0 and st.locked
                       else None)(cli.status()))
    assert s is not None, "never locked at 300 Hz"
    assert abs(s.frequency_Hz - 300.0) < 1.0


def test_start_reply_carries_the_lock_generation(service_and_client):
    _, cli = service_and_client
    r = cli._cmd({"cmd": "stop"})
    assert r["ok"] is True
    r = cli._cmd({"cmd": "start"})
    gen = r["lock_gen"]
    s = _wait(lambda: (lambda st: st if st.lock_gen == gen and st.locked else None)(
        cli.status()))
    assert s is not None


def test_refusals_come_back_as_ok_false_with_a_reason(service_and_client):
    _, cli = service_and_client
    r = cli._cmd({"cmd": "set_blade", "blade": "MC1F60"})       # still running
    assert r["ok"] is False and "standby" in r["error"]
    with pytest.raises(RuntimeError, match="standby"):
        cli.set_blade("MC1F60")
    r = cli._cmd({"cmd": "set_frequency"})
    assert r["ok"] is False and "frequency_Hz" in r["error"]
    r = cli._cmd({"cmd": "no_such_verb"})
    assert r["ok"] is False


def test_blade_change_over_the_wire(service_and_client):
    _, cli = service_and_client
    cli.set_enable(False)
    cli.set_blade("MC1F60")
    s = _wait(lambda: (lambda st: st if st.blade == "MC1F60" else None)(cli.status()))
    assert s.freq_min_Hz == 120.0 and s.freq_max_Hz == 6000.0
    assert s.ref_mode == "internal"


def test_set_config_over_wire_reclamps(service_and_client):
    _, cli = service_and_client
    cli.start()
    cli.set_frequency(800.0)
    cli.cfg.limits.freq_max_Hz = 400.0
    cli.apply_config()
    s = _wait(lambda: (lambda st: st if st.setpoint_frequency_Hz == 400.0 else None)(
        cli.status()))
    assert s is not None


def test_null_measurement_travels_as_nan(service_and_client):
    """JSON has no NaN: a blind wheel travels as null and comes back as NaN."""
    _, cli = service_and_client
    cli.set_enable(False)
    cli.set_output_mode("target")
    r = cli._cmd({"cmd": "status"})
    assert r["status"]["frequency_Hz"] is None
    import math
    s = _wait(lambda: (lambda st: st if st.lock_source == "timer" else None)(cli.status()))
    assert s is not None and math.isnan(s.frequency_Hz)
