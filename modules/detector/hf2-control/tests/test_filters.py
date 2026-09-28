"""Filter arithmetic against numbers known independently.

The settling times are Zurich Instruments' published table (time in units of
tau to reach 90 / 99 / 99.9 % of a step). They are computed here from the gamma
distribution, not copied, so matching the table to the printed precision is a
real check of the formula.
"""

import math

import pytest

from hf2 import filters


@pytest.mark.parametrize("order, p90, p99, p999", [
    (1, 2.30, 4.61, 6.91),
    (2, 3.89, 6.64, 9.23),
    (3, 5.32, 8.41, 11.23),
    (4, 6.68, 10.05, 13.06),
    (8, 11.77, 16.00, 19.62),
])
def test_settling_table(order, p90, p99, p999):
    assert filters.settle_tc(order, 90) == pytest.approx(p90, abs=0.01)
    assert filters.settle_tc(order, 99) == pytest.approx(p99, abs=0.01)
    assert filters.settle_tc(order, 99.9) == pytest.approx(p999, abs=0.01)


def test_first_order_is_exactly_ln():
    # one RC stage: 1 - exp(-x) = p  ->  x = -ln(1 - p)
    assert filters.settle_tc(1, 99) == pytest.approx(math.log(100), rel=1e-9)


def test_settle_time_scales_with_tau():
    assert filters.settle_time_s(0.02, 4) == pytest.approx(2 * filters.settle_time_s(0.01, 4))


def test_enbw_first_order_is_one_over_four_tau():
    assert filters.enbw_Hz(0.01, 1) == pytest.approx(25.0)


def test_enbw_shrinks_with_order_at_fixed_tau():
    widths = [filters.enbw_Hz(0.01, n) for n in range(1, 9)]
    assert all(a > b for a, b in zip(widths, widths[1:]))


def test_3dB_bandwidth_first_order():
    assert filters.bandwidth_3dB_Hz(1.0, 1) == pytest.approx(1 / (2 * math.pi))


def test_transfer_is_unity_on_resonance_and_half_power_at_3dB():
    assert filters.transfer(0.0, 0.01, 4) == pytest.approx(1.0)
    f3 = filters.bandwidth_3dB_Hz(0.01, 4)
    assert abs(filters.transfer(f3, 0.01, 4)) ** 2 == pytest.approx(0.5, rel=1e-9)
