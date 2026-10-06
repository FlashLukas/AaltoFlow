"""The measured spur table (spurs.py + sg12000l_spurs.json) the GUI draws.

These pin the physics the measurement showed, so a re-extraction that breaks
the table (or a JSON that does not ship) is caught offline."""

import json

import pytest

from dssg import spurs


def _by_tag(f_hz, p_dbm):
    return {tag: (fr, lv) for fr, lv, tag in spurs.spur_lines(f_hz, p_dbm)}


def test_table_ships_and_has_no_serial_number():
    assert spurs.table_ok()
    tab = spurs.load_table()
    assert set(tab["model"]) <= set(spurs.MULTIPLE)
    # model + firmware only: the unit's *IDN? serial must never reach git
    assert tab["source"]["instrument"].startswith("SG12000L fw ")
    assert len(json.dumps(tab["source"])) < 2000


def test_divider_region_strong_third_flat_in_power():
    lo, hi = _by_tag(0.6e9, -20.0), _by_tag(0.6e9, 5.0)
    assert lo["3f"][0] == pytest.approx(1.8e9)
    dbc_lo, dbc_hi = lo["3f"][1] - -20.0, hi["3f"][1] - 5.0
    assert -20 < dbc_hi < -8                      # square-wave 3rd harmonic
    assert dbc_lo == pytest.approx(dbc_hi, abs=2)  # made before the attenuator


def test_amplifier_region_second_harmonic_grows_with_power():
    dbc = {p: _by_tag(4.0e9, p)["2f"][1] - p for p in (-5.0, 5.0)}
    assert dbc[5.0] > -15                         # strong at full output
    assert dbc[5.0] - dbc[-5.0] == pytest.approx(10, abs=3)  # ~1 dBc per dB


def test_power_beyond_the_scan_is_clamped():
    a, b = _by_tag(4.0e9, 5.0)["2f"][1], _by_tag(4.0e9, 15.0)["2f"][1]
    assert b - a == pytest.approx(10.0)           # carrier moves, dBc does not


def test_doubler_region_leaks_half_frequency_only_up_high():
    assert "f/2" in _by_tag(11e9, 0.0)
    assert "f/2" not in _by_tag(5e9, 5.0)         # below the floor there


def test_nothing_invented_where_nothing_was_measured():
    assert _by_tag(0.1e9, 0.0) == {}              # carrier below the sweep
    assert "2f" not in _by_tag(2.6e9, 5.0)        # below the analyser floor
    assert "2f" not in _by_tag(9e9, 5.0)          # 18 GHz: above the analyser
    assert spurs.harmonics_measured(4e9)
    assert not spurs.harmonics_measured(9e9)
    assert not spurs.harmonics_measured(0.1e9)


def test_missing_file_degrades_to_no_spurs(tmp_path):
    assert spurs.load_table.__wrapped__(tmp_path / "nope.json") == {}


def test_a_carrier_takes_what_the_nearest_measured_carrier_showed():
    """Lab PC 2026-10-06 (Lukas: "this is missing harmonics for sure"): at
    3300 MHz / +5 dBm the screen drew NO 2nd harmonic, though the measured
    carrier 3304.88 MHz showed it at -15.4 dBc. Segments stopped exactly at
    their first measured carrier (stored rounded, 3.3049 GHz), so the measured
    point itself and everything below it looked clean."""
    from dssg import spurs
    for f in (3.30488e9, 3.300e9, 3.20e9):        # nearer to 3304.88 than to 3024.4
        lines = {t: p for _f, p, t in spurs.spur_lines(f, 5.0)}
        assert "2f" in lines, f
        assert -12.5 < lines["2f"] < -9.0, (f, lines["2f"])   # +5 dBm, about -15 dBc
    # nearer to 3024.4 MHz, where the 2nd harmonic was below the floor
    assert "2f" not in {t for _f, _p, t in spurs.spur_lines(3.10e9, 5.0)}


def test_a_segment_reaches_half_way_to_the_next_measured_carrier():
    from dssg.spurs import _reach
    assert _reach([3.3049, 4.0], [3.0244, 3.3049, 4.0, 4.3]) == (
        (3.3049 + 3.0244) / 2, (4.0 + 4.3) / 2)
    assert _reach([0.5, 1.0], [0.5, 1.0]) == (0.5, 1.0)       # nothing beyond the data
