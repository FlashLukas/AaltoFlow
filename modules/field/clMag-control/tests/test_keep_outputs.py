"""shutdown{keep_outputs} (Lukas, 2026-10-06).

A plain `shutdown` stays the SAFE stop: ramp to zero, OUTP OFF. With
`keep_outputs: true` it is a RESTART for a code update: the service closes the
supply and releases everything, but sends nothing that changes the current or
the output -- the magnet keeps its field and the next start adopts it.
"""

import time

import pytest

pytest.importorskip("zmq")

from clMag.config import Config
from clMag.sim_system import build_sim_system
from clMag.net.service import ClMagService

# a port block of its own (no other clMag test uses 261xx)
CMD, PUB = 26110, 26111
WRITES = ("set_current", "enable_output")


class Recorder:
    """Wraps the simulated supply and logs every call by name."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapper(*a, **kw):
            self.calls.append(name)
            return attr(*a, **kw)
        return wrapper


def _service():
    cfg = Config()
    # the supply is driving 1.2 A when the service starts (adopted as is)
    ctrl, kepco, probe, acq, cal = build_sim_system(
        cfg, initial_current_A=1.2, output_on=True)
    probe.emulate_timing = False
    rec = Recorder(kepco)
    ctrl.kepco = rec
    svc = ClMagService(ctrl, host="127.0.0.1", cmd_port=CMD, pub_port=PUB)
    svc.start()
    time.sleep(0.2)
    return svc, kepco, rec


def test_keep_outputs_is_refused_a_restart_ramps_down():
    # Lukas 2026-10-11: magnets ramp to zero on a restart too
    svc, kepco, rec = _service()
    r = svc._dispatch({"cmd": "shutdown", "keep_outputs": True})
    assert r["ok"] and r["stopping"] and r["kept_outputs"] is False
    assert "not honoured" in r["note"]
    svc.stop()
    assert "set_current" in rec.calls             # the ramp to zero
    assert kepco.read_output() is False and kepco.read_current() == 0.0


def test_plain_shutdown_still_ramps_down_and_switches_off():
    svc, kepco, rec = _service()
    r = svc._dispatch({"cmd": "shutdown"})
    assert r["ok"] and r["kept_outputs"] is False
    svc.stop()
    assert "set_current" in rec.calls             # the ramp to zero
    assert kepco.read_output() is False and kepco.read_current() == 0.0


def test_keep_outputs_text_false_is_false():
    # gotcha #3: the string "false" from a hand-typed console must not keep
    svc, kepco, rec = _service()
    r = svc._dispatch({"cmd": "shutdown", "keep_outputs": "false"})
    assert r["kept_outputs"] is False
    svc.stop()
    assert kepco.read_output() is False
