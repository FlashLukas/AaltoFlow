"""The describe manifest: ids, live limits, the array detector, acquire and
wait blocks, and a revision that moves when a limit does."""

import json

import pytest

from ccs200.config import Config
from ccs200.net.describe import build_manifest, read_path
from ccs200.net.protocol import status_to_dict
from ccs200.sim_system import build_sim_system
from ccs200.spectrometer import Spectrometer


@pytest.fixture
def spec():
    cfg = Config()
    cfg.scan.continuous = False
    s, _ = build_sim_system(cfg, realtime=False, seed=1)
    s.start(run=False)
    yield s
    s.shutdown()


def _params(s):
    return {p["id"]: p for p in build_manifest(s)["parameters"]}


def test_manifest_is_json_and_names_the_module(spec):
    m = build_manifest(spec)
    json.dumps(m, allow_nan=False)                    # strict JSON: no NaN anywhere
    assert m["module"] == "ccs200" and m["schema"] == 1
    ids = [p["id"] for p in m["parameters"]]
    assert len(ids) == len(set(ids))


def test_spectrum_is_a_real_array_detector_read_by_command(spec):
    p = _params(spec)["spectrum"]
    assert p["type"] == "array" and p["dtype"] == "float" and p["shape"] == ["wavelength"]
    dim = p["dims"][0]
    assert dim["name"] == "wavelength" and dim["unit"] == "nm" and dim["length"] == 3648
    assert dim["coord_verb"] == "get_wavelengths" and dim["coord_key"] == "values"
    assert p["read"] == {"verb": "get_trace", "key": "spectrum", "args": {"which": "sample"}}
    assert p["read_path"] is None


def test_scalar_detectors_share_one_acquire_group(spec):
    p = _params(spec)
    ids = ("spectrum", "peak_wavelength", "peak_intensity", "integrated_intensity", "saturated")
    acq = [p[i]["acquire"] for i in ids]
    assert all(a == acq[0] for a in acq)
    a = acq[0]
    assert a["trigger_verb"] == "acquire" and a["target_key"] == "acq_id"
    assert a["ready"] == {"policy": "adopt_then_flag", "setpoint_key": "acq_id",
                          "flag_key": "acquiring", "invert": True}


def test_read_paths_resolve_in_a_real_status(spec):
    spec.acquire()
    while spec.status().acquiring:
        spec.step()
    st = status_to_dict(spec.status())
    for p in build_manifest(spec)["parameters"]:
        if p["read_path"]:
            assert read_path(st, p["read_path"]) is not None or p["id"] in (
                "dark_integration_time",), p["id"]
    assert read_path(st, ["sample", "peak_nm"]) == pytest.approx(546.07, abs=0.2)


def test_controls_have_live_limits_verbs_and_settle(spec):
    p = _params(spec)
    it = p["integration_time"]
    assert it["unit"] == "ms" and it["scale"] == 1e-3
    assert it["min"] == pytest.approx(0.01) and it["max"] == pytest.approx(60000)
    assert it["set"] == {"verb": "set_integration_time", "arg": "integration_time_s"}
    for pid in ("integration_time", "averages", "dark_subtract", "continuous",
                "window_min", "window_max", "light_on"):
        assert p[pid]["settle"]["policy"] == "echoes", pid


def test_actions_carry_wait_blocks(spec):
    p = _params(spec)
    w = p["take_dark"]["wait"]
    assert w["target_key"] == "acq_id" and w["ready"]["flag_key"] == "acquiring"
    assert p["clear_dark"]["wait"] == {"ready": {"policy": "immediate"}}
    assert "wait" not in p["acquire"] and "wait" not in p["abort"]


def test_revision_moves_with_the_window_and_the_timeout(spec):
    r0 = build_manifest(spec)["revision"]
    spec.set_window(500.0, 600.0)
    r1 = build_manifest(spec)["revision"]
    assert r1 != r0
    assert _params(spec)["window_min"]["max"] == pytest.approx(599.0)
    spec.set_integration_time(30.0)
    spec.set_averages(4)
    assert _params(spec)["spectrum"]["acquire"]["timeout_s"] >= 4 * 30.0
    assert build_manifest(spec)["revision"] != r1
    r2 = build_manifest(spec)["revision"]
    spec.acquire()                                    # a value change is NOT a revision
    assert build_manifest(spec)["revision"] == r2


def test_simulation_group_only_on_the_simulator():
    from fake_tlccs import FakeTlccs
    from ccs200.backends.tlccs import TlccsSpectrometer
    sim_ids = {"light_on", "lamp_level_per_s", "line_level_per_s", "dark_rate_per_s"}
    s, _ = build_sim_system(Config(), realtime=False)
    assert sim_ids <= set(_params(s))
    real = Spectrometer(TlccsSpectrometer(resource="USB0::x", dll=FakeTlccs()), Config())
    real.start(run=False)
    try:
        assert not sim_ids & set(_params(real))
        assert "Thorlabs" in build_manifest(real)["label"]
    finally:
        real.shutdown()
