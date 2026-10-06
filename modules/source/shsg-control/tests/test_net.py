"""End-to-end over ZeroMQ: a service (simulated generator) and a client on
loopback. Uses non-default ports so it never collides with a running service."""

import time

import pytest

from shsg.config import Config
from shsg.sim_system import build_sim_system
from shsg.net.service import ShsgService
from shsg.net.client import ShsgClient

CMD_PORT = 17650
PUB_PORT = 17651


@pytest.fixture
def service_and_client():
    cfg = Config()
    gen, sim = build_sim_system(cfg)
    gen.sim = sim
    svc = ShsgService(gen, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, status_hz=20.0)
    svc.start()
    cli = ShsgClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def test_shutdown_verb_keep_outputs(service_and_client):
    svc, cli = service_and_client
    cli.set_rf(True)
    sim = svc.gen.sim
    n = len(sim.commands)
    r = cli._cmd({"cmd": "shutdown", "keep_outputs": True})
    assert r["ok"] and r["stopping"] and r["kept_outputs"] is True
    svc.stop()                       # what serve_forever's finally does
    assert sim.commands[n:] == [] and sim.read_state()["rf_on"] is True
    assert svc.gen.status().connected is False


def test_shutdown_verb_plain_and_text_false_park(service_and_client):
    svc, cli = service_and_client
    cli.set_rf(True)
    r = cli._cmd({"cmd": "shutdown", "keep_outputs": "false"})   # gotcha #3
    assert r["kept_outputs"] is False
    svc.stop()
    assert svc.gen.sim.read_state()["rf_on"] is False


def test_info_and_config(service_and_client):
    _, cli = service_and_client
    info = cli.start()
    assert info["power_max_dBm"] == Config().limits.power_max_dBm
    assert cli.get_config().hardware.owner_cmd_port == 5587


def test_commands_take_effect(service_and_client):
    _, cli = service_and_client
    cli.set_frequency(2.5e9)
    cli.set_power(-12.0)
    cli.set_rf(True)
    # direct status query (not the cached PUB frame) is synchronous
    s = cli.status()
    # give one PUB cycle in case status() returned the cached (empty-ish) frame
    for _ in range(20):
        s = cli.status()
        if s.frequency_Hz == 2.5e9 and s.rf_on:
            break
        time.sleep(0.05)
    assert s.frequency_Hz == 2.5e9
    assert s.power_dBm == -12.0
    assert s.rf_on is True
    assert s.connected is True


def test_clamp_over_wire(service_and_client):
    _, cli = service_and_client
    cli.set_power(999.0)
    for _ in range(20):
        s = cli.status()
        if s.power_dBm == Config().limits.power_max_dBm:
            break
        time.sleep(0.05)
    assert s.power_dBm == Config().limits.power_max_dBm


def test_set_config_over_wire(service_and_client):
    _, cli = service_and_client
    cli.start()
    cli.cfg.limits.power_max_dBm = -14.0
    cli.apply_config()
    cli.set_power(-5.0)
    for _ in range(20):
        s = cli.status()
        if s.power_dBm == -14.0:
            break
        time.sleep(0.05)
    assert s.power_dBm == -14.0


def test_refusal_reaches_the_client_as_an_error(service_and_client):
    """A command refused while an SNA sweep holds the TG: the service replies
    {"ok": false} with the reason, and the client raises it (like the local
    brain does), so a GUI or script cannot mistake it for success."""
    svc, cli = service_and_client
    svc.gen.sim.simulate_sweep(True)
    with pytest.raises(RuntimeError, match="sweep"):
        cli.set_frequency(2e9)
    r = cli._cmd({"cmd": "status"})["status"]
    assert r["tg_busy"] is True and r["tg_ready"] is False
    svc.gen.sim.simulate_sweep(False)


def test_real_backend_chain_end_to_end():
    """shsg service with the REAL backend -> fake signalhound owner -> a client.
    What a scan sees: the owner's echo, tg_ready, and the owner's refusals."""
    from fake_owner import FakeOwner
    from shsg.sim_system import build_real_system
    owner = FakeOwner(17664, 17665, on=False, freq_hz=1e9, level_dbm=-20.0).start()
    cfg = Config()
    cfg.hardware.owner_cmd_port, cfg.hardware.owner_pub_port = 17664, 17665
    cfg.hardware.owner_wait_s = 3.0
    gen, _ = build_real_system(cfg)
    svc = ShsgService(gen, host="127.0.0.1", cmd_port=17666, pub_port=17667, status_hz=20.0)
    svc.start()
    cli = ShsgClient(host="127.0.0.1", cmd_port=17666, pub_port=17667, timeout_ms=3000)
    try:
        cli.set_frequency(2e9)
        cli.set_rf(True)
        for _ in range(60):
            st = cli._cmd({"cmd": "status"})["status"]
            if st["frequency_Hz"] == 2e9 and st["rf_on"]:
                break
            time.sleep(0.05)
        assert st["frequency_Hz"] == 2e9 and st["rf_on"] and st["tg_ready"]
        assert owner.state["tg_cw_on"] is True
        owner.set(tg_mode="sweep")
        time.sleep(0.3)
        with pytest.raises(RuntimeError, match="sweep"):
            cli.set_power(-12.0)
    finally:
        cli.shutdown()
        svc.stop()                               # clean stop: CW off at the owner
        owner.stop()
    # the clean stop TRIED to switch the CW off (the owner refused: its sweep
    # holds the TG, and it decides what the TG does afterwards)
    assert owner.tg_cw_requests()[-1] == {"cmd": "tg_cw", "on": False}
