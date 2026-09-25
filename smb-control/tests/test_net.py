"""End-to-end over ZeroMQ: a service (simulated generator) and a client on
loopback. Uses non-default ports so it never collides with a running service."""

import time

import pytest

from smb.config import Config
from smb.sim_system import build_sim_system
from smb.net.service import SmbService
from smb.net.client import SmbClient

CMD_PORT = 15690
PUB_PORT = 15691


@pytest.fixture
def service_and_client():
    cfg = Config()
    gen, _ = build_sim_system(cfg)
    svc = SmbService(gen, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, status_hz=20.0)
    svc.start()
    cli = SmbClient(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT, timeout_ms=2000)
    time.sleep(0.3)                 # let PUB/SUB connect so status frames flow
    yield svc, cli
    cli.shutdown()
    svc.stop()
    time.sleep(0.2)


def test_info_and_config(service_and_client):
    _, cli = service_and_client
    info = cli.start()
    assert info["power_max_dBm"] == Config().limits.power_max_dBm
    assert cli.get_config().hardware.smb_visa == "GPIB0::28::INSTR"


def test_commands_take_effect(service_and_client):
    _, cli = service_and_client
    cli.set_frequency(2.5e9)
    cli.set_power(-12.0)
    cli.set_phase(33.0)
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
    assert s.phase_deg == 33.0
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
    cli.cfg.limits.power_max_dBm = 5.0
    cli.apply_config()
    cli.set_power(50.0)
    for _ in range(20):
        s = cli.status()
        if s.power_dBm == 5.0:
            break
        time.sleep(0.05)
    assert s.power_dBm == 5.0
