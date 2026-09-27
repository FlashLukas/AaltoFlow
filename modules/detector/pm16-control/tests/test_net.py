"""End-to-end over ZeroMQ: a service (simulated meter) and a client on loopback.
Non-default ports, so it never collides with a running service."""

import time

import pytest

from pm16.config import Config
from pm16.sim_system import build_sim_system
from pm16.net.service import Pm16Service
from pm16.net.client import Pm16Client

CMD_PORT = 15720
PUB_PORT = 15721


@pytest.fixture
def service_and_client():
    cfg = Config()
    meter, sim = build_sim_system(cfg, realtime=False, sample_period_s=0.005)
    svc = Pm16Service(meter, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, status_hz=20.0)
    svc.start()
    cli = Pm16Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli, sim
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _wait(pred, cli, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = cli.status()
        if pred(s):
            return s
        time.sleep(0.03)
    return cli.status()


def test_info_and_config(service_and_client):
    _, cli, sim = service_and_client
    info = cli.start()
    assert info["wavelength_max_nm"] == 1100.0
    assert info["range_max_W"] == sim.range_limits()[1]
    assert cli.cfg.sensor.wavelength_nm == 633.0      # adopted from the meter


def test_live_readings_arrive(service_and_client):
    _, cli, _ = service_and_client
    s = _wait(lambda s: s.readings > 3, cli)
    assert s.connected and s.power_W > 0


def test_settings_over_wire(service_and_client):
    _, cli, sim = service_and_client
    cli.set_wavelength(sim.laser_nm)
    cli.set_range(1e-3)
    s = _wait(lambda s: s.wavelength_set_nm == sim.laser_nm and not s.auto_range, cli)
    assert s.wavelength_nm == sim.laser_nm
    assert s.range_W == pytest.approx(1.736957e-2)


def test_acquire_blocking_returns_this_acquisition(service_and_client):
    _, cli, _ = service_and_client
    cli.set_acquisition(3)
    a = cli.acquire_blocking(timeout_s=5)
    b = cli.acquire_blocking(timeout_s=5)
    assert b["acq_id"] == a["acq_id"] + 1
    assert a["n"] == 3 and a["power_W"] > 0


def test_describe_rev_follows_auto_range(service_and_client):
    _, cli, _ = service_and_client
    rev0 = cli.describe()["revision"]
    cli.set_auto_range(False)
    s = _wait(lambda s: s.describe_rev not in (None, rev0), cli, timeout=3.0)
    assert s.describe_rev != rev0
    assert s.describe_rev == cli.describe()["revision"]


def test_shutdown_verb_replies_then_stops(service_and_client):
    svc, cli, _ = service_and_client
    r = cli._cmd({"cmd": "shutdown"})
    assert r == {"ok": True, "stopping": True}
    assert svc._stop.is_set()


def test_bad_request_is_an_error_not_a_crash(service_and_client):
    _, cli, _ = service_and_client
    r = cli._cmd({"cmd": "set_wavelength"})           # missing argument
    assert r["ok"] is False
    assert cli._cmd({"cmd": "nonsense"})["ok"] is False
    assert cli._cmd({"cmd": "status"})["ok"] is True  # the loop survived
