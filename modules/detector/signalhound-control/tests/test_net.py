"""End-to-end over ZeroMQ: a service (simulated analyser) and a client on
loopback. Ports 17080/17081 -- reserved for this module's tests, so they never
collide with a running service or a sibling module's tests."""

import time

import numpy as np
import pytest

from signalhound.config import Config
from signalhound.sim_system import build_sim_system
from signalhound.net.service import SignalhoundService
from signalhound.net.client import SignalhoundClient

CMD_PORT = 17080
PUB_PORT = 17081


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.sweep.span_Hz = 20e6                # 401 bins at 100 kHz RBW
    signalhound, sim = build_sim_system(cfg, realtime=False, seed=4)
    svc = SignalhoundService(signalhound, host="127.0.0.1", cmd_port=CMD_PORT,
                             pub_port=PUB_PORT, status_hz=20.0)
    svc.start()
    cli = SignalhoundClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                            timeout_ms=2000)
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
    assert info["simulated"] is True and info["model"] == "SA44B"
    assert info["freq_max_Hz"] == 4.4e9 and info["tg_attached"] is True
    assert cli.cfg.sweep.span_Hz == 20e6 and not hasattr(cli.cfg, "tracking")
    assert info["tg_level_min_dBm"] == -30.0 and info["tg_points_max"] == 1001


def test_service_start_leaves_the_analyser_unconfigured(service_and_client):
    _, cli = service_and_client
    s = _wait(lambda s: s.connected, cli)
    assert s.configured is False and s.continuous is False and s.sweeps == 0


def test_continuous_sweeps_arrive(service_and_client):
    _, cli = service_and_client
    cli.set_continuous(True)                # start-up leaves it off (start-up rule)
    s = _wait(lambda s: s.sweeps > 3, cli)
    assert s.connected and s.sweeps > 3 and s.points == 401
    last = cli.get_trace("last")
    assert last["trace"].dtype == float and last["trace"].shape == (401,)


def test_acquire_blocking_returns_a_fresh_trace(service_and_client):
    _, cli = service_and_client
    a = cli.acquire_blocking(timeout_s=5)
    b = cli.acquire_blocking(timeout_s=5)
    assert b["acq_id"] == a["acq_id"] + 1
    assert b["freqs_Hz"].shape == b["trace"].shape
    assert b["peak_dBm"] == pytest.approx(Config().scene.tone_dBm, abs=0.5)


def test_the_wire_trace_is_the_brain_trace(service_and_client):
    """dBm travel as a plain list; nothing may be lost or reordered."""
    svc, cli = service_and_client
    cli.acquire_blocking(timeout_s=5)
    local = svc.signalhound.get_trace("sample")
    remote = cli.get_trace("sample")
    assert np.array_equal(local["trace"], remote["trace"])
    assert np.array_equal(local["freqs_Hz"], remote["freqs_Hz"])


def test_get_frequencies_in_hz_and_ghz(service_and_client):
    _, cli = service_and_client
    r = cli._cmd({"cmd": "get_frequencies"})
    assert len(r["values"]) == 401
    assert r["values_GHz"][0] == pytest.approx(r["values"][0] / 1e9)


def test_describe_rev_follows_the_window(service_and_client):
    _, cli = service_and_client
    rev0 = cli.describe()["revision"]
    cli.set_center(2e9)
    s = _wait(lambda s: s.describe_rev not in (None, rev0), cli, timeout=3.0)
    assert s.describe_rev != rev0
    assert s.describe_rev == cli.describe()["revision"]


def test_refusals_are_errors_not_crashes(service_and_client):
    _, cli = service_and_client
    with pytest.raises(ValueError):
        cli.set_detector("quasi-peak")
    assert cli._cmd({"cmd": "set_center"})["ok"] is False          # missing argument
    assert cli._cmd({"cmd": "get_trace", "which": "later"})["ok"] is False
    assert cli._cmd({"cmd": "take_reference"})["ok"] is False      # moved to shsna
    assert cli._cmd({"cmd": "nonsense"})["ok"] is False
    assert cli._cmd({"cmd": "status"})["ok"] is True               # the loop survived


def test_bool_args_are_parsed_not_cast(service_and_client):
    """bool("false") is True (gotcha #3): a hand-typed "false" must mean off."""
    _, cli = service_and_client
    assert cli._cmd({"cmd": "set_reject", "on": "false"})["ok"]
    assert _wait(lambda s: s.reject is False, cli).reject is False


def test_shutdown_verb_replies_then_stops(service_and_client):
    svc, cli = service_and_client
    r = cli._cmd({"cmd": "shutdown"})
    assert r == {"ok": True, "stopping": True, "kept_outputs": False}
    assert svc._stop.is_set()
