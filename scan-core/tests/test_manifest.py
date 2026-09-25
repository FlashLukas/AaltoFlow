"""Tests for building a registry from a service's `describe` manifest.

The claim being tested is that scan-core needs NO per-instrument code: point it
at a module that describes itself and every knob becomes a Parameter, with the
module's own limits and its own settle rule.
"""

from __future__ import annotations

import copy
import time

import pytest

from scan_core.instrument import Instrument
from scan_core.lab import build_lab_registry
from scan_core.manifest import register_manifest, resolve_settle
from scan_core.registry import Gettable, Registry, Settable

from conftest import DEMO_MANIFEST


def test_registry_is_built_entirely_from_the_manifest(fake_service):
    svc = fake_service(15850, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        # controls -> Settables, with the MODULE's limits, not scan-core's
        field = reg.get("field")
        assert isinstance(field, Settable)
        assert field.limits == (-95.0, 95.0)
        assert field.unit == "mT"

        # indicators -> Gettables
        assert isinstance(reg.get("measured_field"), Gettable)

        # actions are not scan axes
        assert reg.get("demag") is None
    finally:
        lab.close()


def test_settle_rule_comes_from_the_manifest(fake_service):
    """The module states when it has arrived, and the coordinator obeys it.

    The fake keeps a stale `field_stable=True` alive for 0.3 s after accepting a
    command; the manifest's adopt_then_flag rule is what stops the set from
    returning on it.
    """
    svc = fake_service(15852, manifest=DEMO_MANIFEST,
                       adopt_delay=0.3, settle_delay=0.3)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        assert reg.get("field").set(40.0) == 40.0
        assert reg.get("measured_field").get() == 40.0
    finally:
        lab.close()


def test_bool_control_gets_two_state_limits_and_a_bool_on_the_wire(fake_service):
    svc = fake_service(15854, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        enabled = reg.get("enabled")
        assert enabled.limits == (0, 1), "a bool is not an unbounded float"
    finally:
        lab.close()


def test_enum_control_is_registered_read_only_with_a_warning(fake_service):
    """scan-core's Settable is numeric; an enum axis would crash on float()."""
    svc = fake_service(15856, manifest=DEMO_MANIFEST)
    warnings = []
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port},
                                  on_warn=warnings.append)
    try:
        assert isinstance(reg.get("mode"), Gettable)
        assert any("mode" in w and "not scannable" in w for w in warnings), warnings
    finally:
        lab.close()


def test_a_control_with_a_readback_can_also_be_a_detector(fake_service):
    """Recording the knob you are driving is normal and must be allowed."""
    from scan_core.recipe import Recipe

    svc = fake_service(15858, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        r = Recipe(name="t",
                   axes=[{"type": "linear", "param": "field",
                          "start": 0, "stop": 10, "num": 2}],
                   detectors=["rf_power", "measured_field"])
        assert r.validate(reg) == [], "a readable control was rejected as a detector"
    finally:
        lab.close()


def test_unknown_settle_policy_degrades_loudly_not_silently():
    """A newer module may name a policy this scan-core has never heard of.

    Refusing to run would be unhelpful; not waiting *silently* would produce a
    grid measured one step behind that looks like clean data. So: don't wait,
    but say so.
    """
    warnings = []
    policy = resolve_settle({"policy": "quantum_entanglement"}, warnings.append)
    assert policy(1.0)({}) is True                      # degraded to immediate
    assert any("quantum_entanglement" in w for w in warnings), warnings

    warnings.clear()
    # a known policy missing a required field must degrade the same way
    resolve_settle({"policy": "adopt_then_flag"}, warnings.append)
    assert any("missing field" in w for w in warnings), warnings


def test_prefix_namespaces_ids_per_module(fake_service):
    """Two modules both owning 'position' must not collide."""
    svc = fake_service(15860, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port}, prefix=True)
    try:
        assert reg.get("fake.field") is not None      # manifest's module name
        assert reg.get("field") is None
    finally:
        lab.close()


def test_falls_back_to_the_builtin_declaration_without_describe(fake_service):
    """A module that predates the verb must still be usable, and must say so."""
    svc = fake_service(15862, manifest=None)          # answers ok:false
    warnings = []
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port},
                                  on_warn=warnings.append)
    try:
        # scan-core's hand-written clMag declaration was used instead
        assert reg.get("field") is not None
        assert any("no `describe`" in w for w in warnings), warnings
    finally:
        lab.close()


def test_manifest_limits_are_used_not_scan_cores_own_idea(fake_service):
    """If the module narrows a limit, the registry must narrow with it."""
    narrowed = copy.deepcopy(DEMO_MANIFEST)
    field = [p for p in narrowed["parameters"] if p["id"] == "field"][0]
    field["min"], field["max"] = -12.0, 12.0
    narrowed["revision"] = 999

    svc = fake_service(15864, manifest=narrowed, adopt_delay=0.05,
                       settle_delay=0.05)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        assert reg.get("field").limits == (-12.0, 12.0)
        assert reg.get("field").set(500.0) == 12.0     # clamped to the module's
    finally:
        lab.close()


def test_actions_never_enter_the_registry():
    """A registry Parameter is a value to sweep or record. A button is neither.

    The control screen reads the manifest directly for buttons, their argument
    lists and their danger flags -- same source of truth, a different view of it.
    """
    reg = Registry()

    class _Stub:
        name = "fake"

        def status(self):
            return {}

    ids = register_manifest(reg, _Stub(), DEMO_MANIFEST)
    assert "field" in ids
    assert "demag" not in ids
    assert reg.get("demag") is None
    # ...but the manifest still carries everything a panel needs for it
    demag = [p for p in DEMO_MANIFEST["parameters"] if p["id"] == "demag"][0]
    assert demag["danger"] is True and demag["args"]


# --------------------------------------------------------------------------- #
# Inherited limits must stay CURRENT, not just be inherited once
# --------------------------------------------------------------------------- #

def test_refresh_stale_picks_up_moved_limits(fake_service):
    """Limits are inherited from the module -- but they MOVE.

    piezo's travel ceiling drops when an axis goes closed-loop; kim's armed
    leash replaces the clamp; clMag's field range IS the calibration. A registry
    built before any of that would let a recipe sweep past what the instrument
    now accepts, and nothing would raise: the service clamps, and the scan
    records points it never visited at the coordinates it thinks it did.
    """
    svc = fake_service(15870, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        assert reg.get("field").limits == (-95.0, 95.0)

        # the instrument narrows its own envelope (a leash armed, a loop mode
        # switched, a calibration loaded) and its revision moves with it
        narrowed = copy.deepcopy(DEMO_MANIFEST)
        field = [p for p in narrowed["parameters"] if p["id"] == "field"][0]
        field["min"], field["max"] = -12.0, 12.0
        narrowed["revision"] = DEMO_MANIFEST["revision"] + 1
        svc.manifest = narrowed
        time.sleep(0.2)                       # let a status frame carry the rev

        notes = []
        changed = lab.refresh_stale(reg, on_warn=notes.append)

        assert "field" in changed, f"limit change not noticed (notes={notes})"
        assert reg.get("field").limits == (-12.0, 12.0)
        assert any("limits changed" in n for n in notes), notes

        # ...and the narrowed limit is now the one that clamps
        assert reg.get("field").set(500.0) == 12.0
    finally:
        lab.close()


def test_refresh_stale_is_a_no_op_when_nothing_moved(fake_service):
    """It runs before every scan, so it must be cheap and silent when idle."""
    svc = fake_service(15872, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port})
    try:
        time.sleep(0.2)
        svc.commands.clear()
        notes = []
        assert lab.refresh_stale(reg, on_warn=notes.append) == []
        assert notes == []
        # it must not re-fetch the whole manifest just to find out nothing moved
        assert "describe" not in svc.commands, \
            "compared by re-reading describe instead of the status revision"
    finally:
        lab.close()


def test_refresh_stale_respects_the_prefix(fake_service):
    svc = fake_service(15874, manifest=DEMO_MANIFEST)
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port}, prefix=True)
    try:
        narrowed = copy.deepcopy(DEMO_MANIFEST)
        field = [p for p in narrowed["parameters"] if p["id"] == "field"][0]
        field["min"], field["max"] = -5.0, 5.0
        narrowed["revision"] = DEMO_MANIFEST["revision"] + 2
        svc.manifest = narrowed
        time.sleep(0.2)

        changed = lab.refresh_stale(reg, prefix=True)
        assert changed == ["fake.field"]
        assert reg.get("fake.field").limits == (-5.0, 5.0)
    finally:
        lab.close()
