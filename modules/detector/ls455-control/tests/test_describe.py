"""The `describe` manifest: bounds looked up live, controls that appear with
the mode / range setting, acquire block on the scan detectors, revision
follows the shape."""

import json

import pytest

from ls455.backends.base import PROBE_RANGES_mT
from ls455.config import Config
from ls455.net.describe import build_manifest, read_path
from ls455.net.protocol import status_to_dict
from ls455.sim_system import build_sim_system


@pytest.fixture
def meter():
    cfg = Config()
    cfg.acquisition.settle_time_constants = 0.0
    m, _ = build_sim_system(cfg, realtime=False)
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
    assert man["module"] == "ls455"


def test_every_field_is_in_mT(meter):
    d = _by_id(build_manifest(meter))
    for pid in ("field", "field_std", "live_field", "range", "rel_setpoint", "field_rel"):
        assert d[pid]["unit"] == "mT", pid


def test_range_is_indicator_on_auto_and_control_on_manual(meter):
    auto = build_manifest(meter)
    assert _by_id(auto)["range"]["kind"] == "indicator"
    meter.set_auto_range(False)
    manual = build_manifest(meter)
    r = _by_id(manual)["range"]
    assert r["kind"] == "control" and r["set"]["verb"] == "set_range"
    assert (r["min"], r["max"]) == (min(PROBE_RANGES_mT["HSE"]), max(PROBE_RANGES_mT["HSE"]))
    assert r["settle"] == {"policy": "echoes", "key": "range_set_mT", "tol": 1e-9}
    assert manual["revision"] != auto["revision"]


def test_range_bounds_follow_the_probe():
    for probe in ("HST", "UHS"):
        cfg = Config()
        # the METER is on manual range; the module adopts that at start
        m, _ = build_sim_system(cfg, realtime=False, probe=probe, auto_range=False)
        m.start(poll=False)
        r = _by_id(build_manifest(m))["range"]
        assert r["max"] == max(PROBE_RANGES_mT[probe])
        m.shutdown()


def test_mode_swaps_digits_for_band(meter):
    dc = build_manifest(meter)
    assert "dc_digits" in _by_id(dc) and "rms_band" not in _by_id(dc)
    meter.set_mode("rms")
    rms = build_manifest(meter)
    assert "rms_band" in _by_id(rms) and "dc_digits" not in _by_id(rms)
    assert rms["revision"] != dc["revision"]
    assert _by_id(rms)["mode"]["options"] == ["dc", "rms", "peak"]


def test_detectors_say_what_they_report(meter):
    dc = _by_id(build_manifest(meter))
    assert dc["field"]["label"] == "Field (DC)"
    meter.set_mode("peak")
    pk = build_manifest(meter)
    d = _by_id(pk)
    assert d["field"]["label"] == "Field (peak)" and "peak" in d["field"]["help"]
    assert "peak_mode" in d and "peak_display" in d
    assert "dc_digits" not in d and "rms_band" not in d


def test_probe_swap_moves_the_revision():
    cfg = Config()
    m, sim = build_sim_system(cfg, realtime=False, auto_range=False)
    m.start(poll=False)
    try:
        before = build_manifest(m)
        d = _by_id(before)
        assert d["reread_probe"]["wait"] == {"ready": {"policy": "immediate"}}
        assert d["range"]["max"] == max(PROBE_RANGES_mT["HSE"])
        sim.swap_probe("HST")
        m.reread_probe()
        after = build_manifest(m)
        assert _by_id(after)["range"]["max"] == max(PROBE_RANGES_mT["HST"])
        assert after["revision"] != before["revision"]
    finally:
        m.shutdown()


def test_detectors_share_one_acquire_keyed_on_the_trigger_reply(meter):
    d = _by_id(build_manifest(meter))
    for pid in ("field", "field_std"):
        acq = d[pid]["acquire"]
        assert acq["trigger_verb"] == "acquire" and acq["target_key"] == "acq_id"
        assert acq["ready"]["policy"] == "adopt_then_flag"
    assert d["field"]["acquire"] == d["field_std"]["acquire"]
    assert "acquire" not in d["live_field"]


def test_read_paths_resolve_against_real_status(meter):
    meter.set_relative(True, 1.0)
    meter.acquire()
    for _ in range(meter.cfg.acquisition.readings + 1):
        meter.poll_once()
    st = status_to_dict(meter.status())
    for p in build_manifest(meter)["parameters"]:
        if p.get("read_path"):
            assert read_path(st, p["read_path"]) is not None or p["id"] == "hw_error", p["id"]


def test_settle_keys_exist_in_status(meter):
    st = status_to_dict(meter.status())
    for p in build_manifest(meter)["parameters"]:
        settle = p.get("settle") or {}
        if "key" in settle:
            assert settle["key"] in st, p["id"]


def test_zero_is_dangerous_and_relative_here_is_scan_usable(meter):
    d = _by_id(build_manifest(meter))
    assert d["zero"]["danger"] is True
    assert "chamber" in d["zero"]["help"].lower()
    assert d["relative_here"]["wait"] == {"ready": {"policy": "immediate"}}


def test_status_carries_the_field_source_key(meter):
    st = status_to_dict(meter.status())
    assert isinstance(st["measured_field_mT"], float)
