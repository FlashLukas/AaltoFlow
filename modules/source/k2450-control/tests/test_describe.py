"""Tests for the `describe` manifest -- the SMU's self-description.

The properties that matter and are easy to get wrong:
  * every bound is read LIVE from the brain (a restated limit is a limit with
    two homes);
  * `revision` changes when the STRUCTURE or the BOUNDS change (source
    function, output boxes, ranges) and not when a measured value changes;
  * the detectors carry an acquire block whose wait targets the trigger's id,
    and the source levels settle on adoption AND `settled`.
"""

import pytest

pytest.importorskip("zmq")

from k2450.config import Config
from k2450.sim_system import build_sim_system
from k2450.net.describe import build_manifest, read_path
from k2450.net.protocol import status_to_dict
from k2450.net.service import K2450Service
from k2450.net.client import K2450Client


def _brain():
    cfg = Config()
    smu, _ = build_sim_system(cfg, realtime=False, seed=0)
    smu.start(poll=False)
    return cfg, smu


def _by_id(manifest):
    return {p["id"]: p for p in manifest["parameters"]}


def test_manifest_shape_and_required_fields():
    cfg, smu = _brain()
    try:
        m = build_manifest(smu)
        assert m["module"] == "k2450"
        assert isinstance(m["revision"], int)
        ids = [p["id"] for p in m["parameters"]]
        assert len(ids) == len(set(ids)), "duplicate parameter ids"
        st = status_to_dict(smu.status())
        for p in m["parameters"]:
            assert p["kind"] in ("control", "indicator", "action"), p["id"]
            assert p["type"] in ("float", "int", "bool", "enum", "string", "action"), p["id"]
            if p["kind"] == "control":
                assert "set" in p and "verb" in p["set"] and "arg" in p["set"], p["id"]
                assert "settle" in p, p["id"]
            if p["kind"] == "indicator":
                assert p["read_path"], p["id"]
                # every indicator resolves in a REAL status dict (the sample
                # is empty before the first acquisition, so skip those)
                if p["read_path"][0] != "sample":
                    assert p["read_path"][0] in st, p["id"]
    finally:
        smu.shutdown()


def test_detectors_have_an_id_targeted_acquire_block():
    cfg, smu = _brain()
    try:
        d = _by_id(build_manifest(smu))
        for pid in ("voltage", "current", "resistance", "voltage_std",
                    "current_std", "resistance_std"):
            acq = d[pid]["acquire"]
            assert acq["trigger_verb"] == "acquire"
            assert acq["target_key"] == "acq_id"
            assert acq["ready"]["policy"] == "adopt_then_flag"
            assert acq["ready"]["setpoint_key"] == "acq_id"
            assert acq["group"] == "sample"          # ONE acquisition for all of them
    finally:
        smu.shutdown()


def test_source_level_settles_on_adoption_and_settled():
    cfg, smu = _brain()
    try:
        v = _by_id(build_manifest(smu))["source_voltage"]
        assert v["kind"] == "control"
        assert v["settle"] == {"policy": "adopt_then_flag",
                               "setpoint_key": "source_voltage_set_V",
                               "flag_key": "settled"}
    finally:
        smu.shutdown()


def test_function_switch_changes_shape_and_revision():
    cfg, smu = _brain()
    try:
        m0 = build_manifest(smu)
        d0 = _by_id(m0)
        assert d0["source_voltage"]["kind"] == "control"
        assert d0["current_limit"]["kind"] == "control"
        assert d0["source_current"]["kind"] == "indicator"
        smu.set_source_function("current")
        m1 = build_manifest(smu)
        d1 = _by_id(m1)
        assert m1["revision"] != m0["revision"]
        assert d1["source_current"]["kind"] == "control"
        assert d1["source_current"]["unit"] == "uA"
        assert d1["voltage_limit"]["kind"] == "control"
        assert d1["source_voltage"]["kind"] == "indicator"
        # uA on the wire: bounds and read path in the same unit
        lo, hi = smu.level_limits("current")
        assert d1["source_current"]["max"] == pytest.approx(hi * 1e6)
        assert d1["source_current"]["read_path"] == ["source_current_set_uA"]
    finally:
        smu.shutdown()


def test_output_boxes_move_the_bounds_and_the_revision():
    cfg, smu = _brain()
    try:
        m0 = build_manifest(smu)
        assert _by_id(m0)["source_voltage"]["max"] == cfg.limits.voltage_max_V
        smu.set_current_limit(0.5)            # above 105 mA -> V confined to 21 V
        m1 = build_manifest(smu)
        assert _by_id(m1)["source_voltage"]["max"] == cfg.limits.box_voltage_V
        assert m1["revision"] != m0["revision"]
    finally:
        smu.shutdown()


def test_revision_ignores_values():
    cfg, smu = _brain()
    try:
        rev0 = build_manifest(smu)["revision"]
        smu.set_output(True)
        smu.poll_once()                      # a reading arrives
        smu.set_voltage(0.3)                 # a value, not a bound
        assert build_manifest(smu)["revision"] == rev0
        cfg.limits.voltage_max_V = 20.0
        assert build_manifest(smu)["revision"] != rev0
        cfg.limits.voltage_max_V = 210.0
        assert build_manifest(smu)["revision"] == rev0, "revision must be derived"
    finally:
        smu.shutdown()


def test_ranges_are_controls_only_when_fixed():
    cfg, smu = _brain()
    try:
        d = _by_id(build_manifest(smu))
        assert d["source_range"]["kind"] == "indicator"
        assert d["measure_range"]["kind"] == "indicator"
        smu.set_measure_range(1e-3)
        d = _by_id(build_manifest(smu))
        assert d["measure_range"]["kind"] == "control"
        assert d["measure_range"]["unit"] == "A"
    finally:
        smu.shutdown()


def test_output_is_danger_and_output_off_is_a_routine_action():
    cfg, smu = _brain()
    try:
        d = _by_id(build_manifest(smu))
        assert d["output"].get("danger") is True
        assert d["output_off"]["kind"] == "action"
        assert d["output_off"]["wait"]["ready"]["key"] == "output"
    finally:
        smu.shutdown()


def test_read_path_survives_a_missing_branch():
    cfg, smu = _brain()
    try:
        m = build_manifest(smu)
        for p in m["parameters"]:
            if p["read_path"]:
                read_path({}, p["read_path"])        # must not raise
        assert read_path({"sample": {}}, ["sample", "current_A"]) is None
    finally:
        smu.shutdown()


def test_describe_over_the_wire_and_both_status_paths_agree():
    """A field in only one status path is a field that vanishes intermittently."""
    cfg = Config()
    smu, _ = build_sim_system(cfg, seed=0)
    svc = K2450Service(smu, host="127.0.0.1", cmd_port=17044, pub_port=17045)
    svc.start()
    client = None
    try:
        client = K2450Client(host="127.0.0.1", cmd_port=17044, pub_port=17045)
        client.start()
        m = client.describe()
        assert m["module"] == "k2450"
        direct = client._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
    finally:
        if client is not None:
            client.shutdown()
        svc.stop()


def test_source_current_is_uA_in_both_modes_and_acquire_timeout_is_derived():
    _, smu = _brain()
    by = {p["id"]: p for p in build_manifest(smu)["parameters"]}
    assert by["source_current"]["unit"] == "uA"
    assert by["source_current"]["read_path"] == ["source_current_set_uA"]
    smu.set_source_function("current")
    by = {p["id"]: p for p in build_manifest(smu)["parameters"]}
    assert by["source_current"]["unit"] == "uA"
    # 1000 readings at 10 NPLC take ~250 s: the declared wait must cover it
    smu.set_nplc(10.0)
    smu.set_acquisition(1000)
    by = {p["id"]: p for p in build_manifest(smu)["parameters"]}
    assert by["voltage"]["acquire"]["timeout_s"] > 600
    smu.shutdown()
