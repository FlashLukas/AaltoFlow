"""The real backend against a FAKE MultiPyVu module -- no MultiVu, no network.

What is pinned here is what would be expensive to find out on the cryostat:
  * the unit conversion (MultiVu speaks Oe, the suite mT: 1 mT = 10 Oe, both
    ways, rates too);
  * the MultiPyVu server is bound to 127.0.0.1, never 0.0.0.0;
  * MultiPyVu's sys.exit() on a failed attach becomes an ordinary error
    instead of silently ending the service;
  * close() never sends a setpoint.
The fake follows MultiPyVu 3.6.1's signatures (read from its source).
"""

from enum import IntEnum

import pytest

from ppms.backends.multivu import MultiVuDynaCool
from ppms.config import Config
from ppms.cryostat import Cryostat


class _FieldApproach(IntEnum):
    linear = 0
    no_overshoot = 1
    oscillate = 2


class _TempApproach(IntEnum):
    fast_settle = 0
    no_overshoot = 1


class _Adapter:
    def __init__(self, approach):
        self.approach_mode = approach


class FakeClient:
    instances = []

    def __init__(self, host="localhost", port=5000):
        self.address = (host, port)
        self.calls = []
        self.instrument_name = "DYNACOOL"
        self.field = _Adapter(_FieldApproach)
        self.temperature = _Adapter(_TempApproach)
        self.h_Oe, self.h_status = 1234.0, "Holding (driven)"
        self.T, self.T_status = 4.2, "Stable"
        FakeClient.instances.append(self)

    def open(self):
        self.calls.append("open")
        return self

    def close_client(self):
        self.calls.append("close_client")

    def get_field(self):
        return self.h_Oe, self.h_status

    def get_field_setpoints(self):
        return 1000.0, 50.0, "linear", "driven"

    def set_field(self, set_point, rate_per_sec, approach_mode, driven_mode=None):
        self.calls.append(("set_field", set_point, rate_per_sec, approach_mode, driven_mode))

    def get_temperature(self):
        return self.T, self.T_status

    def get_temperature_setpoints(self):
        return 4.2, 10.0, "fast_settle"

    def set_temperature(self, set_point, rate_per_min, approach_mode):
        self.calls.append(("set_temperature", set_point, rate_per_min, approach_mode))

    def get_chamber(self):
        return "Purged and Sealed"


class FakeServer:
    instances = []
    exit_on_init = False

    def __init__(self, flags=[], host="0.0.0.0", port=5000, keep_server_open=False):
        if FakeServer.exit_on_init:
            raise SystemExit(0)               # what MultiPyVu does on a failed attach
        self.flags, self.host, self.port = list(flags), host, port
        self.opened = self.closed = False
        FakeServer.instances.append(self)

    def open(self):
        self.opened = True
        return self

    def close(self):
        self.closed = True


class FakeMpv:
    __version__ = "3.6.1"
    Server = FakeServer
    Client = FakeClient


@pytest.fixture(autouse=True)
def _reset():
    FakeServer.instances.clear()
    FakeClient.instances.clear()
    FakeServer.exit_on_init = False


def test_open_binds_localhost_and_passes_the_flavor():
    cfg = Config()
    be = MultiVuDynaCool(cfg, mpv=FakeMpv)
    be.open()
    srv = FakeServer.instances[0]
    assert srv.host == "127.0.0.1", "MultiPyVu must not listen on the network"
    assert srv.port == cfg.hardware.mpv_port and srv.opened
    assert srv.flags == ["DYNACOOL"]
    cli = FakeClient.instances[0]
    assert cli.address == ("127.0.0.1", cfg.hardware.mpv_port)
    assert "DYNACOOL" in be.idn() and "3.6.1" in be.idn()


def test_scaffolding_flag():
    cfg = Config()
    cfg.hardware.scaffolding = True
    be = MultiVuDynaCool(cfg, mpv=FakeMpv)
    be.open()
    assert FakeServer.instances[0].flags == ["DYNACOOL", "-s"]
    assert "simulated by MultiPyVu" in be.idn()


def test_oersted_to_millitesla_both_ways():
    be = MultiVuDynaCool(Config(), mpv=FakeMpv)
    be.open()
    cli = FakeClient.instances[0]
    assert be.read_field() == (123.4, "Holding (driven)")           # 1234 Oe
    assert be.read_field_setpoint() == (100.0, 5.0, "linear")       # 1000 Oe, 50 Oe/s
    be.set_field(250.0, 2.2, "oscillate")
    name, h, rate, mode, driven = cli.calls[-1]
    assert (name, h, rate) == ("set_field", 2500.0, 22.0)
    assert mode is _FieldApproach.oscillate
    assert driven is None, "the DynaCool is driven-only; no driven_mode is sent"


def test_temperature_passes_through_in_kelvin():
    be = MultiVuDynaCool(Config(), mpv=FakeMpv)
    be.open()
    cli = FakeClient.instances[0]
    assert be.read_temperature() == (4.2, "Stable")
    assert be.read_temperature_setpoint() == (4.2, 10.0, "fast_settle")
    be.set_temperature(10.0, 2.0, "no_overshoot")
    assert cli.calls[-1] == ("set_temperature", 10.0, 2.0, _TempApproach.no_overshoot)
    assert be.read_chamber() == "Purged and Sealed"


def test_unknown_approach_is_a_bad_request_not_a_command():
    be = MultiVuDynaCool(Config(), mpv=FakeMpv)
    be.open()
    with pytest.raises(KeyError):
        be.set_field(1.0, 1.0, "persistent")
    assert not any(isinstance(c, tuple) for c in FakeClient.instances[0].calls)


def test_a_failed_attach_is_an_error_not_an_exit():
    FakeServer.exit_on_init = True
    be = MultiVuDynaCool(Config(), mpv=FakeMpv)
    with pytest.raises(RuntimeError, match="MultiVu"):
        be.open()


def test_close_sends_no_setpoint_and_stops_the_server():
    be = MultiVuDynaCool(Config(), mpv=FakeMpv)
    be.open()
    cli, srv = FakeClient.instances[0], FakeServer.instances[0]
    be.close()
    assert cli.calls == ["open", "close_client"]
    assert srv.closed
    be.close()                                # twice is fine
    with pytest.raises(RuntimeError):
        be.read_field()


def test_the_brain_on_the_real_backend_adopts_in_millitesla():
    cfg = Config()
    cryo = Cryostat(MultiVuDynaCool(cfg, mpv=FakeMpv), cfg)
    cryo.start(poll=False)
    try:
        s = cryo.status()
        assert s.setpoint_field_mT == 100.0 and s.measured_field_mT == 123.4
        assert s.setpoint_temperature_K == 4.2 and s.simulated is False
        assert [c for c in FakeClient.instances[0].calls if isinstance(c, tuple)] == []
    finally:
        cryo.shutdown()
