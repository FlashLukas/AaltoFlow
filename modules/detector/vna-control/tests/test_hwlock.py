"""One analyser, one service (Lukas's rule, 2026-09-27: "the same instrument
has to be defined by the same physical address").

The real backends claim the analyser's address in vna.hwlock before the first
byte; these tests drive them against the FAKE analysers (fake_visa, fake_cmt)
in a private lock folder (conftest sets AALTOFLOW_LOCK_DIR). They prove the
claim, not the instrument.
"""

from pathlib import Path

import pytest

from fake_cmt import FakeCmt
from fake_visa import FakePna
from vna import hwlock
from vna.backends.claim import lock_address
from vna.backends.cmt import CmtVna
from vna.backends.pna import PnaVna
from vna.config import Config
from vna.sim_system import build_sim_system


def _pna(address="N5222A"):
    cfg = Config()
    cfg.hardware.visa_resource = address
    fake = FakePna()
    return PnaVna(cfg, resource=fake, sleep=lambda s: None), fake


def _cmt(address="TCPIP0::127.0.0.1::5025::SOCKET"):
    cfg = Config()
    cfg.hardware.driver = "cmt"
    cfg.hardware.cmt_resource = address
    fake = FakeCmt()
    return CmtVna(cfg, resource=fake, sleep=lambda s: None), fake


def test_the_hwlock_copy_is_the_master_byte_for_byte():
    root = Path(__file__).resolve().parents[4]
    master = root / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("suite-common not next to this module (installed on its own)")
    import vna.hwlock as mine
    assert Path(mine.__file__).read_bytes() == master.read_bytes()


@pytest.mark.parametrize("make", [_pna, _cmt])
def test_a_second_backend_on_the_same_analyser_is_refused(make):
    a, _ = make()
    a.open()
    b, fake_b = make()
    with pytest.raises(hwlock.HardwareBusy, match=r"in use by vna \(pid \d+\)"):
        b.open()
    assert fake_b.log == [], "the refused backend must not send a single byte"
    a.close()


@pytest.mark.parametrize("first, second", [
    ("GPIB::16", "GPIB0::16::INSTR"),
    ("TCPIP0::10.0.0.5::hislip0::INSTR", "10.0.0.5"),
    ("tcpip0::10.0.0.5::inst0::INSTR", "TCPIP::10.0.0.5::5025::SOCKET"),
])
def test_one_pna_written_two_ways_is_still_one_pna(first, second):
    a, _ = _pna(first)
    a.open()
    b, fake_b = _pna(second)
    with pytest.raises(hwlock.HardwareBusy, match="vna"):
        b.open()
    assert fake_b.log == []
    a.close()


def test_one_s2vna_socket_written_two_ways_is_still_one_c1209():
    a, _ = _cmt("TCPIP0::127.0.0.1::5025::SOCKET")
    a.open()
    b, fake_b = _cmt("TCPIP::localhost::5025::SOCKET")
    with pytest.raises(hwlock.HardwareBusy, match="vna"):
        b.open()
    assert fake_b.log == []
    a.close()


def test_a_loopback_socket_is_keyed_by_its_port_not_by_this_pc():
    # 127.0.0.1 names THIS PC, not the analyser: S2VNA on 5025 and another
    # local server (MultiPyVu, a second S2VNA) are different instruments
    assert lock_address("TCPIP0::127.0.0.1::5025::SOCKET") == "LOCALHOST:5025"
    assert lock_address("TCPIP0::localhost::5025::SOCKET") == "LOCALHOST:5025"
    assert lock_address("127.0.0.1:5025") == "LOCALHOST:5025"
    a, _ = _cmt("TCPIP0::127.0.0.1::5025::SOCKET")
    b, _ = _cmt("TCPIP0::127.0.0.1::5026::SOCKET")
    a.open()
    b.open()                                  # a different local port: no conflict
    held = {h["normalized"] for h in hwlock.held()}
    assert held == {"LOCALHOST:5025", "LOCALHOST:5026"}
    a.close()
    b.close()
    assert hwlock.held() == []


def test_a_visa_alias_is_claimed_as_the_address_it_resolves_to():
    class Info:
        resource_name = "TCPIP0::10.0.0.5::hislip0::INSTR"

    class Rm:
        def resource_info(self, name):
            assert name == "N5222A"
            return Info()

    assert lock_address("N5222A", Rm()) == "TCPIP0::10.0.0.5::hislip0::INSTR"

    class NoAliases:                          # pyvisa-py: no alias table
        def resource_info(self, name):
            raise ValueError("unknown")

    assert lock_address("N5222A", NoAliases()) == "N5222A"


@pytest.mark.parametrize("make", [_pna, _cmt])
def test_close_releases_the_analyser(make):
    a, _ = make()
    a.open()
    assert len(hwlock.held()) == 1 and hwlock.held()[0]["module"] == "vna"
    a.close()
    assert hwlock.held() == []
    b, _ = make()
    b.open()                                  # free again
    b.close()


@pytest.mark.parametrize("make", [_pna, _cmt])
def test_a_failed_open_releases_the_analyser(make):
    a, fake = make()

    def dead(cmd):
        raise RuntimeError("VI_ERROR_TMO: the analyser does not answer")

    fake.query = dead
    with pytest.raises(RuntimeError, match="VI_ERROR_TMO"):
        a.open()
    assert hwlock.held() == [], "a failed open must not leave the analyser claimed"
    b, _ = make()
    b.open()
    b.close()


def test_a_close_after_a_refused_open_sends_nothing_to_the_analyser():
    a, _ = _pna()
    a.open()
    b, fake_b = _pna()
    with pytest.raises(hwlock.HardwareBusy):
        b.open()
    b.close()                                 # the service's shutdown path
    assert fake_b.log == [], "no ABOR / hand-back to an analyser we never owned"
    assert len(hwlock.held()) == 1, "b's close must not release a's claim"
    a.close()


def test_the_simulator_claims_nothing():
    cfg = Config()
    cfg.field.source = "manual"
    vna = build_sim_system(cfg, realtime=False, seed=1)[0]
    vna.start(run=False)
    try:
        vna.step()
        assert hwlock.held() == []
    finally:
        vna.shutdown()
    assert hwlock.held() == []
