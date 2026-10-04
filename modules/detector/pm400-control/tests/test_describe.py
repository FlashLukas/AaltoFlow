"""The `describe` manifest: bounds looked up live, the shape follows the HEAD,
range control/indicator switch, acquire block on the scan detectors, a wait
block on zero, revision follows the shape."""

import json

import pytest

from pm400.config import Config
from pm400.net.describe import build_manifest, read_path
from pm400.net.protocol import status_to_dict
from pm400.sim_system import build_sim_system


@pytest.fixture
def meter():
    m, _ = build_sim_system(Config(), realtime=False, zero_time_s=0.0)
    m.start(poll=False)
    m.poll_once()
    yield m
    m.shutdown()


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def _swap(m, head):
    m.cfg.sim.head = head
    m.check_head()
    m.poll_once()


@pytest.mark.parametrize("head", ["photodiode", "thermal", "pyro", "none"])
def test_manifest_is_json_and_ids_unique(meter, head):
    _swap(meter, head)
    man = build_manifest(meter)
    json.dumps(man, allow_nan=False)                 # no NaN may leak into JSON
    ids = [p["id"] for p in man["parameters"]]
    assert len(ids) == len(set(ids))
    assert man["module"] == "pm400"


def test_bounds_are_looked_up_not_restated(meter):
    meter.cfg.limits.wavelength_min_nm = 500.0
    p = _by_id(build_manifest(meter))["wavelength"]
    assert p["min"] == 500.0 and p["max"] == 1100.0
    _swap(meter, "thermal")
    p = _by_id(build_manifest(meter))["wavelength"]
    assert p["max"] == 25000.0


def test_power_head_shape(meter):
    d = _by_id(build_manifest(meter))
    for pid in ("power", "power_std", "live_power", "auto_range", "avg_time", "zero"):
        assert pid in d, pid
    assert "energy" not in d and "rep_rate" not in d
    assert d["power"]["unit"] == "mW" and d["power"]["scale"] == 1e-3
    assert d["avg_time"]["set"]["verb"] == "set_avg_time"


def test_pyro_head_shape_and_revision(meter):
    before = build_manifest(meter)
    _swap(meter, "pyro")
    after = build_manifest(meter)
    d = _by_id(after)
    for pid in ("energy", "energy_std", "live_energy", "rep_rate"):
        assert pid in d, pid
    for pid in ("power", "auto_range", "avg_time", "zero"):
        assert pid not in d, pid
    assert d["energy"]["unit"] == "mJ"
    assert d["range"]["kind"] == "control" and d["range"]["label"] == "Energy range"
    assert after["revision"] != before["revision"]


def test_no_head_hides_sensor_controls(meter):
    _swap(meter, "none")
    d = _by_id(build_manifest(meter))
    assert "wavelength" not in d and "range" not in d and "zero" not in d
    assert "power" in d                              # the detector id stays; acquire refuses


def test_range_is_indicator_on_auto_and_control_on_manual(meter):
    auto = build_manifest(meter)
    assert _by_id(auto)["range"]["kind"] == "indicator"
    meter.set_auto_range(False)
    manual = build_manifest(meter)
    r = _by_id(manual)["range"]
    assert r["kind"] == "control" and r["set"]["verb"] == "set_range"
    assert r["set"]["arg"] == "range" and r["scale"] == 1e-3
    assert manual["revision"] != auto["revision"]


def test_detectors_share_one_acquire_keyed_on_the_trigger_reply(meter):
    d = _by_id(build_manifest(meter))
    for pid in ("power", "power_std"):
        acq = d[pid]["acquire"]
        assert acq["trigger_verb"] == "acquire" and acq["target_key"] == "acq_id"
        assert acq["ready"]["policy"] == "adopt_then_flag"
    assert d["power"]["acquire"] == d["power_std"]["acquire"]
    assert "acquire" not in d["live_power"]


def test_zero_has_a_wait_block_and_is_not_danger(meter):
    z = _by_id(build_manifest(meter))["zero"]
    assert "danger" not in z
    w = z["wait"]
    assert w["target_key"] == "zero_id"
    assert w["ready"] == {"policy": "adopt_then_flag", "setpoint_key": "zero_id",
                          "flag_key": "zeroing", "invert": True}
    assert w["check"] == {"key": "zero_error", "equals": "OK"}


@pytest.mark.parametrize("head", ["photodiode", "thermal", "pyro"])
def test_read_paths_resolve_against_real_status(meter, head):
    _swap(meter, head)
    meter.check_head()                                # pyro: fetch the pulse rate
    meter.acquire()
    for _ in range(meter.cfg.acquisition.readings):
        meter.poll_once()
    st = status_to_dict(meter.status())
    for p in build_manifest(meter)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None or p["id"] in (
                "hw_error", "dark_offset"), p["id"]


@pytest.mark.parametrize("head", ["photodiode", "pyro"])
def test_settle_and_wait_keys_exist_in_status(meter, head):
    _swap(meter, head)
    st = status_to_dict(meter.status())
    for p in build_manifest(meter)["parameters"]:
        settle = p.get("settle") or {}
        if "key" in settle:
            assert settle["key"] in st, p["id"]
        ready = (p.get("wait") or {}).get("ready", {})
        for k in ("setpoint_key", "flag_key"):
            if k in ready:
                assert ready[k] in st, p["id"]


def test_acquire_timeout_is_derived_not_fixed(meter):
    before = _by_id(build_manifest(meter))["power"]["acquire"]["timeout_s"]
    meter.set_settle(50.0)
    meter.set_acquisition(500)
    after = _by_id(build_manifest(meter))["power"]["acquire"]["timeout_s"]
    assert after > before and after >= 100.0


def test_range_settle_survives_float_rounding_at_the_top(meter):
    """scan-core sends display x scale. At the top of the range that is not
    exactly the clamped value the service echoes; the tolerance must allow it
    while still telling range steps apart."""
    meter.set_auto_range(False)
    r = _by_id(build_manifest(meter))["range"]
    wire = r["max"] * r["scale"]                     # what scan-core would send
    meter.set_range(wire)
    echoed = meter.status().range_set
    assert abs(echoed - wire) <= r["settle"]["tol"]
    assert r["settle"]["tol"] < r["min"] * r["scale"]  # finer than one step


# ---- declared types (scan-core STORES each detector in its declared type) ----

def _type_problem(d, v):
    """Why status value `v` does not fit descriptor `d` (the promise scan-core's
    storage keeps, developer notes 4b), or "". None = not measured, always fits."""
    if v is None:
        return ""
    t = d["type"]
    for x in (v if isinstance(v, list) else [v]):
        if x is None:
            continue
        if t == "bool" and not isinstance(x, bool):
            return f"bool reads {x!r}"
        if t == "int":
            if isinstance(x, bool) or x != int(x):
                return f"int reads {x!r}"
            if d["kind"] == "indicator" and (x < d.get("min", x) or x > d.get("max", x)):
                return f"{x!r} outside [{d.get('min')}, {d.get('max')}]"
        if t == "enum" and x not in d["options"]:
            return f"{x!r} not in {d['options']}"
        if t == "string" and not isinstance(x, str):
            return f"string reads {x!r}"
        if t == "float" and (isinstance(x, (bool, str)) or not isinstance(x, (int, float))):
            return f"float reads {x!r}"
    return ""


def _misfits(m):
    man = build_manifest(m)
    st = status_to_dict(m.status())
    return [f"{p['id']}: {_type_problem(p, read_path(st, p['read_path']))}"
            for p in man["parameters"] if p.get("read_path")
            and _type_problem(p, read_path(st, p["read_path"]))]


def test_enums_cover_every_flag_and_head_the_code_can_produce(meter):
    """Enumerated from the code: the real driver's warning-code and sensor-
    type maps (incl. unknown codes) and the brain's own 'no_sensor'."""
    from pm400.backends import tlpmx
    from pm400.backends.base import HEAD_KINDS, READING_FLAGS
    p = _by_id(build_manifest(meter))
    assert p["flag"]["type"] == "enum" and p["flag"]["options"] == list(READING_FLAGS)
    assert p["head"]["type"] == "enum" and p["head"]["options"] == list(HEAD_KINDS)
    for code in (0, tlpmx.WARN_OVERFLOW, tlpmx.WARN_UNDERRUN, tlpmx.WARN_NAN, 12345):
        assert tlpmx.warning_flag(code) in READING_FLAGS
    for code in range(-1, 20):
        assert tlpmx.head_kind(code) in HEAD_KINDS
    assert p["acq_id"]["min"] == 0
    assert "store" not in json.dumps(build_manifest(meter))


@pytest.mark.parametrize("head", ["photodiode", "thermal", "pyro", "none"])
def test_status_fits_its_types_for_every_head(meter, head):
    _swap(meter, head)
    assert _misfits(meter) == []
    if head != "none":
        meter.acquire()
        for _ in range(60):
            meter.poll_once()
        assert _misfits(meter) == []
    p = _by_id(build_manifest(meter))
    if "zero_id" in p:
        assert p["zero_id"]["type"] == "int" and p["zero_id"]["min"] == 0
