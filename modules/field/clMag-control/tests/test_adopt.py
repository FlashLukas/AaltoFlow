"""Adopt-on-start (Lukas, 2026-09-27): starting the service READS the
instruments and adopts their state; it writes nothing that changes them.

The simulated instruments start in a deliberately NON-default state (supply
output on at 1.2 A, a DO line high), so "adopted" and "reset to defaults"
cannot be confused. A recording wrapper around the supply fails the test on
any write made before the first user command.
"""

import time

import pytest

from clMag.config import Config
from clMag.sim_system import build_sim_system


class StrictKepco:
    """Wraps a SimulatedKepco, logs every call, and refuses writes until the
    test arms it -- so a start-up write fails loudly instead of being lost."""

    WRITES = ("set_current", "enable_output")

    def __init__(self, inner):
        self._inner = inner
        self.calls = []
        self.writes_allowed = False

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapper(*a, **kw):
            self.calls.append((name, a))
            if name in self.WRITES and not self.writes_allowed:
                raise AssertionError(f"state-changing write at start: {name}{a}")
            return attr(*a, **kw)
        return wrapper


def _start(**pre):
    cfg = Config()
    ctrl, kepco, probe, acq, cal = build_sim_system(cfg, **pre)
    probe.emulate_timing = False
    strict = StrictKepco(kepco)
    ctrl.kepco = strict
    errors = []
    ctrl._on_event = lambda lvl, msg: errors.append(msg) if lvl == "error" else None
    ctrl.start()
    return cfg, ctrl, kepco, strict


def _wait(pred, timeout_s=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_start_issues_no_state_changing_writes_and_adopts_state():
    cfg, ctrl, kepco, strict = _start(initial_current_A=1.2, output_on=True,
                                      initial_do={"Dev1/port0/line1": True})
    try:
        time.sleep(0.4)           # many control-loop ticks + field readings
        # the loop kept running and never wrote (StrictKepco would have raised
        # inside the control thread; check the log too, in case it was swallowed)
        assert ctrl._thread.is_alive()
        assert not [c for c in strict.calls if c[0] in StrictKepco.WRITES]

        s = ctrl.status()
        assert s.state == "IDLE"
        assert s.output_on is True
        assert s.current_A == pytest.approx(1.2)
        assert s.setpoint_field_mT is None
        # the measured field is the one 1.2 A really makes in the sim magnet
        # (125 mT * tanh(1.2/3) ~ 47.5 mT), not 0
        assert _wait(lambda: abs(ctrl.status().measured_field_mT - 47.5) < 1.5)
        # the supply itself is untouched
        assert kepco.read_output() is True and kepco.read_current() == 1.2
        # AUX: a DO set before start is adopted; AO cannot be read back on the
        # 6259 and was not written, so it is reported as unknown (None)
        assert s.aux["do"]["Dev1/port0/line1"] is True
        assert s.aux["do"]["Dev1/port0/line0"] is False
        assert all(v is None for v in s.aux["ao"].values())
    finally:
        strict.writes_allowed = True     # shutdown (ramp to zero) is allowed
        ctrl.shutdown()


def test_first_command_ramps_from_the_adopted_current():
    cfg, ctrl, kepco, strict = _start(initial_current_A=1.2, output_on=True)
    try:
        time.sleep(0.1)
        strict.writes_allowed = True
        ctrl.set_current(0.5)
        assert _wait(lambda: ctrl.status().state == "IDLE"
                     and abs(ctrl.status().current_A - 0.5) < 1e-9)
        sets = [c[1][0] for c in strict.calls if c[0] == "set_current"]
        # a ramp DOWN from 1.2 A in 0.05 A steps -- never a jump from 0
        assert sets[0] == pytest.approx(1.2 - cfg.ramp.increment_A)
        assert max(sets) <= 1.2 + 1e-9
        assert not [c for c in strict.calls if c[0] == "enable_output"]  # was on
    finally:
        ctrl.shutdown()


def test_output_off_is_adopted_and_switched_on_safely():
    """Output off, but a stale 2 A still PROGRAMMED in the supply. Start must
    leave it off; the first command must program the flowing current (0 A)
    BEFORE switching the output on, so the magnet never steps to 2 A."""
    cfg, ctrl, kepco, strict = _start(initial_current_A=2.0, output_on=False)
    try:
        time.sleep(0.3)
        s = ctrl.status()
        assert s.output_on is False and s.current_A == 0.0
        assert kepco.read_output() is False

        strict.writes_allowed = True
        ctrl.set_current(0.3)
        assert _wait(lambda: abs(ctrl.status().current_A - 0.3) < 1e-9)
        names = [c[0] for c in strict.calls if c[0] in StrictKepco.WRITES]
        assert names[:2] == ["set_current", "enable_output"]
        first = next(c for c in strict.calls if c[0] == "set_current")
        assert first[1][0] == 0.0
        assert ctrl.status().output_on is True
    finally:
        ctrl.shutdown()


def test_status_over_the_wire_reflects_preexisting_state():
    pytest.importorskip("zmq")
    from clMag.net.service import ClMagService
    from clMag.net.client import ClMagClient

    cfg = Config()
    ctrl, *_ = build_sim_system(cfg, initial_current_A=-0.8, output_on=True)
    svc = ClMagService(ctrl, host="127.0.0.1", cmd_port=5781, pub_port=5782)
    svc.start()
    client = None
    try:
        client = ClMagClient(host="127.0.0.1", cmd_port=5781, pub_port=5782)
        client.start()
        s = client.status()
        assert s.output_on is True
        assert s.current_A == pytest.approx(-0.8)
        assert s.state == "IDLE"
        assert all(v is None for v in s.aux["ao"].values())
        ids = {p["id"] for p in client.describe()["parameters"]}
        assert "output_on" in ids
    finally:
        if client:
            client.shutdown()
        svc.stop()
