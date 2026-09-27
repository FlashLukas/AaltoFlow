"""The discrete tables against the manual (Tables 6-2 and 6-4)."""

import pytest

from sr7230 import tables


def test_time_constant_table_matches_the_manual():
    tcs = tables.TIME_CONSTANTS_S
    assert len(tcs) == 31
    assert tcs[0] == 10e-6 and tcs[12] == 0.1 and tcs[15] == 1.0 and tcs[30] == 100e3
    assert tcs[8] == tables.TC_MIN_NORMAL_S == 5e-3        # TC 8 = 5 ms


def test_sensitivity_tables_match_the_manual():
    v = tables.sensitivity_table("A")
    assert min(v) == 3 and max(v) == 27
    assert v[3] == 10e-9 and v[18] == 1e-3 and v[24] == 0.1 and v[27] == 1.0
    hb = tables.sensitivity_table("I high-BW")
    assert hb[3] == pytest.approx(10e-15) and hb[27] == pytest.approx(1e-6)
    ln = tables.sensitivity_table("I low-noise")
    assert min(ln) == 7 and ln[7] == pytest.approx(2e-15) and ln[27] == pytest.approx(10e-9)


def test_labels():
    assert tables.sensitivity_label(24, "A") == "100 mV"
    assert tables.sensitivity_label(24, "I high-BW") == "100 nA"
    assert tables.sensitivity_label(3, "I low-noise") == "--"
    assert tables.tc_label(0.1) == "100 ms" and tables.tc_label(2e3) == "2 ks"
    assert tables.unit_for("I low-noise") == "A" and tables.unit_for("-B") == "V"


def test_fast_mode_changes_what_is_allowed():
    normal = tables.allowed_time_constants(False, 1e-6, 1e6)
    fast = tables.allowed_time_constants(True, 1e-6, 1e6)
    assert min(normal) == 5e-3 and min(fast) == 10e-6
    assert tables.allowed_slopes(True) == (6, 12)
    assert tables.allowed_slopes(False) == (6, 12, 18, 24)


def test_nearest_is_on_a_log_scale():
    allowed = tables.TIME_CONSTANTS_S
    assert allowed[tables.nearest_tc_index(0.07, allowed)] == 0.05   # 0.07/0.05 < 0.1/0.07
    assert allowed[tables.nearest_tc_index(0.08, allowed)] == 0.1
    assert allowed[tables.nearest_tc_index(3e-3, allowed)] == 2e-3
