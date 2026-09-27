"""One physical SR830, one service (Lukas: "the same instrument has to be
defined by the same physical address").

The real backend claims its GPIB address through hwlock before the first byte
goes out. These tests run it against a fake pyvisa (no VISA, no GPIB) with the
lock files in a temp folder (conftest sets AALTOFLOW_LOCK_DIR).
"""

import os
import subprocess
import sys
import types

import pytest

from sr830 import hwlock
from sr830.config import Config
from sr830.hwlock import HardwareBusy


class _Inst:
    """Just enough of a VISA instrument for VisaSR830.open()."""

    def __init__(self, log):
        self.timeout = None
        self.read_termination = self.write_termination = None
        self.log = log

    def write(self, cmd):
        self.log.append(cmd)

    def query(self, cmd):
        self.log.append(cmd)
        return "0\n"

    def close(self):
        pass


@pytest.fixture
def fake_pyvisa(monkeypatch):
    """A fake pyvisa module. `ctl.fail_open = True` makes open_resource raise
    (the address exists in the config but nothing answers)."""
    ctl = types.SimpleNamespace(fail_open=False, opened=[], log=[])
    mod = types.ModuleType("pyvisa")

    class RM:
        def open_resource(self, name):
            if ctl.fail_open:
                raise OSError(f"VI_ERROR_RSRC_NFOUND: {name}")
            ctl.opened.append(name)
            return _Inst(ctl.log)

        def close(self):
            pass

    mod.ResourceManager = RM
    monkeypatch.setitem(sys.modules, "pyvisa", mod)
    return ctl


def _backend(resource):
    from sr830.backends.visa_sr830 import VisaSR830
    return VisaSR830(resource)


def test_hwlock_copy_is_identical_to_the_master():
    here = os.path.dirname(os.path.abspath(hwlock.__file__))
    master = os.path.join(here, "..", "..", "..", "..", "..", "suite-common",
                          "src", "suite_common", "hwlock.py")
    if not os.path.isfile(master):
        pytest.skip("suite-common not next to this module (installed alone)")
    with open(master, "rb") as a, open(hwlock.__file__, "rb") as b:
        assert a.read() == b.read()


def test_second_open_on_the_same_address_is_refused(fake_pyvisa):
    a = _backend("GPIB0::8::INSTR")
    a.open()
    try:
        b = _backend("GPIB0::8::INSTR")
        with pytest.raises(HardwareBusy, match="sr830"):
            b.open()
        # the refused backend never reached VISA
        assert fake_pyvisa.opened == ["GPIB0::8::INSTR"]
    finally:
        a.close()


@pytest.mark.parametrize("other", ["GPIB::8", "gpib0::8::instr", "GPIB0::8"])
def test_same_address_written_differently_conflicts(fake_pyvisa, other):
    a = _backend("GPIB0::8::INSTR")
    a.open()
    try:
        with pytest.raises(HardwareBusy, match="GPIB0::8"):
            _backend(other).open()
    finally:
        a.close()


def test_a_different_address_does_not_conflict(fake_pyvisa):
    a, b = _backend("GPIB0::8::INSTR"), _backend("GPIB0::9::INSTR")
    a.open()
    b.open()
    a.close()
    b.close()


def test_close_releases_the_address(fake_pyvisa):
    a = _backend("GPIB0::8::INSTR")
    a.open()
    assert [h["module"] for h in hwlock.held()] == ["sr830"]
    a.close()
    assert hwlock.held() == []
    b = _backend("GPIB0::8::INSTR")
    b.open()                      # free again
    b.close()
    a.close()                     # closing twice is harmless


def test_a_failed_open_releases_the_address(fake_pyvisa):
    fake_pyvisa.fail_open = True
    a = _backend("GPIB0::8::INSTR")
    with pytest.raises(OSError):
        a.open()
    assert hwlock.held() == []
    fake_pyvisa.fail_open = False
    b = _backend("GPIB0::8::INSTR")
    b.open()
    b.close()


def test_a_start_refused_after_open_releases_the_address(fake_pyvisa):
    # open() succeeds but the brain cannot interpret a reply (every query of
    # the fake answers "0", and FREQ? = 0 / SENS? ... may or may not be
    # accepted) -- either way, after a failed start OR a shutdown, the address
    # must be free again.
    from sr830.lockin import DspLockIn
    li = DspLockIn(_backend("GPIB0::8::INSTR"), Config())
    li.cfg.safety.sine_min_on_stop = False
    li.cfg.safety.aux_out_zero_on_stop = False
    try:
        li.start(poll=False)
    except Exception:
        pass
    li.shutdown()
    assert hwlock.held() == []


def test_busy_start_sends_nothing_and_shutdown_touches_nothing(fake_pyvisa):
    # A service that lost the claim must not send the "outputs safe" commands
    # (SINE OUT to minimum, AUX OUT to 0 V) to an instrument it does not own.
    from sr830.lockin import DspLockIn
    holder = hwlock.claim("GPIB0::8", "another-module")
    try:
        li = DspLockIn(_backend("GPIB0::8::INSTR"), Config())
        with pytest.raises(HardwareBusy, match="another-module"):
            li.start(poll=False)
        li.shutdown()
        assert fake_pyvisa.opened == [] and fake_pyvisa.log == []
        assert li.status().connected is False
    finally:
        holder.release()


def test_the_simulator_claims_nothing():
    from sr830.sim_system import build_sim_system
    li, _ = build_sim_system(Config(), seed=1)
    li.start(poll=False)
    try:
        assert hwlock.held() == []
    finally:
        li.shutdown()
    assert hwlock.held() == []


def test_run_service_reports_busy_in_one_line(tmp_path):
    # The whole script, as the launcher starts it, with a fake pyvisa on the
    # path and the address held by "another-module": one ASCII line on stderr
    # naming the address and the holder, exit code 3, no traceback.
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "pyvisa.py").write_text(
        "class ResourceManager:\n"
        "    def open_resource(self, name):\n"
        "        raise AssertionError('must not open a busy address')\n"
        "    def close(self):\n"
        "        pass\n", encoding="utf-8")
    # conftest pointed AALTOFLOW_LOCK_DIR at a temp folder; the child inherits it
    env = dict(os.environ, PYTHONPATH=str(fake), PYTHONUNBUFFERED="1")
    holder = hwlock.claim("GPIB0::8::INSTR", "another-module")
    try:
        script = os.path.join(os.path.dirname(__file__), "..", "scripts", "run_service.py")
        r = subprocess.run([sys.executable, script, "--real", "--resource", "GPIB::8",
                            "--cmd-port", "17290", "--pub-port", "17291"],
                           env=env, capture_output=True, text=True, timeout=60)
    finally:
        holder.release()
    assert r.returncode == 3, r.stderr
    err = r.stderr.strip()
    assert "Traceback" not in err
    assert len(err.splitlines()) == 1
    assert "GPIB0::8" in err and "another-module" in err
    err.encode("ascii")
