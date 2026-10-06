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
