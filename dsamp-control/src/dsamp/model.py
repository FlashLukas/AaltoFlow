"""What actually comes out of the amplifier: an ESTIMATE from the datasheet.

The amplifier has no frequency input -- its gain setting is a number, and the
real gain at a given frequency is that number minus the band's roll-off.
DS Instruments publishes typical small-signal gain at the MAXIMUM setting
(GB6000 datasheet, "Technical specifications"): 31 dB at 1 GHz falling to 20 dB
at 6 GHz. We treat the setting as the gain at the flat low end and subtract
the droop from that table:

    gain(setting, f) = setting - droop(f),   droop(f) = 31 dB - G_typ(f)

This is NOT a calibration. It is the vendor's typical curve, good to a few dB,
and useful for one thing: warning you BEFORE the output approaches compression
or a level your downstream parts cannot take. Measure your own unit (a VNA
S21 at a few settings) if you need the real number.

Compression uses the smooth textbook form

    P_out = P_lin - 10 log10(1 + 10^((P_lin - P_sat)/10))

with P_sat chosen so that the output is 1 dB compressed exactly at the P1dB
from the config. 10 log10(1 + x) = 1 dB when x = 10^0.1 - 1, i.e.
P_lin - P_sat = 10 log10(10^0.1 - 1) = -5.87 dB, and at that point P_lin =
P1dB + 1 dB, so P_sat = P1dB + 6.87 dB.

Pure functions, no Qt, no hardware: the brain, the simulator and the GUI's
roll-off plot all use the same numbers.
"""

from __future__ import annotations

import math

# Typical small-signal gain at the MAXIMUM setting, GB6000 datasheet (Rev 3 v1.2).
# The 0.05 GHz point is the band edge; the datasheet curve is flat to ~1 GHz.
_F_GHZ = (0.01, 0.05, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
_G_TYP = (31.0, 31.0, 31.0, 30.0, 28.0, 25.0, 24.0, 20.0)
_G_REF = 31.0

#: P_sat - P1dB for the soft-compression curve (see the module docstring).
_SAT_ABOVE_P1DB = 1.0 - 10.0 * math.log10(10 ** 0.1 - 1.0)   # = 6.87 dB


def droop_dB(frequency_Hz: float) -> float:
    """Gain lost to the band's roll-off at this frequency (>= 0), linear interpolation."""
    f = frequency_Hz / 1e9
    if f <= _F_GHZ[0]:
        return _G_REF - _G_TYP[0]
    if f >= _F_GHZ[-1]:
        # beyond 6 GHz the amp is out of band; keep falling at the last slope
        slope = (_G_TYP[-1] - _G_TYP[-2]) / (_F_GHZ[-1] - _F_GHZ[-2])
        return _G_REF - (_G_TYP[-1] + slope * (f - _F_GHZ[-1]))
    for i in range(len(_F_GHZ) - 1):
        f0, f1 = _F_GHZ[i], _F_GHZ[i + 1]
        if f0 <= f <= f1:
            g = _G_TYP[i] + (_G_TYP[i + 1] - _G_TYP[i]) * (f - f0) / (f1 - f0)
            return _G_REF - g
    return 0.0   # unreachable


def est_gain_dB(setting_dB: float, frequency_Hz: float) -> float:
    """Estimated small-signal gain at `frequency_Hz` for a gain SETTING."""
    return setting_dB - droop_dB(frequency_Hz)


def compressed_output_dBm(linear_out_dBm: float, p1db_dBm: float) -> float:
    """Output after soft compression, for a would-be linear output level."""
    p_sat = p1db_dBm + _SAT_ABOVE_P1DB
    return linear_out_dBm - 10.0 * math.log10(1.0 + 10 ** ((linear_out_dBm - p_sat) / 10.0))


def estimate(setting_dB: float, frequency_Hz: float, input_dBm: float,
             p1db_dBm: float) -> tuple[float, float, float]:
    """(estimated gain dB, estimated output dBm, compression dB) for an operating point."""
    g = est_gain_dB(setting_dB, frequency_Hz)
    lin = input_dBm + g
    out = compressed_output_dBm(lin, p1db_dBm)
    return g, out, lin - out
