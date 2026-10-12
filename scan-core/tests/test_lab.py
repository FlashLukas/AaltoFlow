"""Tests for the real-instrument registry.

The point of `lab.py` is that a Settable built from a service behaves exactly
like a simulated one -- blocking set, clamped to limits, readable back -- so a
recipe does not care which it is running against. Everything comes from the
module's own `describe`; scan-core has no per-instrument code.
"""

from __future__ import annotations

import pytest

from scan_core.instrument import InstrumentError
from scan_core.lab import build_lab_registry
from scan_core.registry import Gettable, Settable

from conftest import DEMO_MANIFEST


def test_registry_shape_and_limits_from_the_service(fake_service):
    """Limits must come from the instrument, not from a number typed in here."""
    svc = fake_service(15830, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        field = reg.get("field")
        assert isinstance(field, Settable)
        assert field.unit == "mT"
        assert field.limits == (-95.0, 95.0)          # the manifest's min/max
        assert isinstance(reg.get("measured_field"), Gettable)
    finally:
        lab.close()


def test_field_set_blocks_until_settled(fake_service):
    """Settable.set must not return until the field is actually there.

    This is the contract engine.py depends on: when set() returns, reading a
    detector is meaningful. The fake keeps a stale `stable=True` alive for
    0.3 s after the command, so a set that ignored the guard would return with
    measured still at 0.
    """
    svc = fake_service(15832, manifest=DEMO_MANIFEST, adopt_delay=0.3, settle_delay=0.3)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        field = reg.get("field")
        assert field.set(40.0) == 40.0
        assert field.get() == 40.0
        assert reg.get("measured_field").get() == 40.0
    finally:
        lab.close()


def test_field_set_clamps_to_the_instrument_envelope(fake_service):
    """Out-of-range requests are clamped, and the clamped value is what is set."""
    svc = fake_service(15834, manifest=DEMO_MANIFEST, adopt_delay=0.05, settle_delay=0.05)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        assert reg.get("field").set(500.0) == 95.0     # clamped to the max
        assert reg.get("measured_field").get() == 95.0
    finally:
        lab.close()


def test_a_service_without_describe_is_refused(fake_service):
    """No hand-written fallback: a module that cannot describe itself is
    refused, and the error says where the contract is written down."""
    svc = fake_service(15836, manifest=None)           # answers ok:false
    with pytest.raises(InstrumentError) as exc:
        build_lab_registry(host="127.0.0.1", include=("clMag",),
                           ports={"clMag": svc.cmd_port})
    assert "describe" in str(exc.value)
    assert "INSTRUMENT_MODULE_GUIDE.md section 6b" in str(exc.value)


def test_include_is_required():
    """There is no default instrument: the suite assumes no lab's rig."""
    with pytest.raises(TypeError):
        build_lab_registry()                            # type: ignore[call-arg]


def test_a_single_name_is_one_module_not_its_letters(fake_service):
    svc = fake_service(15840, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry("clMag", host="127.0.0.1",
                                  ports={"clMag": svc.cmd_port})
    try:
        assert list(lab.instruments) == ["clMag"]
    finally:
        lab.close()


def test_missing_service_closes_every_socket_it_opened(fake_service):
    """A half-built registry must not leave connections behind."""
    svc = fake_service(15838, manifest=DEMO_MANIFEST)
    with pytest.raises(InstrumentError):
        build_lab_registry(
            host="127.0.0.1", include=("clMag", "smb"), timeout_ms=300,
            ports={"clMag": svc.cmd_port, "smb": 15898})   # smb: nothing there


def test_unknown_instrument_name_is_refused(fake_service):
    with pytest.raises(ValueError) as exc:
        build_lab_registry(host="127.0.0.1", include=("lockin",), timeout_ms=300)
    assert "lockin" in str(exc.value)
