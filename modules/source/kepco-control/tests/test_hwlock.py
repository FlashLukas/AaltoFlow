"""One physical BOP, one service (Lukas: "the same instrument has to be defined
by the same physical address").

The real backend claims its GPIB address in open() BEFORE anything goes on the
bus; a second claim -- from a second kepco, or from clMag, which drives the SAME
box -- is refused with HardwareBusy. All offline: pyvisa is replaced by a fake,
and AALTOFLOW_LOCK_DIR points the lock files at a temp folder so the tests never
touch (or collide with) the real %LOCALAPPDATA%\\AaltoFlow\\locks.
"""

import subprocess
import sys
import types
from pathlib import Path

import pytest

from kepco import hwlock
from kepco.backends.bop_gpib import VisaBOP
from kepco.config import Config
from kepco.hwlock import HardwareBusy
from kepco.sim_system import build_sim_system
from kepco.supply import BipolarSupply

REPLIES = {"*IDN?": "KEPCO,BIT 4886 20-10,E1234,2.0-1.0",
           "FUNC:MODE?": "1", "OUTP?": "0",
           "VOLT?": "1.00000E+01", "CURR?": "0.00000E+00",
           "MEAS:VOLT?": "0.00000E+00", "MEAS:CURR?": "0.00000E+00"}


class FakeInst:
    def __init__(self, replies, fail_on=None):
        self.writes = []
        self.replies = replies
        self.fail_on = fail_on
        self.closed = False

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.writes.append(cmd)
        if cmd == self.fail_on:
            raise OSError(f"VI_ERROR_TMO on {cmd}")      # a timed-out GPIB read
        return self.replies[cmd]

    def close(self):
        self.closed = True


@pytest.fixture
def visa(monkeypatch, tmp_path):
    """A fake pyvisa; records every instrument it hands out. Locks in tmp."""
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    made = []
    state = {"fail_on": None, "replies": dict(REPLIES)}

    class RM:
        def open_resource(self, name):
            inst = FakeInst(state["replies"], state["fail_on"])
            made.append(inst)
            return inst

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "pyvisa", types.SimpleNamespace(ResourceManager=RM))
    return made, state


def test_second_backend_on_the_same_address_is_refused(visa):
    made, _ = visa
    a = VisaBOP("GPIB0::6::INSTR")
    a.open()
    b = VisaBOP("GPIB0::6::INSTR")
    with pytest.raises(HardwareBusy, match="kepco") as e:
        b.open()
    assert "GPIB0::6" in str(e.value)
    # the refused backend never opened a VISA session, so it sent nothing
    assert len(made) == 1
    a.close()


@pytest.mark.parametrize("other", ["GPIB::6", "gpib0::6", "GPIB0::6", "gpib0::6::instr"])
def test_the_same_address_written_differently_still_conflicts(visa, other):
    a = VisaBOP("GPIB0::6::INSTR")
    a.open()
    with pytest.raises(HardwareBusy, match="kepco"):
        VisaBOP(other).open()
    a.close()


def test_a_different_gpib_address_does_not_conflict(visa):
    a = VisaBOP("GPIB0::6::INSTR")
    a.open()
    b = VisaBOP("GPIB0::7::INSTR")
    b.open()                                     # another box: fine
    a.close()
    b.close()


def test_close_releases_the_address(visa):
    a = VisaBOP("GPIB0::6::INSTR")
    a.open()
    assert [h["normalized"] for h in hwlock.held()] == ["GPIB0::6"]
    assert hwlock.held()[0]["module"] == "kepco"
    a.close()
    assert hwlock.held() == []
    b = VisaBOP("GPIB::6")
    b.open()                                     # free again
    b.close()


def test_a_failing_open_releases_the_address_and_writes_nothing(visa):
    made, state = visa
    state["fail_on"] = "*IDN?"                  # the unit does not answer
    a = VisaBOP("GPIB0::6::INSTR")
    with pytest.raises(OSError):
        a.open()
    assert hwlock.held() == []
    assert made[0].closed                        # VISA session closed too
    assert made[0].writes == ["*IDN?"]           # and no OUTP OFF on the way out
    state["fail_on"] = None
    b = VisaBOP("GPIB0::6::INSTR")
    b.open()
    b.close()


def test_brain_that_cannot_read_the_state_releases_without_writing(visa):
    """read_state fails after a good open: the brain refuses to start (it will
    not guess whether a coil is energised), must NOT send OUTP OFF, and must
    let go of the address so clMag could take the BOP."""
    made, state = visa
    state["fail_on"] = "FUNC:MODE?"
    supply = BipolarSupply(VisaBOP("GPIB0::6::INSTR"), Config())
    with pytest.raises(RuntimeError, match="could not read"):
        supply.start(poll=False)
    assert hwlock.held() == []
    assert "OUTP OFF" not in made[0].writes
    assert made[0].closed


def test_busy_brain_start_sends_no_safe_state_commands(visa):
    """The brain whose claim is refused must not ramp or switch off a BOP it
    never opened -- shutdown() after the failed start writes nothing."""
    made, _ = visa
    holder = VisaBOP("GPIB0::6::INSTR")
    holder.open()
    n_before = len(made)
    supply = BipolarSupply(VisaBOP("GPIB0::6::INSTR"), Config())
    with pytest.raises(HardwareBusy):
        supply.start(poll=False)
    supply.shutdown()                            # e.g. a GUI closing after the error
    assert len(made) == n_before                 # no second VISA session ever
    assert "OUTP OFF" not in made[0].writes      # the holder's BOP untouched
    holder.close()


def test_the_simulator_claims_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("AALTOFLOW_LOCK_DIR", str(tmp_path / "locks"))
    supply, _ = build_sim_system(Config())
    supply.start(poll=False)
    supply.step()
    assert hwlock.held() == []
    supply.shutdown()


def test_hwlock_is_the_master_copy():
    """Modules carry a copy of suite-common's hwlock.py (they do not depend on
    suite-common); tools/check_modules.py also compares them."""
    here = Path(hwlock.__file__)
    master = here.parents[5] / "suite-common" / "src" / "suite_common" / "hwlock.py"
    if not master.is_file():
        pytest.skip("installed without suite-common next to it")
    assert here.read_bytes() == master.read_bytes()


def test_service_exits_cleanly_when_the_bop_is_busy(visa, tmp_path):
    """run_service.py --real against a claimed address: one line on stderr
    naming address + holder, exit code 3, no traceback. The address is held
    by THIS process; the child finds it busy through the lock file. pyvisa is
    never reached (the claim comes first), so no VISA is needed."""
    holder = VisaBOP("GPIB0::6::INSTR")
    holder.open()
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_service.py"
    import os
    env = dict(os.environ)
    r = subprocess.run([sys.executable, str(script), "--real", "--visa", "GPIB::6",
                        "--cmd-port", "17018", "--pub-port", "17019"],
                       capture_output=True, text=True, timeout=60, env=env)
    holder.close()
    assert r.returncode == 3, r.stderr
    assert "Traceback" not in r.stderr
    err = r.stderr.strip().splitlines()
    assert len(err) == 1
    assert "GPIB0::6" in err[0] and "kepco" in err[0] and "pid" in err[0]
