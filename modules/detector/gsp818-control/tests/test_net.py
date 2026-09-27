"""End-to-end over ZeroMQ: a service (simulated analyser) and a client on
loopback. Ports 17060/17061 -- this module's own test range, so it never
collides with a running service or a sibling module's tests."""

import time

import numpy as np
import pytest

from gsp818.config import Config
from gsp818.sim_system import build_sim_system
from gsp818.net.service import Gsp818Service
from gsp818.net.client import Gsp818Client

CMD_PORT = 17060
PUB_PORT = 17061


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.sweep.points = 401
    sa, sim = build_sim_system(cfg, realtime=False, seed=4)
    svc = Gsp818Service(sa, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, status_hz=20.0)
    svc.start()
    cli = Gsp818Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli, sim
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _wait(pred, cli, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = cli.status()
        if pred(s):
            return s
        time.sleep(0.03)
    return cli.status()


def test_info_and_config(service_and_client):
    _, cli, _ = service_and_client
    info = cli.start()
    assert info["simulated"] is True and info["freq_max_Hz"] == 1.8e9
    assert "pos_peak" in info["detectors"]
    assert cli.cfg.sweep.points == 401 and cli.cfg.tracking.tg_on is False


def test_continuous_sweeps_arrive(service_and_client):
    _, cli, _ = service_and_client
    s = _wait(lambda s: s.sweeps > 3, cli)
    assert s.connected and s.sweeps > 3
    last = cli.get_trace("last")
    assert last["power_dBm"].dtype == float and last["power_dBm"].shape == (401,)


def test_acquire_blocking_and_the_wire_loses_nothing(service_and_client):
    svc, cli, _ = service_and_client
    cli.set_center(100e6); cli.set_span(2e6)
    _wait(lambda s: s.span_Hz == 2e6, cli)
    a = cli.acquire_blocking(timeout_s=5)
    b = cli.acquire_blocking(timeout_s=5)
    assert b["acq_id"] == a["acq_id"] + 1
    assert b["peak_Hz"] == pytest.approx(100e6, abs=1e4)
    assert b["peak_dBm"] == pytest.approx(-20.0, abs=0.5)
    local = svc.gsp818.get_trace("sample")
    assert np.array_equal(local["power_dBm"], b["power_dBm"])
    assert np.array_equal(local["freqs_Hz"], b["freqs_Hz"])


def test_get_frequencies_in_hz_and_mhz(service_and_client):
    _, cli, _ = service_and_client
    r = cli._cmd({"cmd": "get_frequencies"})
    assert len(r["values"]) == 401
    assert r["values_MHz"][-1] == pytest.approx(r["values"][-1] / 1e6)


def test_every_setter_verb_round_trips(service_and_client):
    _, cli, _ = service_and_client
    cli.set_rbw(30e3); cli.set_vbw(3e3); cli.set_ref_level(-20); cli.set_atten(5)
    cli.set_sweep_time(0.5); cli.set_detector("sample"); cli.set_preamp(True)
    cli.set_averages(3); cli.set_tg_level(-15); cli.set_points(201)
    s = _wait(lambda s: s.points == 201 and s.tg_level_dBm == -15, cli)
    assert (s.rbw_set_Hz, s.vbw_set_Hz, s.ref_level_dBm, s.atten_set_dB) == (30e3, 3e3, -20, 5)
    assert s.sweep_time_set_s == 0.5 and s.detector == "sample" and s.preamp is True
    assert s.averages == 3 and not (s.rbw_auto or s.vbw_auto or s.atten_auto)
    cli.set_rbw_auto(True); cli.set_sweep_time_auto(True)
    s = _wait(lambda s: s.rbw_auto and s.sweep_time_auto, cli)
    assert s.rbw_auto and s.sweep_time_auto
    # a human typing "off" in a console must not switch anything ON (gotcha #3's cousin)
    assert cli._cmd({"cmd": "set_preamp", "on": "off"})["ok"]
    assert _wait(lambda s: s.preamp is False, cli).preamp is False


def test_tg_and_the_reference_over_the_wire(service_and_client):
    svc, cli, sim = service_and_client
    cli.set_start(100e6); cli.set_stop(1.7e9)
    cli.set_tg(True); cli.set_dut("thru")
    _wait(lambda s: s.tg_on and s.dut == "thru", cli)
    assert _wait(lambda s: sim.tg_output, cli) and sim.tg_output is True
    ref = cli.take_reference_blocking(timeout_s=5)
    assert ref["tg_on"] is True
    cli.set_dut("bandpass")
    _wait(lambda s: s.dut == "bandpass", cli)
    cli.acquire_blocking(timeout_s=5)
    n = cli.get_trace("sample", "norm")
    assert "norm_dB" in n and "power_dBm" not in n
    assert np.array_equal(n["norm_dB"], svc.gsp818.get_trace("sample", "norm")["norm_dB"])
    cli.set_points(201)
    _wait(lambda s: s.points == 201, cli)
    cli.acquire_blocking(timeout_s=5)
    r = cli._cmd({"cmd": "get_trace", "which": "sample", "quantity": "norm"})
    assert r["ok"] is False and "201 points vs reference 401" in r["error"]
    cli.clear_reference()
    assert _wait(lambda s: not s.reference.get("present"), cli).reference["present"] is False


def test_describe_rev_follows_the_span(service_and_client):
    _, cli, _ = service_and_client
    rev0 = cli.describe()["revision"]
    cli.set_stop(1e9)
    s = _wait(lambda s: s.describe_rev not in (None, rev0), cli, timeout=3.0)
    assert s.describe_rev != rev0 and s.describe_rev == cli.describe()["revision"]


def test_refusals_are_errors_not_crashes(service_and_client):
    _, cli, _ = service_and_client
    with pytest.raises(ValueError):
        cli.set_detector("rms")
    with pytest.raises(ValueError):
        cli.set_dut("amplifier")
    assert cli._cmd({"cmd": "set_start"})["ok"] is False          # missing argument
    assert cli._cmd({"cmd": "get_trace", "which": "later"})["ok"] is False
    assert cli._cmd({"cmd": "get_trace", "quantity": "phase"})["ok"] is False
    assert cli._cmd({"cmd": "nonsense"})["ok"] is False
    assert cli._cmd({"cmd": "status"})["ok"] is True               # the loop survived


def test_shutdown_verb_replies_then_stops_with_tg_off(service_and_client):
    svc, cli, sim = service_and_client
    cli.set_tg(True)
    _wait(lambda s: sim.tg_output, cli)
    r = cli._cmd({"cmd": "shutdown"})
    assert r == {"ok": True, "stopping": True}
    assert svc._stop.is_set()
    svc.gsp818.shutdown()
    assert sim.tg_output is False
