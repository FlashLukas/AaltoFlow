"""End-to-end over ZeroMQ: a service (simulated console) and a client on
loopback. Ports 17400-17419 are this module's test range, so the tests never
collide with a running service or a sibling module's tests."""

import time

import pytest

from pm400.config import Config
from pm400.sim_system import build_sim_system
from pm400.net.service import Pm400Service
from pm400.net.client import Pm400Client

CMD_PORT = 17400
PUB_PORT = 17401


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.hardware.head_check_s = 0.1
    meter, sim = build_sim_system(cfg, realtime=True, zero_time_s=0.2)
    # Short averaging -> fast test. Set on the simulated console's "front
    # panel" (start-up adopts it); the config is never pushed at start.
    sim.front_panel(avg_time_s=0.005)
    svc = Pm400Service(meter, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                       status_hz=20.0)
    svc.start()
    cli = Pm400Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli, cfg
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
    _, cli, cfg = service_and_client
    info = cli.start()
    assert info["head"] == "photodiode" and info["unit"] == "W"
    assert info["wavelength_max_nm"] == 1100.0
    assert cli.cfg.sim.head == "photodiode"


def test_live_readings_arrive(service_and_client):
    _, cli, _ = service_and_client
    s = _wait(lambda s: s.readings > 3, cli)
    assert s.connected and s.value > 0 and s.quantity == "power"


def test_settings_over_wire(service_and_client):
    _, cli, cfg = service_and_client
    cli.set_wavelength(cfg.sim.laser_nm)
    cli.set_range(5e-4)
    cli.set_avg_time(0.01)
    s = _wait(lambda s: s.wavelength_set_nm == cfg.sim.laser_nm and not s.auto_range
              and s.avg_time_set_s == 0.01, cli)
    assert s.wavelength_nm == cfg.sim.laser_nm
    assert s.range == pytest.approx(1e-3)
    assert s.avg_time_s == pytest.approx(0.01)


def test_acquire_blocking_returns_this_acquisition(service_and_client):
    _, cli, _ = service_and_client
    cli.set_acquisition(3)
    a = cli.acquire_blocking(timeout_s=5)
    b = cli.acquire_blocking(timeout_s=5)
    assert b["acq_id"] == a["acq_id"] + 1
    assert a["n"] == 3 and a["value"] > 0 and a["unit"] == "W"


def test_zero_over_the_wire_is_numbered(service_and_client):
    _, cli, _ = service_and_client
    n = cli.zero()
    s = _wait(lambda s: s.zero_id == n and not s.zeroing, cli)
    assert s.zero_error == "OK"


def test_head_swap_moves_describe_rev(service_and_client):
    _, cli, _ = service_and_client
    rev0 = cli.describe()["revision"]
    cli.get_config()
    cli.cfg.sim.head = "pyro"
    cli.apply_config()                      # set_config: the "user" plugs in a pyro head
    s = _wait(lambda s: s.quantity == "energy" and s.describe_rev not in (None, rev0), cli)
    assert s.unit == "J"
    man = cli.describe()
    assert s.describe_rev == man["revision"]
    assert "energy" in {p["id"] for p in man["parameters"]}


def test_shutdown_verb_replies_then_stops(service_and_client):
    svc, cli, _ = service_and_client
    r = cli._cmd({"cmd": "shutdown"})
    assert r == {"ok": True, "stopping": True, "kept_outputs": True}
    assert svc._stop.is_set()


def test_bad_request_is_an_error_not_a_crash(service_and_client):
    _, cli, _ = service_and_client
    r = cli._cmd({"cmd": "set_wavelength"})           # missing argument
    assert r["ok"] is False
    assert cli._cmd({"cmd": "nonsense"})["ok"] is False
    assert cli._cmd({"cmd": "set_avg_time", "avg_time_s": "fast"})["ok"] is False
    assert cli._cmd({"cmd": "status"})["ok"] is True  # the loop survived
