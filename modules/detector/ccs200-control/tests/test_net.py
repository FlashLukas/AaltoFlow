"""End-to-end over ZeroMQ: a service (simulated spectrometer) and a client on
loopback. Ports 17260/17261 -- this module's own test range, so it never
collides with a running service or a sibling module's tests."""

import time

import numpy as np
import pytest

from ccs200.config import Config
from ccs200.sim_system import build_sim_system
from ccs200.net.service import Ccs200Service
from ccs200.net.client import Ccs200Client

CMD_PORT = 17260
PUB_PORT = 17261


@pytest.fixture
def service_and_client():
    cfg = Config()
    ccs200, _ = build_sim_system(cfg, realtime=False, seed=4)
    svc = Ccs200Service(ccs200, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                        status_hz=20.0)
    svc.start()
    cli = Ccs200Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli
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
    _, cli = service_and_client
    info = cli.start()
    assert info["simulated"] is True and info["pixels"] == 3648
    assert info["integration_min_s"] == 1e-5 and info["integration_max_s"] == 60.0
    # the service ADOPTED the sim instrument's own 5 ms, not the config's 10 ms
    assert cli.cfg.scan.integration_time_s == 0.005
    assert cli.status().integration_time_s == 0.005


def test_continuous_scans_arrive(service_and_client):
    _, cli = service_and_client
    s = _wait(lambda s: s.scans > 3, cli)
    assert s.connected and s.scans > 3
    last = cli.get_trace("last")
    assert last["spectrum"].shape == (3648,) and last["wavelengths_nm"].shape == (3648,)


def test_the_wire_spectrum_is_the_brain_spectrum(service_and_client):
    svc, cli = service_and_client
    cli.set_continuous(False)
    t = cli.acquire_blocking(timeout_s=5)
    local = svc.ccs200.get_trace("sample")
    assert t["acq_id"] == local["acq_id"]
    assert np.array_equal(t["spectrum"], local["spectrum"])
    assert np.array_equal(cli.wavelengths(), svc.ccs200.wavelengths())
    assert t["peak_nm"] == pytest.approx(546.07, abs=0.3)


def test_acquire_ids_increase_and_follow_settings(service_and_client):
    _, cli = service_and_client
    cli.set_integration_time(0.004)
    _wait(lambda s: s.integration_time_s == 0.004, cli)
    a = cli.acquire_blocking(timeout_s=5)
    b = cli.acquire_blocking(timeout_s=5)
    assert b["acq_id"] == a["acq_id"] + 1 and b["integration_time_s"] == 0.004


def test_dark_round_trip_and_refusal(service_and_client):
    _, cli = service_and_client
    cli.set_dark_subtract(True)
    _wait(lambda s: s.dark_subtract, cli)
    with pytest.raises(ValueError, match="no dark"):
        cli.acquire()
    cli.set_light(False)
    _wait(lambda s: s.light_on is False, cli)
    dark = cli.take_dark_blocking(timeout_s=5)
    assert dark["spectrum"].mean() < 0.01
    s = _wait(lambda s: s.dark.get("present") is True, cli)
    assert s.dark["matches"] is True
    cli.set_light(True)
    t = cli.acquire_blocking(timeout_s=5)
    assert t["dark_applied"] is True
    cli.clear_dark()
    assert _wait(lambda s: s.dark.get("present") is False, cli).dark["present"] is False


def test_describe_rev_follows_the_window(service_and_client):
    _, cli = service_and_client
    rev0 = cli.describe()["revision"]
    cli.set_window(540.0, 560.0)
    s = _wait(lambda s: s.describe_rev not in (None, rev0), cli, timeout=3.0)
    assert s.describe_rev != rev0
    assert s.describe_rev == cli.describe()["revision"]


def test_refusals_are_errors_not_crashes(service_and_client):
    _, cli = service_and_client
    with pytest.raises(ValueError):
        cli.set_sim("gravity", 9.81)
    assert cli._cmd({"cmd": "set_integration_time"})["ok"] is False    # missing argument
    assert cli._cmd({"cmd": "get_trace", "which": "later"})["ok"] is False
    assert cli._cmd({"cmd": "nonsense"})["ok"] is False
    assert cli._cmd({"cmd": "status"})["ok"] is True                   # the loop survived


def test_string_bools_are_parsed_not_truthy(service_and_client):
    _, cli = service_and_client
    assert cli._cmd({"cmd": "set_continuous", "on": "off"})["ok"] is True
    assert _wait(lambda s: s.continuous is False, cli).continuous is False


def test_shutdown_verb_replies_then_stops(service_and_client):
    svc, cli = service_and_client
    r = cli._cmd({"cmd": "shutdown"})
    assert r == {"ok": True, "stopping": True, "kept_outputs": True}
    assert svc._stop.is_set()
