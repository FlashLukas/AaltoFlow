"""One physical AG-UC2 = one service (hwlock.py), offline.

Lukas's rule: the same instrument is defined by the same physical address. The
real driver claims its COM port in open() BEFORE the first byte goes out, so a
second service pointed at the same controller is refused with a message naming
the holder. Uses the fake serial port of test_real_backend and a private lock
folder (AALTOFLOW_LOCK_DIR), so a running service on this PC is never touched.
"""

import pytest

from agilis import hwlock
from agilis.backends.ag_uc2 import AgUC2
from agilis.config import Config
from agilis.hwlock import HardwareBusy

from test_real_backend import fake_serial  # noqa: F401  (pytest fixture)


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


def _dev(port="COM9"):
    cfg = Config()
    cfg.hardware.port = port
    return AgUC2(cfg)


def test_second_open_on_same_port_is_refused(fake_serial):  # noqa: F811
    a = _dev("COM9")
    a.open()
    n_ports = len(fake_serial)
    b = _dev("COM9")
    with pytest.raises(HardwareBusy, match="agilis") as ei:
        b.open()
    assert "COM9" in str(ei.value)
    # the refused one never opened the port, so it sent nothing at all
    assert len(fake_serial) == n_ports
    assert [h["module"] for h in hwlock.held()] == ["agilis"]
    a.close()


@pytest.mark.parametrize("first, second", [("COM5", "com5"), ("COM5", "ASRL5::INSTR"),
                                           ("\\\\.\\COM5", "COM5")])  # Win32 device path
def test_same_port_written_differently_conflicts(fake_serial, first, second):  # noqa: F811
    a = _dev(first)
    a.open()
    try:
        with pytest.raises(HardwareBusy, match="COM5"):
            _dev(second).open()
    finally:
        a.close()


def test_other_port_is_independent(fake_serial):  # noqa: F811
    a, b = _dev("COM5"), _dev("COM6")
    a.open()
    b.open()
    assert sorted(h["normalized"] for h in hwlock.held()) == ["COM5", "COM6"]
    a.close()
    b.close()


def test_close_releases(fake_serial):  # noqa: F811
    a = _dev()
    a.open()
    a.close()
    assert hwlock.held() == []
    b = _dev()
    b.open()                    # succeeds now
    b.close()
    a.close()                   # a second close is harmless


def test_failing_open_releases(fake_serial, monkeypatch):  # noqa: F811
    # VE unanswered -> TimeoutError inside open(): the claim must go with it
    import sys
    mod = sys.modules["serial"]
    real = mod.Serial

    def mute(**kw):
        s = real(**kw)
        s.write = lambda data: None          # swallow everything, answer nothing
        return s
    monkeypatch.setattr(mod, "Serial", mute)
    with pytest.raises(TimeoutError):
        _dev().open()
    assert hwlock.held() == []

    # the port itself refusing to open (wrong COM, vendor applet holds it)
    def boom(**kw):
        raise OSError("could not open port")
    monkeypatch.setattr(mod, "Serial", boom)
    with pytest.raises(OSError):
        _dev().open()
    assert hwlock.held() == []

    monkeypatch.setattr(mod, "Serial", real)
    d = _dev()
    d.open()                    # nothing left claimed
    d.close()


def test_failed_brain_start_releases(fake_serial, monkeypatch):  # noqa: F811
    """brain.start closes the backend when adoption fails -> claim released."""
    from agilis.agilis import AgilisStage
    d = _dev()
    brain = AgilisStage(d, d.cfg)
    monkeypatch.setattr(brain, "_adopt_start", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        brain.start()
    assert hwlock.held() == []


def test_sim_claims_nothing():
    from agilis.sim_system import build_sim_system
    cfg = Config()
    cfg.hardware.port = "COM9"              # even with a port configured
    brain, _sim = build_sim_system(cfg)
    brain.start()
    try:
        assert hwlock.held() == []
    finally:
        brain.shutdown()
    assert hwlock.held() == []
