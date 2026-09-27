"""The SR830's discrete steps, against the manual's tables (chapter 5, p. 5-6)."""

import pytest

from sr830 import tables


def test_time_constant_table():
    assert len(tables.TC_LABELS) == 20
    assert tables.TC_LABELS[0] == "10 us" and tables.TC_LABELS[19] == "30 ks"
    assert tables.TC_LABELS[7] == "30 ms" and tables.TC_LABELS[10] == "1 s"
    assert tables.TC_SECONDS[13] == pytest.approx(30.0)          # the last one allowed > 200 Hz
    assert tables.TC_LABELS[tables.TC_LONG_FIRST_INDEX] == "100 s"


def test_sensitivity_table_voltage_and_current():
    assert len(tables.SENS_LABELS_V) == 27
    assert tables.SENS_LABELS_V[0] == "2 nV" and tables.SENS_LABELS_V[26] == "1 V"
    assert tables.SENS_LABELS_V[17] == "1 mV" and tables.SENS_LABELS_V[20] == "10 mV"
    # "2 nV/fA" ... "1 V/uA": the same index, a million times smaller in amps
    assert tables.SENS_LABELS_A[0] == "2 fA" and tables.SENS_LABELS_A[26] == "1 uA"
    assert tables.sens_full_scale(20, "I1M") == pytest.approx(1e-8)
    assert tables.sens_full_scale(20, "A") == pytest.approx(1e-2)


def test_parsing_labels_and_numbers():
    assert tables.tc_index("30 ms") == 7
    assert tables.tc_index(0.025) == 7              # nearest step on a log scale
    assert tables.tc_index("3 ks") == 17
    assert tables.sens_index("10 mV") == 20
    assert tables.sens_index("10 nA") == 20         # the current twin
    assert tables.sens_index(3e-3) == 19            # snaps UP: 3 mV needs the 5 mV range
    assert tables.sens_index(0.01) == 20            # an exact step stays put
    assert tables.sens_index(99.0) == 26            # above 1 V: the largest range
    with pytest.raises(ValueError):
        tables.tc_index("30 mV")
    with pytest.raises(ValueError):
        tables.sens_index(float("nan"))


def test_enum_choice_and_slope_order():
    assert tables.choice("LOW_NOISE", tables.RESERVES, "reserve") == "low_noise"
    assert tables.choice(1, tables.RESERVES, "reserve") == "normal"
    with pytest.raises(ValueError):
        tables.choice("huge", tables.RESERVES, "reserve")
    assert [tables.slope_order(s) for s in tables.SLOPES] == [1, 2, 3, 4]
    assert tables.unit_for("I100M") == "A" and tables.unit_for("A-B") == "V"
    # FMOD: 1 = internal, 0 = external -- the index IS the GPIB parameter
    assert tables.REF_SOURCES.index("internal") == 1
