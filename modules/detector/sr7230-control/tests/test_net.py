"""End-to-end over ZeroMQ on loopback, on this module's own test ports
(17220..17239) so the tests never collide with a running service or with the
other modules' tests."""

import json
import time

import pytest

from sr7230.config import Config
from sr7230.sim_system import build_sim_system
from sr7230.net.service import Sr7230Service
from sr7230.net.client import Sr7230Client

CMD_PORT = 17220
PUB_PORT = 17221


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.filter.fast_mode = True
    cfg.filter.time_constant_s = 2e-3        # short, so acquisitions finish fast
    li, sim = build_sim_system(cfg, seed=3)
    svc = Sr7230Service(li, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                        status_hz=20.0)
    svc.start()
    cli = Sr7230Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                          # let PUB/SUB connect
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


def _status_when(cli, cond, timeout=2.0):
    def check():
        st = cli.status()
        return st if cond(st) else None
    return _wait(check, timeout)


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["model"] == "Signal Recovery 7230"
    assert info["instrument_freq_max_Hz"] == 120e3
    assert info["tc_min_s"] == 10e-6                # fast mode on
    assert any(s["label"] == "100 mV" for s in info["sensitivities"])
    assert cli.get_config().hardware.port == 50000


def test_settings_take_effect(service_and_client):
    _, cli, sim = service_and_client
    assert cli.set_time_constant(0.05)["ok"]
    assert cli.set_slope("6 dB/oct")["ok"]
    assert cli.set_frequency(999.0)["ok"]
    assert cli.set_sensitivity("10 mV")["ok"]
    assert cli.set_input("A-B")["ok"]
    assert cli.set_coupling("DC")["ok"]
    assert cli.set_phase(12.5)["ok"]
    s = _status_when(cli, lambda st: st.tc_set_s == 0.05 and st.freq_set_Hz == 999.0
                     and st.sensitivity == "10 mV" and st.phase_deg == 12.5)
    assert s is not None
    assert s.slope == "6 dB/oct" and s.input == "A-B" and s.coupling == "DC"
    assert sim.dc is True


def test_refusals_come_back_as_errors(service_and_client):
    _, cli, _ = service_and_client
    assert cli.set_slope(9)["ok"] is False
    assert cli.set_sensitivity("3 mV")["ok"] is False
    assert cli._cmd({"cmd": "set_input", "mode": "C"})["ok"] is False
    assert cli._cmd({"cmd": "set_frequency"})["ok"] is False       # missing argument
    assert cli._cmd({"cmd": "no_such_verb"})["ok"] is False


def test_acquire_blocking_returns_its_own_sample(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    first = cli.acquire_blocking(timeout_s=5.0)
    second = cli.acquire_blocking(timeout_s=5.0)
    assert second["acq_id"] == first["acq_id"] + 1
    assert second["r"] > 0
    assert len(second["adc"]) == 2 and second["overload"] in (True, False)


def test_auto_operation_over_the_wire(service_and_client):
    _, cli, _ = service_and_client
    time.sleep(0.2)                                 # let the output settle first
    n = cli.auto("auto_measure")
    s = _status_when(cli, lambda st: st.auto_id == n and not st.auto_busy, timeout=3.0)
    assert s is not None and s.auto_error == ""
    assert s.sensitivity == "5 mV"                  # the ~2 mV simulated signal


def test_amplitude_over_the_wire_and_shutdown_zeroes_it(service_and_client):
    svc, cli, sim = service_and_client
    assert cli.set_amplitude(0.25)["ok"]
    assert _wait(lambda: sim.osc_amp == 0.25)
    r = cli._cmd({"cmd": "shutdown"})
    assert r["ok"] and r["stopping"]
    svc.stop()
    assert sim.osc_amp == 0.0


def test_status_is_valid_json_before_any_reading(service_and_client):
    """NaN is not JSON: missing readings must travel as null."""
    svc, cli, _ = service_and_client
    raw = json.dumps(svc.status_payload(), allow_nan=False)   # raises on NaN
    assert "sample" in json.loads(raw)


def test_set_config_over_wire(service_and_client):
    _, cli, _ = service_and_client
    cli.start()
    cli.cfg.limits.tc_max_s = 0.1
    cli.cfg.filter.time_constant_s = 5.0
    cli.apply_config()
    s = _status_when(cli, lambda st: st.tc_set_s == 0.1)
    assert s is not None and s.tc_s == 0.1
