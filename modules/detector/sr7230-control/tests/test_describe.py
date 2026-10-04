"""The `describe` manifest -- this service's self-description.

Beyond the checks every module has (bounds follow cfg, revision tracks bounds
but not values, every read_path resolves, both status paths agree), the 7230
has its own:

  * every scan detector carries the SAME acquire group, keyed on the id the
    trigger returns (gotcha #17)
  * fast mode, the input and the reference each MOVE the manifest
  * the auto operations are waitable actions with an outcome check
"""

import time

import pytest

pytest.importorskip("zmq")

from sr7230.config import Config
from sr7230.sim_system import build_sim_system
from sr7230.net.describe import build_manifest, read_path
from sr7230.net.protocol import status_to_dict
from sr7230.net.service import Sr7230Service
from sr7230.net.client import Sr7230Client

CMD_PORT = 17222
PUB_PORT = 17223


@pytest.fixture
def brain():
    cfg = Config()
    li, _ = build_sim_system(cfg, seed=4)
    li.start()
    yield cfg, li
    li.shutdown()


def _by_id(m):
    return {p["id"]: p for p in m["parameters"]}


def test_manifest_shape_and_required_fields(brain):
    cfg, li = brain
    m = build_manifest(li)
    assert m["module"] == "sr7230" and isinstance(m["revision"], int)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids)), "duplicate parameter ids"
    for p in m["parameters"]:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        if p["kind"] == "control":
            assert "verb" in p["set"] and "arg" in p["set"], p["id"]
        if p["kind"] == "indicator":
            assert p["read_path"], p["id"]
        if p["type"] == "enum":
            assert p["options"], p["id"]


def test_every_read_path_resolves_and_enums_read_an_option(brain):
    cfg, li = brain
    li.set_reference("ext_ttl")                  # so ref_locked exists too
    li.acquire()
    deadline = time.monotonic() + 3
    while li.status().acquiring and time.monotonic() < deadline:
        time.sleep(0.02)
    st = status_to_dict(li.status())
    for p in build_manifest(li)["parameters"]:
        if p.get("read_path"):
            v = read_path(st, p["read_path"])
            assert v is not None, p["id"]
            if p["type"] == "enum":
                assert v in p["options"], (p["id"], v)


def test_scan_detectors_share_one_acquisition_keyed_on_the_trigger_reply(brain):
    cfg, li = brain
    m = _by_id(build_manifest(li))
    scan_ids = ["x", "y", "r", "theta", "adc1", "adc2", "sample_overload", "sample_locked"]
    blocks = [m[i]["acquire"] for i in scan_ids]
    assert all(b == blocks[0] for b in blocks)
    b = blocks[0]
    assert b["trigger_verb"] == "acquire" and b["target_key"] == "acq_id"
    assert b["ready"] == {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                          "flag_key": "acquiring", "invert": True}
    assert "acquire" not in m["live_r"]          # live values are NOT scan-safe


def test_fast_mode_moves_the_time_constant_range_and_the_slopes(brain):
    cfg, li = brain
    m = _by_id(build_manifest(li))
    rev0 = build_manifest(li)["revision"]
    assert m["tc"]["min"] == pytest.approx(5.0)            # ms
    assert m["slope"]["options"] == ["6 dB/oct", "12 dB/oct", "18 dB/oct", "24 dB/oct"]
    li.set_fast_mode(True)
    m = _by_id(build_manifest(li))
    assert m["tc"]["min"] == pytest.approx(0.01)
    assert m["slope"]["options"] == ["6 dB/oct", "12 dB/oct"]
    assert build_manifest(li)["revision"] != rev0


def test_current_mode_changes_units_and_sensitivities(brain):
    cfg, li = brain
    m = _by_id(build_manifest(li))
    assert m["r"]["unit"] == "V" and "100 mV" in m["sensitivity"]["options"]
    li.set_input("I high-BW")
    m = _by_id(build_manifest(li))
    assert m["r"]["unit"] == "A" and m["live_x"]["unit"] == "A"
    assert "100 nA" in m["sensitivity"]["options"]
    assert "100 mV" not in m["sensitivity"]["options"]
    assert m["theta"]["unit"] == "deg"


def test_reference_changes_the_shape(brain):
    cfg, li = brain
    assert "ref_locked" not in _by_id(build_manifest(li))
    li.set_harmonic(2)
    assert _by_id(build_manifest(li))["freq"]["max"] == pytest.approx(60e3)
    li.set_reference("ext_analog")
    m = _by_id(build_manifest(li))
    assert "ref_locked" in m and m["freq"]["max"] == pytest.approx(120e3)


def test_limits_come_from_the_config_and_revision_follows(brain):
    cfg, li = brain
    m = build_manifest(li)
    assert _by_id(m)["amplitude"]["max"] == cfg.limits.amplitude_max_V
    assert _by_id(m)["amplitude"]["danger"] is True
    rev0 = m["revision"]
    cfg.limits.amplitude_max_V = 0.2
    assert build_manifest(li)["revision"] != rev0
    cfg.limits.amplitude_max_V = Config().limits.amplitude_max_V
    assert build_manifest(li)["revision"] == rev0


def test_revision_ignores_measured_values(brain):
    cfg, li = brain
    rev0 = build_manifest(li)["revision"]
    time.sleep(0.2)                           # the poller updates live values
    assert build_manifest(li)["revision"] == rev0


def test_auto_actions_are_waitable_with_an_outcome_check(brain):
    cfg, li = brain
    m = _by_id(build_manifest(li))
    for op in ("auto_phase", "auto_sensitivity", "auto_measure"):
        w = m[op]["wait"]
        assert w["target_key"] == "auto_id"
        assert w["ready"]["flag_key"] == "auto_busy"
        assert w["check"] == {"key": "auto_error", "equals": ""}


def test_describe_over_the_wire_and_both_status_paths_agree(brain):
    cfg, li = brain
    li.shutdown()                            # the service starts it again
    svc = Sr7230Service(li, host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT)
    svc.start()
    cli = None
    try:
        cli = Sr7230Client(host="127.0.0.1", cmd_port=CMD_PORT, pub_port=PUB_PORT)
        m = cli.describe()
        assert m["module"] == "sr7230"
        direct = cli._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
    finally:
        if cli is not None:
            cli.shutdown()
        svc.stop()


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


def _misfits(li):
    m = build_manifest(li)
    st = status_to_dict(li.status())
    return [f"{p['id']}: {_type_problem(p, read_path(st, p['read_path']))}"
            for p in m["parameters"] if p.get("read_path")
            and _type_problem(p, read_path(st, p["read_path"]))]


def test_declared_types_and_counter(brain):
    cfg, li = brain
    p = _by_id(build_manifest(li))
    assert p["acq_id"]["type"] == "int" and p["acq_id"]["min"] == 0
    assert p["harmonic"]["type"] == "int" and p["harmonic"]["kind"] == "control"
    assert "store" not in str(build_manifest(li))
    n = li.acquire()
    deadline = time.monotonic() + 15
    while li.status().acquiring and time.monotonic() < deadline:
        time.sleep(0.05)
    assert li.status().acq_id == n
    assert _misfits(li) == []


def test_every_sensitivity_and_slope_readback_fits(brain):
    """Enumerated from the code, not one snapshot: every SEN index the 7230
    can report (0..31) in every input mode, and every slope with fast mode on
    and off (the adopted state can hold one fast mode does not offer). An
    index outside the mode's table reads None -- never a '--' placeholder,
    which is not an option and would be lost in a scan."""
    from sr7230.config import INPUT_MODES, SLOPES_DB, REF_SOURCES
    cfg, li = brain
    for mode in INPUT_MODES:
        cfg.signal.input = mode
        for i in range(32):
            cfg.signal.sensitivity_index = i
            assert _misfits(li) == [], (mode, i)
            sens = li.status().sensitivity
            assert sens is None or sens != "--"
    for fast in (False, True):
        cfg.filter.fast_mode = fast
        for db in SLOPES_DB:
            cfg.filter.slope_db = db
            assert _misfits(li) == [], (fast, db)
    for src in REF_SOURCES:                    # ref_locked: bool or None
        cfg.reference.source = src
        assert _misfits(li) == [], src
