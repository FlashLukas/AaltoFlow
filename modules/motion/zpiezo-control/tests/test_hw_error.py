"""Loud hardware errors + an honest echo (Lukas's decisions, 2026-09-28).

zpiezo settles with `echoes(voltage)`: a scan waits until the status reports
the voltage it asked for.  That is only as good as `voltage` itself.  Checked:
  * the echo is the KCube's READ-BACK, taken under the same lock as the write,
    so it cannot run ahead of the applied value (there is no step ramp in
    zpiezo: set_voltage writes once, see CLAUDE.local.md);
  * BUT on a failed read, status() fell back to voltage = TARGET -- the
    commanded value.  After set_voltage(30) with a dead USB link the status
    said "30 V", exactly what the scan waits for: settled, on a voltage nobody
    measured.  Now: the last GOOD read-back (NaN if there never was one),
    `hw_error` with the message, and one error event per failure episode.

Offline, no ports.
"""

from __future__ import annotations

import math

import pytest

from zpiezo.backends.sim import SimZ
from zpiezo.config import Config
from zpiezo.net import protocol as P
from zpiezo.net.client import _status_from_dict
from zpiezo.net.describe import build_manifest
from zpiezo.zpiezo import ZPiezo


class FlakyZ(SimZ):
    fail = False

    def read_voltage(self):
        if self.fail:
            raise OSError("KCube: no reply")
        return super().read_voltage()


def _brain(v0=10.0):
    be = FlakyZ(v0=v0)
    brain = ZPiezo(be, Config())
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))
    brain.start()
    return brain, be, events


def _echo_settled(status, target, tol=0.01):
    """scan-core's `echoes` predicate, as it would run on this status."""
    d = P.status_to_dict(status)
    v = d.get("voltage")
    return v is not None and abs(float(v) - float(target)) <= tol


def test_a_failed_read_does_not_echo_the_commanded_voltage():
    brain, be, _ = _brain(v0=10.0)
    assert brain.status().voltage == 10.0
    be.fail = True
    brain.set_voltage(30.0)              # the write works, the read-back does not
    st = brain.status()
    assert st.target == 30.0
    assert not _echo_settled(st, 30.0)   # a scan must NOT settle on this
    assert st.voltage == 10.0            # the last voltage actually read back
    assert "KCube: no reply" in st.hw_error
    brain.shutdown()


def test_no_good_read_ever_gives_nan_not_the_target():
    be = FlakyZ(v0=5.0)
    be.fail = True
    brain = ZPiezo(be, Config())
    brain.start()
    brain.set_voltage(5.0)
    st = brain.status()
    assert math.isnan(st.voltage)
    assert not _echo_settled(st, 5.0)
    assert st.hw_error
    brain.shutdown()


def test_one_error_event_per_episode_and_recovery():
    brain, be, events = _brain()
    events.clear()
    be.fail = True
    for _ in range(5):
        brain.status()
    assert len([m for lvl, m in events if lvl == "error"]) == 1
    be.fail = False
    st = brain.status()
    assert st.hw_error == ""
    assert any(lvl == "info" and "recovered" in m for lvl, m in events)
    be.fail = True
    brain.status()
    assert len([m for lvl, m in events if lvl == "error"]) == 2
    brain.shutdown()


def test_the_echo_is_the_read_back_not_the_command():
    """A KCube that holds a slightly different voltage than commanded (DAC
    truncation): the status must report what it HOLDS."""
    class Truncating(SimZ):
        def set_voltage(self, volts):
            super().set_voltage(volts - 0.002)

    brain = ZPiezo(Truncating(v0=0.0), Config())
    brain.start()
    brain.set_voltage(20.0)
    st = brain.status()
    assert st.voltage == pytest.approx(19.998)
    assert st.hw_error == ""
    brain.shutdown()


def test_wire_client_and_describe_carry_hw_error():
    brain, be, _ = _brain()
    be.fail = True
    d = P.status_to_dict(brain.status())
    assert d["hw_error"]
    assert _status_from_dict(d).hw_error == d["hw_error"]
    assert _status_from_dict({}).hw_error == ""       # older service
    ids = {p["id"]: p for p in build_manifest(brain)["parameters"]}
    assert ids["hw_error"]["read_path"] == ["hw_error"]
    brain.shutdown()
