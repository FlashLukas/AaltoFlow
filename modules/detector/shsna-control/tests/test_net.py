"""End-to-end over ZeroMQ: a service (simulated SNA) and a client on loopback.
Non-default ports (17730/17731), so it never collides with a running service."""

import time

import numpy as np
import pytest

from shsna.config import Config
from shsna.sim_system import build_sim_system
from shsna.net.service import ShsnaService
from shsna.net.client import ShsnaClient

CMD_PORT = 17730
PUB_PORT = 17731


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.sweep.start_Hz, cfg.sweep.stop_Hz, cfg.sweep.points = 700e6, 1300e6, 201
    shsna, sim = build_sim_system(cfg, realtime=False, seed=4)
    svc = ShsnaService(shsna, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, status_hz=20.0)
    svc.start()
    cli = ShsnaClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
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
    assert info["simulated"] is True and info["freq_max_Hz"] == 4.4e9
    assert info["points_max"] == 1001 and info["owner"] == "simulated"
    assert cli.cfg.sweep.points == 201


def test_nothing_sweeps_at_start(service_and_client):
    _, cli = service_and_client
    time.sleep(0.3)
    s = cli.status()
    assert s.connected and s.sweeps == 0 and s.continuous is False
    assert s.owner["reachable"] is True and s.owner["tg_attached"] is True


def test_reference_then_transmission_over_the_wire(service_and_client):
    svc, cli = service_and_client
    cli.set_sim("dut_inserted", False)
    ref = cli.take_reference_blocking(timeout_s=5)
    assert ref["reference"].shape == (201,) and ref["freqs_Hz"][0] == 700e6
    s = _wait(lambda s: s.reference.get("present") is True, cli)
    assert s.reference["acq_id"] == ref["acq_id"] and s.reference["points"] == 201
    cli.set_sim("dut_inserted", True)
    a = cli.acquire_blocking(timeout_s=5)
    b = cli.acquire_blocking(timeout_s=5)
    assert b["acq_id"] == a["acq_id"] + 1 and b["raw"].shape == (201,)
    t = cli.get_trace("transmission")
    local = svc.shsna.get_trace("transmission")
    assert np.array_equal(t["transmission"], local["transmission"])     # the wire loses nothing
    np.testing.assert_allclose(t["freqs_Hz"], local["freqs_Hz"])
    r = cli.get_result()
    assert r["peak_freq_hz"] == pytest.approx(1e9, abs=6e6)
    assert np.allclose(cli.frequencies(), local["freqs_Hz"])
    cli.clear_reference()
    assert _wait(lambda s: s.reference.get("present") is False, cli).reference["present"] is False
    with pytest.raises(ValueError, match="needs a thru reference"):
        cli.get_trace("transmission")


def test_a_failure_reaches_the_client(service_and_client):
    svc, cli = service_and_client
    svc.shsna.backend.fail_next = "TG lost"
    with pytest.raises(RuntimeError, match="TG lost"):
        cli.acquire_blocking(timeout_s=5)
    s = cli.status()
    assert s.acq_error == "TG lost" and s.sample["failed"] is True


def test_describe_rev_follows_the_sweep_span(service_and_client):
    _, cli = service_and_client
    rev0 = cli.describe()["revision"]
    cli.set_stop(1.2e9)
    s = _wait(lambda s: s.describe_rev not in (None, rev0), cli, timeout=3.0)
    assert s.describe_rev != rev0
    assert s.describe_rev == cli.describe()["revision"]


def test_setters_round_trip(service_and_client):
    _, cli = service_and_client
    cli.set_points(101)
    cli.set_rbw(3e3)
    cli.set_averages(3)
    cli.set_continuous(True)
    s = _wait(lambda s: s.points == 101 and s.rbw_Hz == 3e3 and s.averages == 3
              and s.continuous, cli)
    assert (s.points, s.rbw_Hz, s.averages, s.continuous) == (101, 3e3, 3, True)
    assert _wait(lambda s: s.sweeps > 1, cli).sweeps > 1    # now it sweeps on its own
    assert cli.get_trace("raw", "last")["raw"].shape == (101,)


def test_refusals_are_errors_not_crashes(service_and_client):
    _, cli = service_and_client
    with pytest.raises(ValueError):
        cli.set_sim("warp_drive", 1)
    assert cli._cmd({"cmd": "set_start"})["ok"] is False          # missing argument
    assert cli._cmd({"cmd": "get_trace", "which": "s21"})["ok"] is False
    assert cli._cmd({"cmd": "set_level", "level_dBm": -10})["ok"] is False   # there is no level
    assert cli._cmd({"cmd": "nonsense"})["ok"] is False
    assert cli._cmd({"cmd": "status"})["ok"] is True               # the loop survived


def test_shutdown_verb_replies_then_stops(service_and_client):
    svc, cli = service_and_client
    r = cli._cmd({"cmd": "shutdown"})
    assert r == {"ok": True, "stopping": True, "kept_outputs": True}
    assert svc._stop.is_set()
