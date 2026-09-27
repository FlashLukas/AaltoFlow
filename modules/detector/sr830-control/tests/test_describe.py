"""The `describe` manifest -- this service's self-description.

Beyond the checks every module has (bounds follow cfg, revision tracks bounds
but not values, every read_path resolves, both status paths agree), the SR830
has its own:

  * every scan detector carries the SAME acquire group, keyed on the id the
    trigger returns (one settle per point, and not fooled by the previous one)
  * `freq` flips between control and indicator with the reference source
  * current input changes the unit of X/Y/R and the sensitivity options
  * above 200 Hz the long time constants leave the options
  * auto functions are routine actions with a wait block keyed on their run id
"""

import time

import pytest

pytest.importorskip("zmq")

from sr830 import tables
from sr830.config import Config
from sr830.lockin import STREAM_CHANNELS
from sr830.sim_system import build_sim_system
from sr830.net.describe import build_manifest, read_path
from sr830.net.protocol import status_to_dict
from sr830.net.service import Sr830Service
from sr830.net.client import Sr830Client


@pytest.fixture
def brain():
    cfg = Config()
    li, sim = build_sim_system(cfg, seed=4)
    li.start()
    yield cfg, li
    li.shutdown()


def _by_id(m):
    return {p["id"]: p for p in m["parameters"]}


def test_manifest_shape_and_required_fields(brain):
    cfg, li = brain
    m = build_manifest(li)
    assert m["module"] == "sr830" and isinstance(m["revision"], int)
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids)), "duplicate parameter ids"
    for p in m["parameters"]:
        assert p["kind"] in ("control", "indicator", "action"), p["id"]
        if p["kind"] == "control":
            assert "verb" in p["set"] and "arg" in p["set"], p["id"]
            if p["type"] == "enum":
                assert p["options"], p["id"]
        if p["kind"] == "indicator":
            assert p["read_path"], p["id"]


def test_every_read_path_resolves_against_a_real_status(brain):
    cfg, li = brain
    li.acquire()
    deadline = time.monotonic() + 3
    while li.status().acquiring and time.monotonic() < deadline:
        time.sleep(0.02)
    st = status_to_dict(li.status())
    for p in build_manifest(li)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None, p["id"]


def test_every_set_verb_is_a_real_verb(brain):
    """A descriptor naming a verb the service does not know would only fail
    when someone clicks it. Send each control its own current value, and run
    every auto action."""
    cfg, li = brain
    li.shutdown()
    svc = Sr830Service(li, host="127.0.0.1", cmd_port=17206, pub_port=17207)
    svc.start()
    cli = Sr830Client(host="127.0.0.1", cmd_port=17206, pub_port=17207)
    try:
        st = cli._cmd({"cmd": "status"})["status"]
        for p in build_manifest(li)["parameters"]:
            if p["kind"] != "control":
                continue
            value = read_path(st, p["read_path"])
            msg = {"cmd": p["set"]["verb"], p["set"]["arg"]: value,
                   **p["set"].get("extra", {})}
            r = cli._cmd(msg)
            assert r["ok"], (p["id"], r)
        for aid in ("auto_gain", "auto_reserve", "auto_phase"):
            r = cli._cmd({"cmd": aid})
            assert r["ok"] and "auto_id" in r
            cli.wait_auto(r["auto_id"], timeout_s=10.0)
    finally:
        cli.shutdown()
        svc.stop()


def test_scan_detectors_share_one_acquisition_keyed_on_the_trigger_reply(brain):
    cfg, li = brain
    m = _by_id(build_manifest(li))
    scan_ids = ["x", "y", "r", "theta", "aux1", "aux2", "aux3", "aux4",
                "sample_freq", "overload"]
    blocks = [m[i]["acquire"] for i in scan_ids]
    assert all(b == blocks[0] for b in blocks)
    b = blocks[0]
    assert b["trigger_verb"] == "acquire" and b["target_key"] == "acq_id"
    assert b["ready"] == {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                          "flag_key": "acquiring", "invert": True}
    assert "acquire" not in m["live_r"]          # live values are NOT scan-safe
    streamed = {pid for pid, p in m.items() if "stream" in p}
    assert streamed == set(STREAM_CHANNELS)


def test_frequency_is_a_control_only_on_internal_reference(brain):
    cfg, li = brain
    m = _by_id(build_manifest(li))
    assert m["freq"]["kind"] == "control"
    assert m["freq"]["max"] == cfg.limits.freq_max_Hz
    assert "unlocked" not in m
    rev_int = build_manifest(li)["revision"]
    li.set_reference_source("external")
    m = _by_id(build_manifest(li))
    assert m["freq"]["kind"] == "indicator" and "unlocked" in m
    assert build_manifest(li)["revision"] != rev_int


def test_harmonic_and_frequency_limits_follow_each_other(brain):
    cfg, li = brain
    li.set_harmonic(4)
    m = _by_id(build_manifest(li))
    assert m["freq"]["max"] == pytest.approx(cfg.limits.freq_max_Hz / 4)
    li.set_frequency(10000.0)
    assert _by_id(build_manifest(li))["harmonic"]["max"] == 10


def test_current_input_changes_units_and_sensitivity_options(brain):
    cfg, li = brain
    rev0 = build_manifest(li)["revision"]
    li.set_input_source("I100M")
    m = _by_id(build_manifest(li))
    assert m["x"]["unit"] == "A" and m["r"]["unit"] == "A"
    assert m["sensitivity"]["options"] == list(tables.SENS_LABELS_A)
    assert build_manifest(li)["revision"] != rev0


def test_long_time_constants_leave_the_options_above_200_Hz(brain):
    cfg, li = brain
    m = _by_id(build_manifest(li))              # 1 kHz
    assert m["time_constant"]["options"][-1] == "30 s"
    li.set_frequency(100.0)
    m = _by_id(build_manifest(li))
    assert m["time_constant"]["options"][-1] == "30 ks"
    cfg.limits.tc_max = "10 s"
    assert _by_id(build_manifest(li))["time_constant"]["options"][-1] == "10 s"


def test_auto_functions_are_routine_actions(brain):
    cfg, li = brain
    m = _by_id(build_manifest(li))
    for aid in ("auto_gain", "auto_reserve", "auto_phase"):
        w = m[aid]["wait"]
        assert w["target_key"] == "auto_id"
        assert w["ready"]["setpoint_key"] == "auto_id"
        assert w["ready"]["flag_key"] == "auto_busy" and w["ready"]["invert"] is True


def test_limits_come_from_the_config_and_revision_follows(brain):
    cfg, li = brain
    m = build_manifest(li)
    assert _by_id(m)["sine_out"]["max"] == cfg.limits.sine_max_V
    rev0 = m["revision"]
    cfg.limits.sine_max_V = 1.0
    assert _by_id(build_manifest(li))["sine_out"]["max"] == 1.0
    assert build_manifest(li)["revision"] != rev0
    cfg.limits.sine_max_V = Config().limits.sine_max_V
    assert build_manifest(li)["revision"] == rev0


def test_revision_ignores_measured_values(brain):
    cfg, li = brain
    rev0 = build_manifest(li)["revision"]
    time.sleep(0.2)                           # the poller updates live values
    assert build_manifest(li)["revision"] == rev0


def test_describe_over_the_wire_and_both_status_paths_agree(brain):
    cfg, li = brain
    li.shutdown()                            # the service starts it again
    svc = Sr830Service(li, host="127.0.0.1", cmd_port=17208, pub_port=17209)
    svc.start()
    cli = None
    try:
        cli = Sr830Client(host="127.0.0.1", cmd_port=17208, pub_port=17209)
        m = cli.describe()
        assert m["module"] == "sr830"
        direct = cli._cmd({"cmd": "status"})["status"]
        assert direct["describe_rev"] == m["revision"]
    finally:
        if cli is not None:
            cli.shutdown()
        svc.stop()
