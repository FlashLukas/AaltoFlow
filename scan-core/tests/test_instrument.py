"""Tests for the generic ZeroMQ instrument client and the settle policies.

All of these run against `conftest.FakeService` on loopback, on ports well away
from the suite's 5555-5568 range so they cannot collide with services someone
has running.
"""

from __future__ import annotations

import time

import pytest

from scan_core.instrument import (Instrument, InstrumentError, adopt_then_flag,
                                  echoes, flag_only)


def test_info_status_and_error_reply(fake_service):
    svc = fake_service(15810)
    with Instrument("fake", host="127.0.0.1", cmd_port=svc.cmd_port) as inst:
        assert inst.start()["field_hi"] == 95.0

        st = inst.status()
        assert st["field_stable"] is True
        assert st["setpoint_field_mT"] == 0.0

        # A service that says ok=false must raise, not return a dict the caller
        # might use as if the command had worked.
        with pytest.raises(InstrumentError) as exc:
            inst.command("no_such_verb")
        assert "no_such_verb" in str(exc.value)

        # ...and the connection still works afterwards
        assert inst.command("status")["ok"] is True


def test_unreachable_service_raises_quickly():
    """Nothing is listening on this port; start() must fail fast and clearly."""
    inst = Instrument("nobody", host="127.0.0.1", cmd_port=15899, timeout_ms=300)
    try:
        t0 = time.monotonic()
        with pytest.raises(InstrumentError) as exc:
            inst.start()
        assert time.monotonic() - t0 < 3.0
        assert "15899" in str(exc.value)      # the message names the address
    finally:
        inst.close()


def test_adopt_then_flag_survives_stale_status(fake_service):
    """The core guarantee: never return on the previous point's success.

    The fake starts settled at 0 mT, so at the moment `set_field` is accepted
    the live status reads setpoint=0, stable=True. A policy that only watches
    the flag returns right there -- see the next test. This one must not.
    """
    svc = fake_service(15812, adopt_delay=0.3, settle_delay=0.3)
    policy = adopt_then_flag("setpoint_field_mT", "field_stable")
    with Instrument("fake", host="127.0.0.1", cmd_port=svc.cmd_port) as inst:
        inst.command("set_field", field_mT=40.0)
        st = inst.wait_until(policy(40.0), timeout_s=10.0, what="field")

        assert st["setpoint_field_mT"] == 40.0
        assert st["field_stable"] is True
        assert st["measured_field_mT"] == 40.0


def test_flag_only_is_fooled_by_stale_status(fake_service):
    """Executable documentation for the trap `adopt_then_flag` avoids.

    If this test ever starts failing, the stale-status window has gone away and
    the guard could be reconsidered. Until then it is exactly why the guard is
    not optional.
    """
    svc = fake_service(15814, adopt_delay=0.4, settle_delay=0.4)
    policy = flag_only("field_stable")
    with Instrument("fake", host="127.0.0.1", cmd_port=svc.cmd_port) as inst:
        inst.command("set_field", field_mT=40.0)
        st = inst.wait_until(policy(40.0), timeout_s=10.0, what="field")

        # It "succeeded" instantly -- at the OLD point.
        assert st["measured_field_mT"] == 0.0
        assert st["setpoint_field_mT"] == 0.0


def test_wait_until_raises_with_a_useful_message(fake_service):
    svc = fake_service(15816, adopt_delay=5.0)
    policy = adopt_then_flag("setpoint_field_mT", "field_stable")
    with Instrument("fake", host="127.0.0.1", cmd_port=svc.cmd_port) as inst:
        inst.command("set_field", field_mT=40.0)
        with pytest.raises(TimeoutError) as exc:
            inst.wait_until(policy(40.0), timeout_s=0.3, what="field = 40 mT")
        msg = str(exc.value)
        assert "field = 40 mT" in msg          # what we were waiting for
        assert "setpoint_field_mT" in msg      # and what we saw instead


def test_echoes_policy_confirms_a_set_and_forget_value(fake_service):
    """The set-and-forget half of the suite: no flag, but the value echoes back."""
    svc = fake_service(15818)
    policy = echoes("power_dBm", tol=1e-3)
    with Instrument("fake", host="127.0.0.1", cmd_port=svc.cmd_port) as inst:
        assert inst.status()["power_dBm"] == -10.0

        inst.command("set_power", power_dBm=-3.5)
        st = inst.wait_until(policy(-3.5), timeout_s=5.0, what="rf power")
        assert st["power_dBm"] == -3.5


def test_status_falls_back_to_request_before_any_pub_frame(fake_service):
    """status() must work in the gap before the first PUB frame arrives."""
    svc = fake_service(15820)
    inst = Instrument("fake", host="127.0.0.1", cmd_port=svc.cmd_port)
    try:
        st = inst.status()          # immediately, SUB cache certainly empty
        assert "field_stable" in st
    finally:
        inst.close()
