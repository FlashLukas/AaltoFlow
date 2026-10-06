"""End-to-end over ZeroMQ: a service (simulated laser) and a client on
loopback. Uses this module's own test ports (17340..17359) so it never collides
with a running service or with a sibling module's tests."""

import time

import pytest

from superk.config import Config
from superk.sim_system import build_sim_system
from superk.net.service import SuperkService
from superk.net.client import SuperkClient

CMD_PORT = 17342
PUB_PORT = 17343


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def rig():
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.2
    cfg.hardware.poll_hz = 20.0
    laser, backend = build_sim_system(cfg)
    svc = SuperkService(laser, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT,
                        status_hz=20.0)
    svc.start()
    cli = SuperkClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli, backend
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def test_info_and_config(rig):
    _, cli, _ = rig
    info = cli.start()
    assert info["power_max_pct"] == Config().limits.power_max_pct
    assert info["filters"] == ["VIS-nIR", "nIR2", "IR"]
    assert cli.get_config().hardware.extreme_addr == 15


def test_commands_take_effect(rig):
    _, cli, _ = rig
    cli.set_power(25.0)
    cli.set_filter("nIR2")
    cli.set_line(1, 1064.0, 70.0)
    cli.set_amplitude(3, 40.0)
    cli.set_rf(True)
    cli.set_emission(True)
    assert wait_for(lambda: cli.status().emission_on)
    s = cli.status()
    assert s.power_pct == 25.0
    assert s.filter == "nIR2"
    assert s.wavelength_nm[0] == 1064.0 and s.amplitude_pct[0] == 70.0
    assert s.amplitude_pct[2] == 40.0
    assert s.rf_on is True and s.connected is True


def test_emission_refused_over_the_wire(rig):
    _, cli, backend = rig
    backend.open_interlock()
    r = cli._cmd({"cmd": "emission_on"})
    assert r["ok"] is False and "interlock" in r["error"]
    with pytest.raises(ValueError):
        cli.set_emission(True)


def test_string_off_is_off(rig):
    """bool("off") is True; the service parses a hand-typed string."""
    _, cli, backend = rig
    cli.set_rf(True)
    r = cli._cmd({"cmd": "set_rf", "on": "off"})
    assert r["ok"]
    assert backend.read_rf() is False


def test_bad_requests_get_an_error_not_a_crash(rig):
    _, cli, _ = rig
    assert cli._cmd({"cmd": "set_wavelength", "line": 9, "wavelength_nm": 600})["ok"] is False
    assert cli._cmd({"cmd": "set_power"})["ok"] is False
    assert cli._cmd({"cmd": "no_such_verb"})["ok"] is False
    assert cli._cmd({"cmd": "status"})["ok"] is True          # still alive


def test_set_config_over_wire(rig):
    _, cli, _ = rig
    cli.start()
    cli.cfg.limits.power_max_pct = 15.0
    cli.apply_config()
    cli.set_power(40.0)
    assert wait_for(lambda: cli.status().power_pct == 15.0)


def test_shutdown_verb_switches_the_laser_off():
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.0
    laser, backend = build_sim_system(cfg)
    svc = SuperkService(laser, host="127.0.0.1", cmd_port=17344, pub_port=17345)
    import threading
    t = threading.Thread(target=svc.serve_forever, daemon=True)
    t.start()
    cli = SuperkClient(host="127.0.0.1", cmd_port=17344, pub_port=17345, timeout_ms=2000)
    try:
        assert wait_for(lambda: cli._cmd({"cmd": "status"}).get("ok"))
        cli.set_rf(True)
        cli.set_emission(True)
        assert wait_for(backend.read_emission)
        assert cli._cmd({"cmd": "shutdown"})["ok"]
        t.join(10)
        assert not t.is_alive()
        assert backend.read_emission() is False and backend.read_rf() is False
    finally:
        cli.shutdown()


def _serve(cfg):
    import threading
    laser, backend = build_sim_system(cfg)
    svc = SuperkService(laser, host="127.0.0.1", cmd_port=17344, pub_port=17345)
    t = threading.Thread(target=svc.serve_forever, daemon=True)
    t.start()
    cli = SuperkClient(host="127.0.0.1", cmd_port=17344, pub_port=17345, timeout_ms=2000)
    return backend, t, cli


def test_shutdown_verb_keep_outputs_is_a_restart():
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.0
    backend, t, cli = _serve(cfg)
    try:
        assert wait_for(lambda: cli._cmd({"cmd": "status"}).get("ok"))
        cli.set_rf(True)
        cli.set_emission(True)
        assert wait_for(backend.read_emission)
        n = len(backend.writes)
        r = cli._cmd({"cmd": "shutdown", "keep_outputs": True})
        assert r["ok"] and r["kept_outputs"] is True
        t.join(10)
        assert not t.is_alive()
        assert not [w for w in backend.writes[n:] if w[0] in ("set_emission", "set_rf")]
        assert backend.read_emission() is True and backend.read_rf() is True
        assert backend._open is False
    finally:
        cli.shutdown()


def test_shutdown_verb_keep_outputs_text_false_is_false():
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.0
    backend, t, cli = _serve(cfg)
    try:
        assert wait_for(lambda: cli._cmd({"cmd": "status"}).get("ok"))
        cli.set_rf(True)
        cli.set_emission(True)
        assert wait_for(backend.read_emission)
        r = cli._cmd({"cmd": "shutdown", "keep_outputs": "false"})   # gotcha #3
        assert r["kept_outputs"] is False
        t.join(10)
        assert backend.read_emission() is False and backend.read_rf() is False
    finally:
        cli.shutdown()


def test_lost_client_guard_over_the_wire():
    """A remote GUI's client owns the emission it switched on and pings; when
    it goes away, the service switches emission off. A raw client (like
    scan-core) sends no owner, so its emission is never cut."""
    cfg = Config()
    cfg.hardware.sim_warmup_s = 0.0
    cfg.hardware.poll_hz = 20.0
    cfg.hardware.client_timeout_s = 0.8
    laser, backend = build_sim_system(cfg)
    svc = SuperkService(laser, host="127.0.0.1", cmd_port=17346, pub_port=17347,
                        status_hz=20.0)
    svc.start()
    gui = SuperkClient(host="127.0.0.1", cmd_port=17346, pub_port=17347,
                       timeout_ms=2000, ping_s=0.2)
    raw = SuperkClient(host="127.0.0.1", cmd_port=17346, pub_port=17347,
                       timeout_ms=2000, ping_s=0.2)
    try:
        gui.set_emission(True)
        assert wait_for(lambda: gui.status().emission_guarded)
        time.sleep(2.0)                        # 2.5x the timeout, pings flowing
        assert gui.status().emission_set is True
        gui.shutdown()                         # the GUI crashes / closes
        assert wait_for(lambda: backend.read_emission() is False, 5.0)
        # scan-core style: a plain command with no owner -> no guard
        assert raw._cmd({"cmd": "emission_on"})["ok"]
        assert wait_for(backend.read_emission)
        time.sleep(2.0)
        assert backend.read_emission() is True
        assert raw.status().emission_guarded is False
    finally:
        raw.shutdown()
        svc.stop()
        time.sleep(0.2)
