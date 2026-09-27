"""End-to-end over ZeroMQ: a service (simulated VNA) and a client on loopback.
Non-default ports, so it never collides with a running service."""

import time

import numpy as np
import pytest

from vna import model
from vna.config import Config
from vna.sim_system import build_sim_system
from vna.net.service import VnaService
from vna.net.client import VnaClient

CMD_PORT = 15730
PUB_PORT = 15731


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.field.source = "manual"
    cfg.sweep.points = 401
    vna, sim = build_sim_system(cfg, realtime=False, seed=4)
    svc = VnaService(vna, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, status_hz=20.0)
    svc.start()
    cli = VnaClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
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
    assert info["simulated"] is True and info["freq_max_Hz"] == 20e9
    assert cli.cfg.sweep.points == 401 and cli.cfg.field.source == "manual"


def test_continuous_sweeps_arrive(service_and_client):
    _, cli = service_and_client
    s = _wait(lambda s: s.sweeps > 3, cli)
    assert s.connected and s.sweeps > 3
    last = cli.get_trace("last")
    assert last["s"].dtype == complex and last["s"].shape == (401,)


def test_acquire_blocking_returns_a_complex_trace_at_this_field(service_and_client):
    _, cli = service_and_client
    cli.set_manual_field(60.0)
    _wait(lambda s: s.manual_field_mT == 60.0, cli)
    a = cli.acquire_blocking(timeout_s=5)
    b = cli.acquire_blocking(timeout_s=5)
    assert b["acq_id"] == a["acq_id"] + 1
    assert np.iscomplexobj(b["s"]) and b["freqs_Hz"].shape == b["s"].shape
    assert b["field_mT"] == 60.0
    assert b["dip_Hz"] == pytest.approx(model.kittel_Hz(60.0, Config().sample), abs=8e6)


def test_the_wire_trace_is_the_brain_trace(service_and_client):
    """Complex travels as re/im lists; nothing may be lost or reordered."""
    svc, cli = service_and_client
    cli.acquire_blocking(timeout_s=5)
    local = svc.vna.get_trace("sample")
    remote = cli.get_trace("sample")
    assert np.array_equal(local["s"], remote["s"])
    assert np.array_equal(local["freqs_Hz"], remote["freqs_Hz"])


def test_get_frequencies_in_hz_and_ghz(service_and_client):
    _, cli = service_and_client
    r = cli._cmd({"cmd": "get_frequencies"})
    assert len(r["values"]) == 401
    assert r["values_GHz"][0] == pytest.approx(r["values"][0] / 1e9)


def test_describe_rev_follows_the_sweep_span(service_and_client):
    _, cli = service_and_client
    rev0 = cli.describe()["revision"]
    cli.set_stop(3e9)
    s = _wait(lambda s: s.describe_rev not in (None, rev0), cli, timeout=3.0)
    assert s.describe_rev != rev0
    assert s.describe_rev == cli.describe()["revision"]


def test_refusals_are_errors_not_crashes(service_and_client):
    _, cli = service_and_client
    with pytest.raises(ValueError):
        cli.set_field_source("hall probe")
    assert cli._cmd({"cmd": "set_start"})["ok"] is False          # missing argument
    assert cli._cmd({"cmd": "get_trace", "which": "later"})["ok"] is False
    assert cli._cmd({"cmd": "nonsense"})["ok"] is False
    assert cli._cmd({"cmd": "status"})["ok"] is True               # the loop survived


def test_reference_verbs_round_trip(service_and_client):
    """take_reference / get_trace quantity u / clear_reference / set_sparam /
    set_manual_angle over the wire, and the reference block in status."""
    svc, cli = service_and_client
    assert cli._cmd({"cmd": "get_trace", "which": "last", "quantity": "u"})["ok"] is False

    cli.set_manual_field(0.0, angle_deg=45.0)
    _wait(lambda s: s.manual_angle_deg == 45.0, cli)
    ref = cli.take_reference_blocking(timeout_s=5)
    assert np.iscomplexobj(ref["s"]) and ref["field_mT"] == 0.0
    assert ref["angle_deg"] == pytest.approx(45.0)
    s = _wait(lambda s: s.reference.get("present") is True, cli)
    assert s.reference["acq_id"] == ref["acq_id"] and s.reference["sparam"] == "S21"

    cli.set_manual_field(60.0)
    _wait(lambda s: s.manual_field_mT == 60.0, cli)
    cli.acquire_blocking(timeout_s=5)
    u = cli.get_trace("sample", "u")
    local = svc.vna.get_trace("sample", "u")
    assert "u" in u and "s" not in u
    assert np.array_equal(u["u"], local["u"])        # the wire loses nothing
    assert u["reference_acq_id"] == ref["acq_id"]

    cli.set_sparam("S12")
    _wait(lambda s: s.sparam == "S12", cli)
    cli.acquire_blocking(timeout_s=5)
    r = cli._cmd({"cmd": "get_trace", "which": "sample", "quantity": "u"})
    assert r["ok"] is False and "S-parameter S12 vs reference S21" in r["error"]
    with pytest.raises(ValueError, match="Take a new reference"):
        cli.get_trace("sample", "u")

    cli.set_manual_angle(30.0)
    assert _wait(lambda s: s.manual_angle_deg == 30.0, cli).manual_angle_deg == 30.0
    cli.clear_reference()
    assert _wait(lambda s: s.reference.get("present") is False, cli).reference["present"] is False
    info = cli.info()
    assert info["simulated"] is True and "S22" in info["sparams"]


def test_shutdown_verb_replies_then_stops(service_and_client):
    svc, cli = service_and_client
    r = cli._cmd({"cmd": "shutdown"})
    assert r == {"ok": True, "stopping": True}
    assert svc._stop.is_set()
