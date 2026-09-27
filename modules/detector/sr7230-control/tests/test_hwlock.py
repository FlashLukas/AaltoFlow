"""One 7230, one service: the hardware lock (hwlock.py) in the real backend.

Lukas's rule: "the same instrument has to be defined by the same physical
address." For the 7230 that address is its IP address. These tests check that
the real backend claims it BEFORE sending anything, gives it back on close()
and on a failed open(), and that the simulator never claims anything.

All offline: AALTOFLOW_LOCK_DIR points the lock files at a temp folder, and the
instrument is a stub transport (or the fake 7230 server from
test_tcp_backend.py) -- nothing reaches a network.
"""

import os
import subprocess
import sys

import pytest

from sr7230 import hwlock
from sr7230.backends.tcp7230 import Tcp7230, InstrumentError
from sr7230.config import Config
from sr7230.hwlock import HardwareBusy
from sr7230.lockin import LockIn
from sr7230.sim_system import build_sim_system

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path))
    return tmp_path


class StubTransport:
    """Answers like a 7230 on port 50000 (text, status 'complete', no overload)
    and logs every command, so a test can prove nothing was sent."""

    def __init__(self, ident="7230", fail_open=False):
        self.log: list[str] = []
        self.ident = ident
        self.fail_open = fail_open
        self.is_open = False

    def open(self):
        if self.fail_open:
            raise ConnectionRefusedError("nobody at that address")
        self.is_open = True

    def close(self):
        self.is_open = False

    def query(self, cmd, timeout_s=None):
        self.log.append(cmd)
        return {"ID": self.ident, "VER": "2.20", "REFMODE": "0"}.get(cmd, "0"), 1, 0


def _dev(address, **kw):
    t = StubTransport(**kw)
    return Tcp7230(address, transport=t), t


def test_second_open_on_the_same_address_is_refused_and_names_the_holder():
    a, _ = _dev("10.0.0.5")
    a.open()
    b, tb = _dev("10.0.0.5")
    with pytest.raises(HardwareBusy, match="sr7230"):
        b.open()
    assert tb.log == [] and not tb.is_open      # not one byte to a box we do not own
    a.close()


@pytest.mark.parametrize("other", ["10.0.0.5:50000", "TCPIP0::10.0.0.5::50000::SOCKET",
                                   "TCPIP::10.0.0.5::inst0::INSTR", " 10.0.0.5 "])
def test_the_same_box_written_differently_still_conflicts(other):
    a, _ = _dev("10.0.0.5")
    a.open()
    b, _ = _dev(other)
    with pytest.raises(HardwareBusy):
        b.open()
    a.close()


def test_a_different_box_is_not_blocked():
    a, _ = _dev("10.0.0.5")
    b, _ = _dev("10.0.0.6")
    a.open()
    b.open()
    assert len(hwlock.held()) == 2
    a.close()
    b.close()


def test_close_releases_the_address():
    a, _ = _dev("10.0.0.5")
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["sr7230"]
    a.close()
    assert hwlock.held() == []
    b, _ = _dev("10.0.0.5")
    b.open()                                    # free again
    b.close()


@pytest.mark.parametrize("kw", [{"fail_open": True},        # nothing answers
                                {"ident": "7265"}])          # not a 7230
def test_a_failed_open_releases_the_address(kw):
    a, _ = _dev("10.0.0.5", **kw)
    with pytest.raises((ConnectionError, InstrumentError)):
        a.open()
    assert hwlock.held() == []
    b, _ = _dev("10.0.0.5")
    b.open()
    b.close()


def test_a_real_socket_claims_the_host(tmp_path):
    # the default transport, against the fake 7230 server on loopback
    from test_tcp_backend import Fake7230, PORT
    fake = Fake7230()
    try:
        d = Tcp7230("127.0.0.1", port=PORT, timeout_s=2.0)
        d.open()
        assert hwlock.held()[0]["normalized"] == "TCPIP::127.0.0.1"
        with pytest.raises(HardwareBusy):
            Tcp7230("127.0.0.1", port=50001).open()
        d.close()
        assert hwlock.held() == []
    finally:
        fake.close()


def test_the_simulator_claims_nothing():
    lockin, _ = build_sim_system(Config())
    lockin.start(poll=False)
    try:
        assert hwlock.held() == []
    finally:
        lockin.shutdown()
    assert hwlock.held() == []


def test_a_busy_start_sends_nothing_even_on_shutdown():
    # The brain must not "make OSC OUT safe" on a box another service drives.
    holder = hwlock.claim("10.0.0.5", "another")
    try:
        dev, t = _dev("10.0.0.5")
        cfg = Config()
        cfg.hardware.osc_off_on_shutdown = True
        lockin = LockIn(dev, cfg)
        with pytest.raises(HardwareBusy, match="another"):
            lockin.start(poll=False)
        lockin.shutdown()
        assert t.log == []
    finally:
        holder.release()


def test_run_service_ends_with_one_clean_line_when_busy(lock_dir):
    holder = hwlock.claim("10.0.0.99", "kepco")      # any holder, this process
    try:
        env = dict(os.environ, AALTOFLOW_LOCK_DIR=str(lock_dir))
        p = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "scripts", "run_service.py"), "--real",
             "--address", "10.0.0.99", "--cmd-port", "17241", "--pub-port", "17242"],
            capture_output=True, text=True, timeout=60, env=env, cwd=_ROOT)
    finally:
        holder.release()
    assert p.returncode == 3
    err = p.stderr.strip()
    assert "Traceback" not in err and len(err.splitlines()) == 1, err
    assert "TCPIP::10.0.0.99" in err and "kepco" in err
    assert err.isascii()
