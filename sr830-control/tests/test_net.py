"""End-to-end over ZeroMQ on loopback, on this module's own test ports
(17200..17219), so the tests never collide with a running service or with a
sibling module's tests."""

import json
import time

import pytest

from sr830.config import Config
from sr830.sim_system import build_sim_system
from sr830.net.service import Sr830Service
from sr830.net.client import Sr830Client

CMD_PORT = 17200
PUB_PORT = 17201


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.demod.time_constant = "3 ms"          # short, so acquisitions finish fast
    li, sim = build_sim_system(cfg, seed=3)
    sim.auto_gain_s = 0.1
    svc = Sr830Service(li, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                       status_hz=20.0)
    svc.start()
    cli = Sr830Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                           # let PUB/SUB connect
    yield svc, cli, sim
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _wait(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.03)
    return pred()


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["freq_max_Hz"] == 102000.0
    assert len(info["time_constants"]) == 20 and len(info["sensitivities"]) == 27
    assert cli.get_config().hardware.resource == "GPIB0::8::INSTR"


def test_settings_take_effect(service_and_client):
    _, cli, sim = service_and_client
    assert cli.set_time_constant("10 ms")["ok"]
    assert cli.set_sensitivity("5 mV")["ok"]
    assert cli.set_frequency(999.0)["ok"]
    assert cli.set_slope("12 dB/oct")["ok"]
    assert cli.set_sync_filter(True)["ok"]
    assert cli.set_aux_out(3, -2.5)["ok"]
    assert cli.set_sine_out(0.5)["ok"]
    s = _wait(lambda: (lambda st: st if st.time_constant == "10 ms"
                       and st.sensitivity == "5 mV" and st.freq_set_Hz == 999.0
                       and st.order == 2 and st.sync_filter
                       and st.aux_out_set_V[2] == -2.5 else None)(cli.status()))
    assert s is not None
    assert sim.sine_V == pytest.approx(0.5)


def test_refusals_come_back_as_errors(service_and_client):
    _, cli, _ = service_and_client
    assert cli.set_reference_source("external")["ok"]
    r = cli.set_frequency(1000.0)             # refused: external reference
    assert r["ok"] is False and "EXTERNAL" in r["error"]
    assert cli.set_aux_out(7, 1.0)["ok"] is False
    assert cli._cmd({"cmd": "set_reserve", "reserve": "huge"})["ok"] is False
    assert cli._cmd({"cmd": "set_phase"})["ok"] is False          # missing argument
    assert cli._cmd({"cmd": "no_such_verb"})["ok"] is False


def test_acquire_blocking_returns_its_own_sample(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    first = cli.acquire_blocking(timeout_s=5.0)
    second = cli.acquire_blocking(timeout_s=5.0)
    assert second["acq_id"] == first["acq_id"] + 1
    assert second["r"] > 0
    assert len(second["aux_in"]) == 4


def test_auto_gain_over_the_wire(service_and_client):
    _, cli, _ = service_and_client
    assert cli.set_sensitivity("1 V")["ok"]
    time.sleep(0.2)
    n = cli.auto_gain()
    st = cli.wait_auto(n, timeout_s=5.0)
    assert st["sensitivity"] == "5 mV" and st["auto_id"] == n


def test_status_is_valid_json_before_any_reading(service_and_client):
    """NaN is not JSON: missing readings must travel as null."""
    svc, cli, _ = service_and_client
    raw = json.dumps(svc.status_payload(), allow_nan=False)   # raises on NaN
    assert "sample" in json.loads(raw)


def test_set_config_over_wire(service_and_client):
    _, cli, sim = service_and_client
    cli.start()
    cli.cfg.limits.sine_max_V = 0.1
    cli.cfg.reference.sine_out_V = 2.0
    cli.apply_config()
    s = _wait(lambda: (lambda st: st if st.sine_out_set_V == pytest.approx(0.1)
                       else None)(cli.status()))
    assert s is not None and sim.sine_V == pytest.approx(0.1)


def test_shutdown_verb_stops_and_makes_outputs_safe():
    cfg = Config()
    li, sim = build_sim_system(cfg, seed=4)
    svc = Sr830Service(li, host="127.0.0.1", cmd_port=17202, pub_port=17203)
    import threading
    t = threading.Thread(target=svc.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    cli = Sr830Client(host="127.0.0.1", cmd_port=17202, pub_port=17203)
    try:
        assert cli.set_sine_out(1.0)["ok"]
        assert cli._cmd({"cmd": "shutdown"})["stopping"] is True
        t.join(5.0)
        assert not t.is_alive()
        assert sim.sine_V == pytest.approx(0.004)
    finally:
        cli.shutdown()
