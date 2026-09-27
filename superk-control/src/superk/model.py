"""A small, plausible model of what comes out of a SuperK EXTREME + SELECT.

Used by the simulator (to report an estimated power per line) and by the GUI's
spectrum indicator (to draw the white-light envelope). It is a MODEL, not a
calibration: the numbers are datasheet-class, good enough to make the simulated
instrument behave like the real one (more power level -> more light; a line in
the IR is broader than one in the visible; RF amplitude saturates), and to catch
code that would be wrong on the real laser. Pure functions, no Qt, no numpy.

Physics in three lines:
  * The supercontinuum spectral density S(lambda) [mW/nm] of an EXW-12 class
    source spans ~400-2400 nm with a strong peak at the 1064 nm pump and a
    softer visible shoulder. It scales ~linearly with the power level above a
    small threshold (below it the fibre does not broaden).
  * An AOTF diffracts a band whose FWHM grows ~ lambda^2 (the acoustic
    phase-matching condition): a couple of nm in the visible, ~10+ nm at 2 um.
  * The diffraction efficiency vs RF drive power P follows
    eta = eta_max * sin^2( (pi/2) * sqrt(P / P_sat) ): it rises, saturates at
    P_sat, and falls again if you overdrive -- which is why the amplitude is
    clamped and why "more RF" is not always "more light".
"""

from __future__ import annotations

import math

SC_MIN_NM = 400.0
SC_MAX_NM = 2400.0
PUMP_NM = 1064.0


def sc_density_mW_per_nm(wl_nm: float, power_pct: float) -> float:
    """Spectral density of the white light at `wl_nm`, at power level `power_pct`."""
    if wl_nm < SC_MIN_NM or wl_nm > SC_MAX_NM:
        return 0.0
    # a smooth broad envelope (visible edge rises from 400 nm, IR tail decays
    # towards 2400 nm) plus the residual pump peak
    blue_edge = 1.0 / (1.0 + math.exp(-(wl_nm - 470.0) / 25.0))
    ir_tail = 1.0 / (1.0 + math.exp((wl_nm - 2150.0) / 80.0))
    body = 1.2 + 1.6 * math.exp(-((wl_nm - 1150.0) / 450.0) ** 2)
    pump = 6.0 * math.exp(-((wl_nm - PUMP_NM) / 4.0) ** 2)
    shape = blue_edge * ir_tail * body + pump
    # below ~5 % the fibre is not pumped hard enough to broaden
    drive = max(0.0, (power_pct - 5.0) / 95.0)
    return shape * drive


def aotf_fwhm_nm(wl_nm: float) -> float:
    """AOTF passband FWHM, ~ lambda^2 scaling (about 2.5 nm at 550 nm)."""
    return 2.5 * (wl_nm / 550.0) ** 2


def aotf_efficiency(amplitude_pct: float, sat_pct: float = 80.0,
                    eta_max: float = 0.85) -> float:
    """Diffraction efficiency vs RF amplitude (sin^2 of the drive, saturating)."""
    a = max(0.0, amplitude_pct)
    return eta_max * math.sin(0.5 * math.pi * math.sqrt(a / sat_pct)) ** 2


def line_power_mW(wl_nm: float, amplitude_pct: float, power_pct: float,
                  crystal_min_nm: float, crystal_max_nm: float) -> float:
    """Estimated optical power in one AOTF line, 0 outside the crystal's range."""
    if amplitude_pct <= 0 or not (crystal_min_nm <= wl_nm <= crystal_max_nm):
        return 0.0
    return (sc_density_mW_per_nm(wl_nm, power_pct) * aotf_fwhm_nm(wl_nm)
            * aotf_efficiency(amplitude_pct))


def wavelength_rgb(wl_nm: float) -> tuple[int, int, int] | None:
    """The colour the eye sees at `wl_nm` (380-750 nm), None outside the visible.

    The usual piecewise approximation (Bruton), used only to paint visible lines
    in their own colour in the GUI; IR lines are painted in the theme accent.
    """
    w = wl_nm
    if w < 380 or w > 750:
        return None
    if w < 440:
        r, g, b = (440 - w) / 60, 0.0, 1.0
    elif w < 490:
        r, g, b = 0.0, (w - 440) / 50, 1.0
    elif w < 510:
        r, g, b = 0.0, 1.0, (510 - w) / 20
    elif w < 580:
        r, g, b = (w - 510) / 70, 1.0, 0.0
    elif w < 645:
        r, g, b = 1.0, (645 - w) / 65, 0.0
    else:
        r, g, b = 1.0, 0.0, 0.0
    return int(r * 255), int(g * 255), int(b * 255)
