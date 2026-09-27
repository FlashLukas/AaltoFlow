"""Lukas's rule (2026-09-27): at start the module READS the chopper and adopts
what it finds -- it never writes anything that changes the controller's state.

Two levels are checked:
  * the brain against the simulator wrapped in a recorder that FAILS on any
    setter call during start-up, with the simulated controller powered up in a
    deliberately non-default state (so adoption is really tested);
  * the real serial backend against a fake COM port, which records every line
    sent: at start only queries ("...?") may go out.
"""

import sys
import types

import pytest

from chopper.chopper import Chopper
from chopper.config import Config
from chopper.sim_system import build_sim_system


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class NoWrites:
    """Wraps a backend; any set_* call raises while `armed`."""

    def __init__(self, inner):
        self._inner = inner
        self.armed = True
        self.writes = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name.startswith("set_") and callable(attr):
            def guarded(*a, **k):
                self.writes.append((name, a))
                if self.armed:
                    raise AssertionError(f"state-changing write at start: {name}{a}")
                return attr(*a, **k)
            return guarded
        return attr


def _non_default_cfg() -> Config:
    cfg = Config()
    s = cfg.sim
    s.blade, s.ref_mode, s.output_mode = "MC1F60", "internal", "actual"
    s.frequency_Hz, s.phase_deg, s.enabled = 2500.0, 45.0, True
    s.jitter_rel = 0.0
    # A tighter safety envelope than what the controller is running at:
    # start-up must NOT clamp (that would be a write); it only adopts.
    cfg.limits.freq_max_Hz = 2000.0
    cfg.limits.phase_max_deg = 30.0
    return cfg


def test_start_issues_no_state_changing_writes_and_adopts_everything():
    cfg = _non_default_cfg()
    clock = Clock()
    _, sim = build_sim_system(cfg, clock=clock, seed=1)
    rec = NoWrites(sim)
    ch = Chopper(rec, cfg, clock=clock, simulated=True)
    ch.start(poll=False)
    for _ in range(25):                      # includes full re-reads (every 10th poll)
        clock.t += 0.1
        ch.poll_once()
    assert rec.writes == []
    s = ch.status()
    assert s.connected and s.hw_error == ""
    assert s.blade == "MC1F60" and s.ref_mode == "internal" and s.output_mode == "actual"
    assert s.setpoint_frequency_Hz == 2500.0          # outside the envelope, still adopted
    assert s.phase_deg == 45.0
    assert s.enabled is True
    assert s.locked is True                            # it was already spinning at 2500 Hz
    assert abs(s.frequency_Hz - 2500.0) < 0.5
    # the controller itself is untouched
    assert sim.get_frequency() == 2500.0 and sim.get_phase() == 45.0 and sim.get_enable()
    rec.armed = False
    ch.shutdown()
    assert rec.writes == []                            # stop_on_exit False: no write at exit either


def test_start_adopts_standby_with_harmonics_without_touching_them():
    cfg = _non_default_cfg()
    cfg.sim.enabled = False
    clock = Clock()
    _, sim = build_sim_system(cfg, clock=clock, seed=1)
    sim._nh, sim._dh = 3, 2                             # set on the front panel earlier
    rec = NoWrites(sim)
    ch = Chopper(rec, cfg, clock=clock, simulated=True)
    ch.start(poll=False)
    s = ch.status()
    assert (s.enabled, s.nharmonic, s.dharmonic) == (False, 3, 2)
    assert rec.writes == []
    rec.armed = False
    ch.shutdown()


# ---- the real backend against a fake COM port -------------------------------

class FakeSerial:
    """Answers like the MC2000B as far as the manual describes it: echo, value,
    prompt. Records every line written."""

    state = {"id": "THORLABS MC2000B v1.0.2", "blade": "4", "ref": "0", "output": "1",
             "nharmonic": "1", "dharmonic": "1", "freq": "2500", "phase": "45",
             "enable": "1", "refoutfreq": "2500", "input": "0", "verbose": "1"}
    sent: list = []

    def __init__(self, *a, **k):
        self._out = b""

    def reset_input_buffer(self):
        self._out = b""

    def write(self, data: bytes):
        line = data.decode("ascii").rstrip("\r")
        FakeSerial.sent.append(line)
        if line.endswith("?"):
            key = line[:-1]
            # verbose mode: a status line before the value, to exercise the parser
            self._out = (line + "\r" + "status: ok\r" + self.state[key] + "\r> ").encode()
        else:
            key, _, val = line.partition("=")
            self.state[key] = val
            self._out = (line + "\r> ").encode()

    def read_until(self, term):
        out, self._out = self._out, b""
        return out

    def close(self):
        pass


@pytest.fixture
def fake_serial(monkeypatch):
    FakeSerial.sent = []
    FakeSerial.state = dict(FakeSerial.state)
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=FakeSerial))
    return FakeSerial


def test_real_backend_open_and_brain_start_send_queries_only(fake_serial, monkeypatch):
    from chopper.backends import mc2000b
    monkeypatch.setattr(mc2000b.time, "sleep", lambda s: None)
    be = mc2000b.SerialMC2000B("COM99")
    ch = Chopper(be, Config(), simulated=False)
    ch.start(poll=False)
    ch.poll_once()
    writes = [ln for ln in fake_serial.sent if not ln.endswith("?")]
    assert writes == [], writes
    s = ch.status()
    assert s.idn.startswith("THORLABS MC2000B")
    assert s.blade == "MC1F60" and s.output_mode == "actual"
    assert s.setpoint_frequency_Hz == 2500.0 and s.phase_deg == 45.0 and s.enabled
    ch.shutdown()
    assert [ln for ln in fake_serial.sent if not ln.endswith("?")] == []


def test_quiet_on_open_is_the_only_opt_in_write(fake_serial, monkeypatch):
    from chopper.backends import mc2000b
    monkeypatch.setattr(mc2000b.time, "sleep", lambda s: None)
    be = mc2000b.SerialMC2000B("COM99", quiet_on_open=True)
    be.open()
    assert [ln for ln in fake_serial.sent if not ln.endswith("?")] == ["verbose=0"]
    be.close()


def test_quiet_on_open_defaults_off():
    assert Config().hardware.quiet_on_open is False
    assert Config().hardware.stop_on_exit is False
