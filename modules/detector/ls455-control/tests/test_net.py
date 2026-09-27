"""End-to-end over ZeroMQ: a service (simulated meter) and a client on loopback.
Ports 17380/17381 -- this module's private test range, so it never collides
with a running service or a sibling module's tests."""

import time

import pytest

from ls455.config import Config
from ls455.sim_system import build_sim_system
from ls455.net.service import Ls455Service
from ls455.net.client import Ls455Client

CMD_PORT = 17380
PUB_PORT = 17381


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.acquisition.settle_time_constants = 1.0       # keep the test quick
    meter, sim = build_sim_system(cfg, realtime=False)
    cfg.hardware.poll_hz = 50.0
    svc = Ls455Service(meter, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, status_hz=20.0)
    svc.start()
    cli = Ls455Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
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
    assert info["probe"] == "HSE" and info["unit"] == "mT"
    assert info["range_max_mT"] == max(sim.ranges_mT())
    assert cli.cfg.meter.mode == "dc"


def test_live_readings_arrive_in_mT(service_and_client):
    _, cli, sim = service_and_client
    s = _wait(lambda s: s.readings > 3, cli)
    assert s.connected and s.field_mT == pytest.approx(sim.field_mT, abs=0.5)
    assert s.measured_field_mT == pytest.approx(s.field_mT)


def test_settings_over_wire(service_and_client):
    _, cli, sim = service_and_client
    cli.set_range(100.0)
    cli.set_dc_digits(5)
    cli.set_display_unit("T")
    cli.set_relative(True, 40.0)
    s = _wait(lambda s: not s.auto_range and s.dc_digits == 5 and s.relative, cli)
    assert s.range_mT == 350.0 and s.range_set_mT == 100.0
    assert s.display_unit == "T" and sim.get_display_unit() == "T"
    assert s.rel_setpoint_mT == 40.0
    r = cli._cmd({"cmd": "set_relative", "setpoint_mT": 41.0})    # setpoint alone keeps 'on'
    assert r["ok"]
    s = _wait(lambda s: s.rel_setpoint_mT == 41.0, cli)
    assert s.relative is True


def test_acquire_blocking_returns_this_acquisition(service_and_client):
    _, cli, _ = service_and_client
    cli.set_acquisition(3)
    a = cli.acquire_blocking(timeout_s=5)
    b = cli.acquire_blocking(timeout_s=5)
    assert b["acq_id"] == a["acq_id"] + 1
    assert a["n"] == 3 and a["field_mT"] > 0


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
    assert cli._cmd({"cmd": "set_range"})["ok"] is False           # missing argument
    assert cli._cmd({"cmd": "set_mode", "mode": "ac"})["ok"] is False
    assert cli._cmd({"cmd": "set_relative"})["ok"] is False
    assert cli._cmd({"cmd": "nonsense"})["ok"] is False
    assert cli._cmd({"cmd": "status"})["ok"] is True                # the loop survived


def test_reread_probe_over_the_wire(service_and_client):
    _, cli, sim = service_and_client
    assert cli.status().probe == "HSE"
    sim.swap_probe("HST")
    cli.reread_probe()
    s = _wait(lambda s: s.probe == "HST", cli)
    assert s.probe == "HST" and max(s.ranges_mT) == 35000.0
    assert s.probe_desc.startswith("HST axial")
    assert cli.info()["probe_geometry"] == "axial"
