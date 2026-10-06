"""The waveform arithmetic: shapes, peak voltage, load factor, phase wrap."""

import pytest

from afg import waveforms as W


def test_shapes():
    assert W.unit_shape("sine", 0.25) == pytest.approx(1.0)
    assert W.unit_shape("square", 0.1) == 1.0 and W.unit_shape("square", 0.6) == -1.0
    assert W.unit_shape("pulse", 0.2, duty_pct=25) == 1.0
    assert W.unit_shape("pulse", 0.3, duty_pct=25) == -1.0
    # triangle: -1 at 0, +1 at the middle
    assert W.unit_shape("ramp", 0.0) == -1.0 and W.unit_shape("ramp", 0.5) == pytest.approx(1.0)
    # rising saw at 100 % symmetry
    assert W.unit_shape("ramp", 0.75, symmetry_pct=100) == pytest.approx(0.5)
    assert -1.0 <= W.unit_shape("noise", 0.3) <= 1.0
    assert W.unit_shape("dc", 0.3) == 0.0


def test_value_with_offset_phase_and_output():
    s = {"output": True, "waveform": "sine", "frequency_Hz": 10.0, "amplitude_Vpp": 2.0,
         "offset_V": 0.5, "phase_deg": 90.0}
    assert W.value(s, 0.0) == pytest.approx(1.5)          # sin(90 deg) = 1 -> 0.5 + 1
    assert W.value(dict(s, waveform="dc"), 0.123) == 0.5
    assert W.value(dict(s, output=False), 0.0) == 0.0


def test_peak():
    assert W.peak("sine", 4.0, -1.0) == 3.0
    assert W.peak("dc", 4.0, -1.0) == 1.0                 # DC has no amplitude


def test_load_factor():
    assert W.load_factor(50.0) == pytest.approx(1.0)
    assert W.load_factor(None) == 2.0                      # high-Z doubles
    assert W.load_factor(150.0) == pytest.approx(1.5)


def test_wrap_phase():
    assert W.wrap_phase(190.0) == pytest.approx(-170.0)
    assert W.wrap_phase(-180.0) == pytest.approx(180.0)
    assert W.wrap_phase(720.0 + 45.0) == pytest.approx(45.0)
