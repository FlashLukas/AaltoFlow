"""Tests for the `describe` manifest -- this service's self-description.

What matters most and is easy to get wrong:
  * every bound is read LIVE (the wavelength range IS the current grating's);
  * `revision` changes when the bounds or the structure change (grating swap,
    accessory fitted), and NOT when a measured value changes;
  * the wavelength settles on adopt_then_flag -- target first, then moving.
"""

import pytest

pytest.importorskip("zmq")

from cs260.config import Config
from cs260.sim_system import build_sim_system
from cs260.net.describe import build_manifest, read_path
from cs260.net.protocol import status_to_dict

from test_monochromator import Clock, run, settle


def _brain(**tweaks):
    cfg = Config()
    for path, value in tweaks.items():
        group, field = path.split("__")
        setattr(getattr(cfg, group), field, value)
    clock = Clock()
    mono, sim = build_sim_system(cfg, clock=clock)
    mono.start(poll=False)
    return cfg, mono, clock


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields():
    cfg, mono, _ = _brain()
    m = build_manifest(mono)
    assert m["module"] == "cs260"
    assert isinstance(m["revision"], int)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids)), "duplicate parameter ids"
    for p in m["parameters"]:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        assert p["type"] in ("float", "int", "bool", "enum", "string", "action"), p["id"]
        if p["kind"] == "control":
            assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
            assert "settle" in p and p["timeout_s"] > 0, p["id"]
        if p["kind"] == "indicator":
            assert p["read_path"], p["id"]
        for a in p.get("args") or []:
            assert "name" in a and "type" in a and "default" in a, p["id"]


def test_every_read_path_resolves_against_a_real_status():
    cfg, mono, _ = _brain(accessories__filter_wheel=True, accessories__dual_port=True)
    st = status_to_dict(mono.status())
    for p in build_manifest(mono)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None, p["id"]


def test_wavelength_is_adopt_then_flag_with_live_limits():
    cfg, mono, _ = _brain()
    w = _by_id(build_manifest(mono))["wavelength"]
    assert w["unit"] == "nm"
    assert (w["min"], w["max"]) == mono.limits_for(1)
    assert w["settle"] == {"policy": "adopt_then_flag", "setpoint_key": "target_nm",
                           "flag_key": "moving", "invert": True, "tol": 1e-3}
    assert w["set"] == {"verb": "set_wavelength", "arg": "wavelength_nm"}


def test_limits_follow_cfg():
    cfg, mono, _ = _brain()
    cfg.gratings.g1_max_nm = 1111.0
    assert _by_id(build_manifest(mono))["wavelength"]["max"] == 1111.0
    cfg.limits.wavelength_max_nm = 900.0
    assert _by_id(build_manifest(mono))["wavelength"]["max"] == 900.0


def test_revision_moves_with_the_grating_not_with_the_wavelength():
    cfg, mono, clock = _brain()
    r0 = build_manifest(mono)["revision"]
    mono.set_wavelength(700.0); settle(mono, clock)
    assert build_manifest(mono)["revision"] == r0, "a value change must not bump revision"
    mono.set_grating(2)
    r1 = build_manifest(mono)["revision"]
    assert r1 != r0, "the grating's range is a new bound"
    assert _by_id(build_manifest(mono))["wavelength"]["max"] == cfg.gratings.g2_max_nm


def test_accessories_appear_only_when_fitted():
    cfg, mono, _ = _brain()
    ids = _by_id(build_manifest(mono))
    assert "filter" not in ids and "port" not in ids
    r0 = build_manifest(mono)["revision"]
    cfg.accessories.filter_wheel = True
    cfg.accessories.dual_port = True
    ids = _by_id(build_manifest(mono))
    assert ids["filter"]["max"] == 6 and ids["port"]["max"] == 2
    assert ids["filter"]["settle"]["setpoint_key"] == "filter_target"
    assert build_manifest(mono)["revision"] != r0


def test_grating_control_bounds_follow_count():
    cfg, mono, _ = _brain()
    assert _by_id(build_manifest(mono))["grating"]["max"] == 2
    cfg.gratings.count = 3
    assert _by_id(build_manifest(mono))["grating"]["max"] == 3


def test_actions():
    cfg, mono, _ = _brain()
    ids = _by_id(build_manifest(mono))
    assert ids["abort"]["wait"]["ready"] == {"policy": "flag_only", "key": "moving",
                                             "invert": True}
    assert ids["calibrate"]["danger"] is True
    assert ids["step"]["args"][0]["name"] == "steps"
