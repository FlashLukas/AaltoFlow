"""The declared TYPE of every detector is how scan-core STORES it (2026-10-04,
developer notes 4b). An enum value outside its options is stored as "not
measured"; an int outside its declared min/max STOPS a scan. So each enum must
cover every value the module can report -- checked against the real
backend's code -> name tables, not one snapshot -- and the acquisition counter
promises only what it keeps (>= 0, no upper bound)."""

import pytest

from ls455.backends import ls455 as real
from ls455.backends.base import MODES, PEAK_DISPLAYS, PEAK_MODES, RMS_BANDS, UNIT_CODES
from ls455.config import Config
from ls455.net.describe import build_manifest
from ls455.sim_system import build_sim_system


def _meter(**kw):
    cfg = Config()
    cfg.acquisition.settle_time_constants = 0.0
    m, _ = build_sim_system(cfg, realtime=False, **kw)
    m.start(poll=False)
    m.poll_once()
    return m


def _by_id(m):
    return {p["id"]: p for p in build_manifest(m)["parameters"]}


def test_enums_cover_every_name_the_real_backend_can_map_to():
    m = _meter()
    try:
        p = _by_id(m)
        assert set(p["mode"]["options"]) == set(MODES) >= set(real._CODE_MODES.values())
        assert set(p["display_unit"]["options"]) == set(UNIT_CODES) \
            >= set(real._CODE_UNITS.values())
        m.set_mode("rms")
        assert set(_by_id(m)["rms_band"]["options"]) == set(RMS_BANDS)
    finally:
        m.shutdown()


@pytest.mark.parametrize("pm", PEAK_MODES)
@pytest.mark.parametrize("pd", PEAK_DISPLAYS)
def test_peak_settings_are_enums_covering_every_front_panel_choice(pm, pd):
    # the real backend indexes PEAK_MODES / PEAK_DISPLAYS with the RDGMODE
    # fields (or falls back to their first entries), so these tuples ARE the
    # complete set; every combination is adopted here from the simulated meter
    m = _meter(peak_mode=pm, peak_display=pd)
    try:
        m.set_mode("peak")
        p = _by_id(m)
        assert p["peak_mode"]["type"] == "enum"
        assert p["peak_display"]["type"] == "enum"
        assert p["peak_mode"]["options"] == list(PEAK_MODES)
        assert p["peak_display"]["options"] == list(PEAK_DISPLAYS)
        st = m.status()
        assert (st.peak_mode, st.peak_display) == (pm, pd)
    finally:
        m.shutdown()


def test_acq_id_is_a_non_negative_counter_without_an_upper_bound():
    m = _meter()
    try:
        d = _by_id(m)["acq_id"]
        assert d["type"] == "int" and d["min"] == 0 and "max" not in d
        first = m.acquire()
        assert first >= 1 and m.status().acq_id == first
    finally:
        m.shutdown()
