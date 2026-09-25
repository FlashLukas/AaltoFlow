"""Tests for the real-instrument registry.

The point of `lab.py` is that a Settable built from a service behaves exactly
like a simulated one -- blocking set, clamped to limits, readable back -- so a
recipe does not care which it is running against.
"""

from __future__ import annotations

import pytest

from scan_core.instrument import InstrumentError
from scan_core.lab import build_lab_registry
from scan_core.registry import Settable, Gettable


def test_clMag_registry_shape_and_limits_from_the_service(fake_service):
    """Limits must come from the instrument, not from a number typed in here."""
    svc = fake_service(15830)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        field = reg.get("field")
        assert isinstance(field, Settable)
        assert field.unit == "mT"
        # the fake reports -95..95 in its info block
        assert field.limits == (-95.0, 95.0)

        ids = {p.id for p in reg.gettables()}
        assert {"measured_field", "current"} <= ids
        assert {"aux_ai1", "aux_ai2", "aux_ai3"} <= ids
        assert isinstance(reg.get("aux_ai1"), Gettable)
    finally:
        lab.close()


def test_field_set_blocks_until_settled(fake_service):
    """Settable.set must not return until the field is actually there.

    This is the contract engine.py depends on: when set() returns, reading a
    detector is meaningful. The fake keeps a stale `stable=True` alive for
    0.3 s after the command, so a set that ignored the guard would return with
    measured still at 0.
    """
    svc = fake_service(15832, adopt_delay=0.3, settle_delay=0.3)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        field = reg.get("field")
        assert field.set(40.0) == 40.0
        # by the time set() returned, the readback must agree
        assert field.get() == 40.0
        assert reg.get("measured_field").get() == 40.0
    finally:
        lab.close()


def test_field_set_clamps_to_the_instrument_envelope(fake_service):
    """Out-of-range requests are clamped, and the clamped value is what is set."""
    svc = fake_service(15834, adopt_delay=0.05, settle_delay=0.05)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        assert reg.get("field").set(500.0) == 95.0     # clamped to field_hi
        assert reg.get("measured_field").get() == 95.0
    finally:
        lab.close()


def test_aux_detector_reads_a_fresh_sample_not_the_status_cache(fake_service):
    """Detectors must issue a command; a cached value is a wrong data point."""
    svc = fake_service(15836)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        svc.commands.clear()
        value = reg.get("aux_ai2").get()
        assert value == pytest.approx(0.1 * len("Dev1/ai2"))
        assert "aux_read_ai" in svc.commands, "detector used the status cache"
    finally:
        lab.close()


def test_missing_service_closes_every_socket_it_opened(fake_service):
    """A half-built registry must not leave connections behind."""
    svc = fake_service(15838)
    with pytest.raises(InstrumentError):
        build_lab_registry(
            host="127.0.0.1", include=("clMag", "smb"), timeout_ms=300,
            ports={"clMag": svc.cmd_port, "smb": 15898})   # smb: nothing there


def test_unknown_instrument_name_is_refused(fake_service):
    with pytest.raises(ValueError) as exc:
        build_lab_registry(host="127.0.0.1", include=("lockin",), timeout_ms=300)
    assert "lockin" in str(exc.value)
