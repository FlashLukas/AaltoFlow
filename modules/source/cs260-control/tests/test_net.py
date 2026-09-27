"""End-to-end over ZeroMQ: a service (simulated monochromator) and a client on
loopback. Uses ports 17240..17259 only -- unique to this module, so the tests
never collide with a running service or with a sibling module's tests."""

import time

import pytest

from cs260.config import Config
from cs260.sim_system import build_sim_system
from cs260.net.service import Cs260Service
from cs260.net.client import Cs260Client

CMD_PORT = 17240
PUB_PORT = 17241


@pytest.fixture
def service_and_client():
    cfg = Config()
    cfg.sim.slew_nm_per_s_at_1200 = 4000.0     # fast drive: tests, not a movie
    cfg.sim.grating_change_s = 0.3
    cfg.motion.poll_s = 0.02
    mono, _ = build_sim_system(cfg)
    svc = Cs260Service(mono, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                       status_hz=20.0)
    svc.start()
    cli = Cs260Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def _status(cli):
    return cli._cmd({"cmd": "status"})["status"]


def wait_arrived(cli, target, timeout=10.0):
    """The adopt_then_flag rule scan-core uses, spelled out."""
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        s = _status(cli)
        if abs(s["target_nm"] - target) < 1e-3 and not s["moving"]:
            return s
        time.sleep(0.02)
    raise AssertionError("never arrived")


def test_info_and_config(service_and_client):
    _, cli = service_and_client
    info = cli.start()
    assert [g["lines"] for g in info["gratings"]] == [1200, 600]
    assert info["filter_wheel"] is False
    assert cli.get_config().hardware.visa == "GPIB0::4::INSTR"


def test_wavelength_move_over_the_wire(service_and_client):
    _, cli = service_and_client
    assert cli.set_wavelength(650.0) == 650.0
    s = _status(cli)                         # the reply said accepted, not done
    assert s["target_nm"] == 650.0 and s["moving"] is True
    s = wait_arrived(cli, 650.0)
    assert s["wavelength_nm"] == pytest.approx(650.0, abs=0.01)
    assert "describe_rev" in s


def test_grating_change_moves_describe_rev(service_and_client):
    _, cli = service_and_client
    rev0 = cli.describe()["revision"]
    cli.set_grating(2)
    s = wait_arrived(cli, _status(cli)["target_nm"])
    assert s["grating"] == 2
    assert cli.describe()["revision"] != rev0
    time.sleep(0.3)
    assert _status(cli)["describe_rev"] == cli.describe()["revision"]


def test_clamp_and_refusals_over_wire(service_and_client):
    _, cli = service_and_client
    hi = cli.live_limits()[1]
    assert cli.set_wavelength(99999.0) == hi
    with pytest.raises(RuntimeError, match="filter wheel"):
        cli.set_filter(2)
    with pytest.raises(RuntimeError, match="grating"):
        cli.set_grating(9)
    r = cli._cmd({"cmd": "no_such_verb"})
    assert r["ok"] is False and "unknown" in r["error"]


def test_shutter_bool_from_text(service_and_client):
    _, cli = service_and_client
    r = cli._cmd({"cmd": "set_shutter", "open": "false"})   # text, not a bool
    assert r["ok"]
    time.sleep(0.1)
    assert _status(cli)["shutter_open"] is False
    cli.set_shutter(True)
    time.sleep(0.1)
    assert _status(cli)["shutter_open"] is True


def test_set_config_over_wire(service_and_client):
    _, cli = service_and_client
    cli.start()
    cli.cfg.gratings.g1_max_nm = 800.0
    cli.cfg.accessories.filter_wheel = True
    cli.apply_config()
    assert cli.live_limits()[1] == 800.0 or _status(cli)["wl_max_nm"] == 800.0
    ids = {p["id"] for p in cli.describe()["parameters"]}
    assert "filter" in ids


def test_shutdown_verb(service_and_client):
    svc, cli = service_and_client
    r = cli._cmd({"cmd": "shutdown"})
    assert r == {"ok": True, "stopping": True}
    assert svc._stop.is_set()
