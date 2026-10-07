"""Fine power: the vernier fills the 0.5 dB gaps of the step attenuator.

Lukas (2026-10-07): "cant we just hide this in the backend and just deliver
power what was asked?" -- yes. The attenuator only makes 0.5 dB steps; the
VERNIER trims the level in raw counts. So a request like -13.73 dBm becomes

    attenuator -13.5 dBm  +  vernier round((-13.73 - -13.5) / slope) counts

and the user never sees the vernier. The remainder is at most half a step
(0.25 dB), i.e. about +-6 counts, where the vernier is close to linear.

THE SLOPE (dB per count) was MEASURED on the lab's SG12000L (firmware V7.84,
2026-10-07, 30 dB pad into a spectrum analyser), a linear fit over -30..+30
counts at -10 dBm. It depends on frequency (table below, interpolated
linearly) and somewhat on power (~0.059 at -20 dBm against 0.044 at -10 and
0 dBm, at 2 GHz) -- the power dependence is NOT modelled. Over a 0.25 dB
remainder that costs at most ~0.06 dB, plus ~0.1 dB around 6 GHz, where the
vernier steps irregularly. Far better than the 0.5 dB steps without it, but
not a substitute for measuring the level when it matters.
"""

from __future__ import annotations

#: (frequency in Hz, dB per count) measured at -10 dBm, slope over -30..+30
SLOPE_TABLE = ((1.0e9, 0.0480), (2.0e9, 0.0441), (4.0e9, 0.0450),
               (6.0e9, 0.0728), (10.0e9, 0.0600))

#: the remainder is never more than half an attenuator step; this caps the
#: counts so a wrong slope can never push the vernier into its non-linear part
MAX_FILL_COUNTS = 15


def slope_dB_per_count(frequency_Hz: float) -> float:
    """dB per vernier count at this frequency (linear between the measured
    points, the end values outside them)."""
    pts = SLOPE_TABLE
    f = float(frequency_Hz)
    if f <= pts[0][0]:
        return pts[0][1]
    for (f0, s0), (f1, s1) in zip(pts, pts[1:]):
        if f <= f1:
            return s0 + (s1 - s0) * (f - f0) / (f1 - f0)
    return pts[-1][1]


def counts_for(remainder_dB: float, frequency_Hz: float) -> int:
    """Vernier counts that add `remainder_dB` at this frequency."""
    n = int(round(float(remainder_dB) / slope_dB_per_count(frequency_Hz)))
    return max(-MAX_FILL_COUNTS, min(MAX_FILL_COUNTS, n))


def dB_for(counts: int, frequency_Hz: float) -> float:
    """The level change `counts` vernier counts make (small counts only)."""
    return int(counts) * slope_dB_per_count(frequency_Hz)


def split(power_dBm: float, step_dB: float, frequency_Hz: float,
          lo: float, hi: float) -> tuple[float, int]:
    """(attenuator setting, vernier counts) for `power_dBm`.

    The attenuator goes to the NEAREST step (so the vernier works on at most
    half a step), kept inside [lo, hi], the safety limits."""
    if step_dB <= 0:
        return float(power_dBm), 0
    att = round(round(power_dBm / step_dB) * step_dB, 6)
    if att < lo - 1e-9:
        att += step_dB
    elif att > hi + 1e-9:
        att -= step_dB
    return att, counts_for(power_dBm - att, frequency_Hz)
