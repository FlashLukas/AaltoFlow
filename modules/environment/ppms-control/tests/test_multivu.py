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


class StrictClient(FakeClient):
    """Fails the test on ANY state-changing call: only queries are allowed
    while the service starts (adopt-on-start rule, 2026-09-27)."""

    def get_field_setpoints(self):
        return 25000.0, 30.0, "oscillate", "driven"      # 2.5 T at 3 mT/s

    def get_temperature_setpoints(self):
        return 10.0, 2.5, "no_overshoot"

    def set_field(self, *a, **k):
        raise AssertionError("set_field during start-up")

    def set_temperature(self, *a, **k):
        raise AssertionError("set_temperature during start-up")

    def __getattr__(self, name):                        # purge/seal/vent/... too
        if name.startswith(("set_", "purge", "seal", "vent", "wait")):
            raise AssertionError(f"{name} during start-up")
        raise AttributeError(name)


class StrictMpv(FakeMpv):
    Client = StrictClient


def test_start_issues_only_queries_and_adopts_multivus_state():
    cfg = Config()
    cryo = Cryostat(MultiVuDynaCool(cfg, mpv=StrictMpv), cfg)
    cryo.start(poll=False)
    try:
        s = cryo.status()
        assert s.setpoint_field_mT == 2500.0                     # 25000 Oe
        assert s.field_rate_mT_per_s == 3.0 and s.field_approach == "oscillate"
        assert s.setpoint_temperature_K == 10.0
        assert s.temperature_rate_K_per_min == 2.5
        assert s.temperature_approach == "no_overshoot"
        assert FakeClient.instances[0].calls == ["open"]
    finally:
        cryo.shutdown()
    assert FakeClient.instances[0].calls == ["open", "close_client"]


# ---- one DynaCool, one service (hwlock) ---------------------------------------
# Lukas's rule: the same instrument is defined by the same physical address, and
# two services must never drive it at once. For this module the address is "the
# MultiVu on this PC" (multivu.MULTIVU_ADDRESS). Locks go to a temp folder.

from ppms import hwlock
from ppms.backends import multivu as mv_mod
from ppms.sim_system import build_sim_system


@pytest.fixture
def lockdir(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path))
    return tmp_path


def test_second_real_backend_is_refused_before_touching_multivu(lockdir):
    a = MultiVuDynaCool(Config(), mpv=FakeMpv)
    a.open()
    b = MultiVuDynaCool(Config(), mpv=FakeMpv)
    with pytest.raises(hwlock.HardwareBusy, match="ppms"):
        b.open()
    # The refused one never reached MultiPyVu: still ONE server, ONE client.
    assert len(FakeServer.instances) == 1 and len(FakeClient.instances) == 1
    a.close()


def test_other_flavor_and_spelling_is_the_same_cryostat(lockdir):
    # A second service with another flavor / MultiPyVu port still reaches the
    # same MultiVu -> must conflict. And another module claiming "multivu"
    # (lower case) is the same address after normalisation.
    a = MultiVuDynaCool(Config(), mpv=FakeMpv)
    a.open()
    cfg = Config()
    cfg.hardware.flavor, cfg.hardware.mpv_port = "", cfg.hardware.mpv_port + 1
    with pytest.raises(hwlock.HardwareBusy):
        MultiVuDynaCool(cfg, mpv=FakeMpv).open()
    with pytest.raises(hwlock.HardwareBusy, match="ppms"):
        hwlock.claim("multivu", "othermodule", wait_s=0.1)
    assert hwlock.normalize("multivu") == hwlock.normalize(mv_mod.MULTIVU_ADDRESS)
    a.close()


def test_close_releases_the_claim(lockdir):
    a = MultiVuDynaCool(Config(), mpv=FakeMpv)
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["ppms"]
    a.close()
    assert hwlock.held() == []
    b = MultiVuDynaCool(Config(), mpv=FakeMpv)
    b.open()                                   # free again
    b.close()


def test_failed_open_releases_the_claim(lockdir):
    FakeServer.exit_on_init = True             # MultiVu not running
    with pytest.raises(RuntimeError):
        MultiVuDynaCool(Config(), mpv=FakeMpv).open()
    assert hwlock.held() == []
    FakeServer.exit_on_init = False
    b = MultiVuDynaCool(Config(), mpv=FakeMpv)
    b.open()
    b.close()


def test_failed_client_open_releases_the_claim(lockdir, monkeypatch):
    def boom(self):
        raise ConnectionError("no server")
    monkeypatch.setattr(FakeClient, "open", boom)
    with pytest.raises(ConnectionError):
        MultiVuDynaCool(Config(), mpv=FakeMpv).open()
    assert hwlock.held() == []


def test_busy_cryostat_start_sends_nothing(lockdir):
    # The brain path the service uses: the refused start must not send any
    # "safe state" to a cryostat it does not own, and shutdown after it is harmless.
    holder = hwlock.claim(mv_mod.MULTIVU_ADDRESS, "ppms")
    try:
        cryo = Cryostat(MultiVuDynaCool(Config(), mpv=FakeMpv), Config())
        with pytest.raises(hwlock.HardwareBusy):
            cryo.start(poll=False)
        cryo.shutdown()
        assert FakeClient.instances == [] and FakeServer.instances == []
    finally:
        holder.release()


def test_scaffolding_and_sim_claim_nothing(lockdir):
    cfg = Config()
    cfg.hardware.scaffolding = True            # MultiPyVu's own simulation
    a = MultiVuDynaCool(cfg, mpv=FakeMpv)
    a.open()
    assert hwlock.held() == []
    a.close()
    cryo, _ = build_sim_system(Config())
    cryo.start(poll=False)
    assert hwlock.held() == []
    cryo.shutdown()
