"""Low-pass filter arithmetic for a lock-in demodulator. Pure maths, no hardware.

A Zurich demodulator's filter of order n is n identical first-order RC stages in
a row, all with time constant tau. Two numbers follow from that and both matter
in the lab:

SETTLING TIME. Feed the filter a step. One stage reaches 1 - exp(-t/tau). The
cascade's step response is the CDF of a gamma (Erlang) distribution with shape n
and scale tau:

    fraction(t) = 1 - exp(-x) * sum_{k=0}^{n-1} x^k / k!,     x = t / tau

So "how long until 99 % settled" is the inverse of that. It grows with the order:
4.6 tau for order 1, 10.0 tau for order 4, 16.0 tau for order 8. These are the
numbers in Zurich's own settling-time table; computing them rather than copying
the table means any order and any percentage work.

NOISE BANDWIDTH. White noise of density e_n [V/sqrt(Hz)] at the input becomes an
output noise of e_n * sqrt(ENBW), where

    ENBW = integral_0^inf |H(f)|^2 df = (1 / (2 pi tau)) * sqrt(pi) G(n-1/2) / (2 G(n))

(1/(4 tau) for order 1). The simulator uses it, so a longer time constant gives
visibly less noise, as on the real instrument.
"""

from __future__ import annotations

import math


def step_response(x: float, order: int) -> float:
    """Fraction of a step reached after x = t/tau, for a filter of `order`."""
    if x <= 0:
        return 0.0
    term = 1.0
    total = 1.0
    for k in range(1, order):
        term *= x / k
        total += term
    return 1.0 - math.exp(-x) * total


def settle_tc(order: int, percent: float = 99.0) -> float:
    """Time, in units of the time constant, to settle to `percent` of a step.

    Solved by bisection. step_response rises monotonically from 0 to 1, so
    bisection cannot miss, and 60 halvings are far beyond float precision.
    """
    order = max(1, int(order))
    target = min(max(percent, 1e-6), 100.0 - 1e-9) / 100.0
    lo, hi = 0.0, 1.0
    while step_response(hi, order) < target:     # bracket the root first
        hi *= 2.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if step_response(mid, order) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def settle_time_s(tc_s: float, order: int, percent: float = 99.0) -> float:
    """Settling time in seconds for time constant `tc_s`."""
    return settle_tc(order, percent) * float(tc_s)


def enbw_Hz(tc_s: float, order: int) -> float:
    """Equivalent noise bandwidth of the filter, in Hz."""
    n = max(1, int(order))
    shape = math.sqrt(math.pi) * math.gamma(n - 0.5) / (2.0 * math.gamma(n))
    return shape / (2.0 * math.pi * float(tc_s))


def bandwidth_3dB_Hz(tc_s: float, order: int) -> float:
    """-3 dB bandwidth of the filter, in Hz."""
    n = max(1, int(order))
    return math.sqrt(2.0 ** (1.0 / n) - 1.0) / (2.0 * math.pi * float(tc_s))


def transfer(df_Hz: float, tc_s: float, order: int) -> complex:
    """Complex response H at a frequency offset df from the demodulation
    frequency: 1 / (1 + i 2 pi df tau)^n."""
    return 1.0 / (1.0 + 2j * math.pi * df_Hz * tc_s) ** max(1, int(order))
