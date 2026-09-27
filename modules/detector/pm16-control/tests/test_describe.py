"""The `describe` manifest: bounds looked up live, range control/indicator
switch, acquire block on the scan detectors, revision follows the shape."""

import json

import pytest

from pm16.config import Config
from pm16.net.describe import build_manifest, read_path
from pm16.net.protocol import status_to_dict
from pm16.sim_system import build_sim_system


@pytest.fixture
def meter():
    m, _ = build_sim_system(Config(), realtime=False)
    m.start(poll=False)
    m.poll_once()
    yield m
    m.shutdown()


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_is_json_and_ids_unique(meter):
    man = build_manifest(meter)
    json.dumps(man, allow_nan=False)                 # no NaN may leak into JSON
    ids = [p["id"] for p in man["parameters"]]
    assert len(ids) == len(set(ids))
    assert man["module"] == "pm16"


def test_bounds_are_looked_up_not_restated(meter):
    meter.cfg.limits.wavelength_min_nm = 500.0
    p = _by_id(build_manifest(meter))["wavelength"]
    assert p["min"] == 500.0 and p["max"] == 1100.0


def test_range_is_indicator_on_auto_and_control_on_manual(meter):
    auto = build_manifest(meter)
    assert _by_id(auto)["range"]["kind"] == "indicator"
    meter.set_auto_range(False)
    manual = build_manifest(meter)
    r = _by_id(manual)["range"]
    assert r["kind"] == "control" and r["set"]["verb"] == "set_range"
    assert r["scale"] == 1e-3
    assert manual["revision"] != auto["revision"]


def test_detectors_share_one_acquire_keyed_on_the_trigger_reply(meter):
    d = _by_id(build_manifest(meter))
    for pid in ("power", "power_std"):
        acq = d[pid]["acquire"]
        assert acq["trigger_verb"] == "acquire" and acq["target_key"] == "acq_id"
        assert acq["ready"]["policy"] == "adopt_then_flag"
    assert d["power"]["acquire"] == d["power_std"]["acquire"]
    assert "acquire" not in d["live_power"]


def test_read_paths_resolve_against_real_status(meter):
    meter.acquire()
    for _ in range(meter.cfg.acquisition.readings):
        meter.poll_once()
    st = status_to_dict(meter.status())
    for p in build_manifest(meter)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None or p["id"] in ("hw_error",), p["id"]


def test_settle_keys_exist_in_status(meter):
    st = status_to_dict(meter.status())
    for p in build_manifest(meter)["parameters"]:
        settle = p.get("settle") or {}
        if "key" in settle:
            assert settle["key"] in st, p["id"]


def test_zero_is_marked_dangerous(meter):
    assert _by_id(build_manifest(meter))["zero"]["danger"] is True
