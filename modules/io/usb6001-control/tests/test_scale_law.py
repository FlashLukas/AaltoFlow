"""Scale laws (linear / log10), the live running mean and the threshold lamp.

Added for a vacuum chamber: a logarithmic gauge p [mbar] = 10^(U - 5) on ai0,
shown as the mean of the last 5 readings, with an "ATM" lamp at 1100 mbar --
the same as the old LabVIEW panel. Offline: the simulated card is fed fixed
voltages so every number below is exact.
"""

import math
from pathlib import Path

import pytest

from usb6001.config import (AIChannel, Config, is_scaled, lamp_state, sanitise,
                            scale_value)
from usb6001.net.describe import build_manifest
from usb6001.net.protocol import apply_config_dict, config_to_dict
from usb6001.sim_system import build_sim_system

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "vacuum_magnet.ini"


def _gauge(**kw) -> AIChannel:
    ch = AIChannel(enabled=True, name="Vacuum", unit="mbar", law="log10",
                   slope=1.0, offset=-5.0)
    for k, v in kw.items():
        setattr(ch, k, v)
    return ch


# ---- the law itself ----------------------------------------------------------------

def test_log10_law_matches_the_gauge_formula():
    ch = _gauge()
    assert scale_value(ch, 8.0) == pytest.approx(1000.0)          # 10^(8-5)
    assert scale_value(ch, 8.0671) == pytest.approx(10 ** 3.0671)  # the panel's 1167 mbar
    assert scale_value(ch, -1.0) == pytest.approx(1e-6)


def test_linear_law_is_unchanged():
    ch = AIChannel(unit="mT", slope=-25.3199, offset=151.8407)
    assert scale_value(ch, 5.99689) == pytest.approx(0.0, abs=1e-3)
    assert scale_value(ch, 6.99689) == pytest.approx(-25.3199, abs=1e-3)


def test_nan_stays_nan_and_overflow_is_inf_not_an_exception():
    assert math.isnan(scale_value(_gauge(), float("nan")))
    assert scale_value(_gauge(slope=1000.0), 10.0) == float("inf")   # 10^9995


def test_is_scaled_counts_the_law():
    assert not is_scaled(AIChannel())
    assert is_scaled(AIChannel(law="log10"))


# ---- the lamp --------------------------------------------------------------------------

def test_lamp_states():
    ch = _gauge(lamp_above=1100.0)
    assert lamp_state(ch, 1167.0) is True
    assert lamp_state(ch, 1100.0) is True          # "at or above"
    assert lamp_state(ch, 1e-3) is False
    assert lamp_state(ch, float("inf")) is True    # gauge off scale counts as above
    assert lamp_state(ch, float("nan")) is None    # no reading
    assert lamp_state(_gauge(), 1167.0) is None    # no lamp configured (NaN threshold)


# ---- sanitise ------------------------------------------------------------------------------

def test_sanitise_fixes_and_reports_bad_values():
    cfg = Config()
    ch = cfg.ai.channels[0]
    ch.law, ch.smooth_reads, ch.lamp_above = "LOG", 0, float("inf")
    msgs = sanitise(cfg)
    assert ch.law == "linear" and ch.smooth_reads == 1 and math.isnan(ch.lamp_above)
    assert len([m for m in msgs if m.startswith("ai0")]) == 3
    ch.law = " Log10 "
    sanitise(cfg)
    assert ch.law == "log10"


# ---- the brain: smoothing applies to LIVE values only --------------------------------

def _daq_with_gauge(volts_sequence, smooth=5, lamp=1100.0):
    cfg = Config()
    for c in cfg.ai.channels:
        c.enabled = False
    cfg.ai.channels[0] = _gauge(smooth_reads=smooth, lamp_above=lamp, lamp_name="ATM")
    daq, sim = build_sim_system(cfg)
    seq = iter(volts_sequence)
    sim.read_ai = lambda samples, rate: [next(seq)]
    daq.start(poll=False)
    return daq


def test_live_value_is_the_running_mean_of_volts_before_the_law():
    daq = _daq_with_gauge([7.0, 8.0, 9.0, 8.0, 8.0, 9.0])
    for _ in range(5):
        daq.poll_once()
    st = daq.status()
    # mean of 7, 8, 9, 8, 8 = 8.0 V -> 10^3; the mean of the PRESSURES would be
    # (100 + 1000 + 10000 + 1000 + 1000) / 5 = 2620: the order matters
    assert st.ai_V[0] == pytest.approx(8.0)
    assert st.ai[0] == pytest.approx(1000.0)
    daq.poll_once()                                # window slides: 8, 9, 8, 8, 9
    assert daq.status().ai_V[0] == pytest.approx(8.4)
    daq.shutdown()


def test_fresh_sample_is_never_smoothed():
    """A scan point must record a reading taken AFTER it was set, not a mean
    reaching back into readings from before."""
    daq = _daq_with_gauge([7.0, 7.0, 7.0, 7.0, 9.0])
    for _ in range(4):
        daq.poll_once()
    r = daq.read_ai(0)                             # triggers the 5th reading (9 V)
    assert r["values_V"]["ai0"] == pytest.approx(9.0)
    assert r["values"]["ai0"] == pytest.approx(1e4)
    assert daq.status().ai_V[0] == pytest.approx(7.4)   # live: mean of 7,7,7,7,9
    daq.shutdown()


def test_lamp_follows_the_live_value():
    daq = _daq_with_gauge([8.2, 7.0, 7.0, 7.0, 7.0, 7.0], smooth=1)
    daq.poll_once()
    assert daq.status().ai_lamp[0] is True         # 10^3.2 = 1585 mbar >= 1100
    daq.poll_once()
    assert daq.status().ai_lamp[0] is False        # 100 mbar
    assert daq.status().ai_lamp[1] is None         # channel without a lamp
    daq.shutdown()


def test_a_shorter_smoothing_applies_on_the_next_reading():
    daq = _daq_with_gauge([7.0, 7.0, 7.0, 9.0, 9.0])
    for _ in range(3):
        daq.poll_once()
    daq.cfg.ai.channels[0].smooth_reads = 1
    daq.apply_config()
    daq.poll_once()
    assert daq.status().ai_V[0] == pytest.approx(9.0)
    daq.shutdown()


# ---- persistence and the wire ----------------------------------------------------------------

def test_ini_round_trip_keeps_law_smoothing_and_lamp(tmp_path):
    cfg = Config()
    cfg.ai.channels[0] = _gauge(smooth_reads=5, lamp_above=1100.0, lamp_name="ATM")
    p = tmp_path / "u.ini"
    cfg.save(str(p))
    back = Config.load(str(p))
    ch = back.ai.channels[0]
    assert (ch.law, ch.slope, ch.offset, ch.smooth_reads) == ("log10", 1.0, -5.0, 5)
    assert (ch.lamp_above, ch.lamp_name) == (1100.0, "ATM")
    assert math.isnan(back.ai.channels[1].lamp_above)        # "nan" survives too


def test_wire_round_trip():
    src = Config()
    src.ai.channels[0] = _gauge(smooth_reads=3, lamp_above=900.0, lamp_name="ATM")
    dst = Config()
    apply_config_dict(dst, config_to_dict(src))
    ch = dst.ai.channels[0]
    assert (ch.law, ch.smooth_reads, ch.lamp_above, ch.lamp_name) == ("log10", 3, 900.0, "ATM")


def test_describe_has_the_lamp_and_says_the_law():
    daq = _daq_with_gauge([8.0] * 3)
    params = {p["id"]: p for p in build_manifest(daq)["parameters"]}
    assert params["ai0_lamp"]["label"] == "ATM"
    assert params["ai0_lamp"]["read_path"] == ["ai_lamp", 0]
    assert "10^(" in params["ai0"]["help"]
    assert "ai0_V" in params                                 # log law = scaled: volts shown too
    assert "Running mean of the last 5" in params["ai0_live"]["help"]
    daq.shutdown()


# ---- the shipped example ----------------------------------------------------------------------

def test_vacuum_magnet_example_loads_clean():
    cfg = Config.load(str(EXAMPLE))
    assert sanitise(cfg) == []
    a = cfg.ai.channels
    # ai0..ai3 + ai4 (antenna sense for the resistance measurement, 2026-11-10)
    assert [c.enabled for c in a] == [True] * 5 + [False] * 3
    assert a[1].terminal == "DIFF"
    assert scale_value(a[0], 8.0) == pytest.approx(1000.0)
    assert a[0].lamp_name == "ATM" and a[0].lamp_above == 1100.0
    # the Hall probe: 0 mT at its offset 5996.89 mV, -0.0253199 mT per mV
    assert scale_value(a[2], 5.99689) == pytest.approx(0.0, abs=1e-6)
    assert scale_value(a[2], 6.99689) == pytest.approx(-25.3199, rel=1e-9)
    relay = cfg.dio.lines[0]
    assert (relay.direction, relay.name, relay.initial, relay.safe_state) == \
        ("out", "Polarity relay", "leave", "leave")
    assert cfg.ai.rate_Hz * 4 <= cfg.limits.ai_aggregate_max_Hz
