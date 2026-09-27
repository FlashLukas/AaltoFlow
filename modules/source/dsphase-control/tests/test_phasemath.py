"""The phase arithmetic: rounding to the step, wrapping, and unwrapping a
readback into the caller's branch."""

import pytest

from dsphase.phasemath import (quantize, wrap, unwrap_near, decimals_for,
                               datasheet_accuracy_deg)


@pytest.mark.parametrize("x, step, want", [
    (33.3, 0.5, 33.5), (33.2, 0.5, 33.0), (33.25, 0.5, 33.5), (-33.25, 0.5, -33.0),
    (10.0, 5.625, 11.25), (0.1, 0.0, 0.1), (359.9, 0.5, 360.0),
])
def test_quantize(x, step, want):
    assert quantize(x, step) == pytest.approx(want)


@pytest.mark.parametrize("x, want", [
    (0, 0), (180, 180), (-180, 180), (190, -170), (270, -90), (-270, 90),
    (360, 0), (540, 180), (-90, -90),
])
def test_wrap_into_device_range(x, want):
    assert wrap(x) == pytest.approx(want)


@pytest.mark.parametrize("readback, ref, want", [
    (-90, 270, 270), (90, -270, -270), (0, 360, 360), (10, 10, 10),
    (180, -180, -180), (-89.5, 270, 270.5),
])
def test_unwrap_near(readback, ref, want):
    assert unwrap_near(readback, ref) == pytest.approx(want)


def test_decimals_for_steps():
    assert decimals_for(0.5) == 1
    assert decimals_for(0.25) == 2
    assert decimals_for(5.625) == 3
    assert decimals_for(1.0) == 0


def test_datasheet_bands():
    assert datasheet_accuracy_deg(2400, 45) == 2.0
    assert datasheet_accuracy_deg(2400, 170) == 3.0
    assert datasheet_accuracy_deg(5800, 45) == 4.0
    assert datasheet_accuracy_deg(5800, -170) == 8.0
    assert datasheet_accuracy_deg(2400, 270) == 2.0      # 270 = -90: within +-90
