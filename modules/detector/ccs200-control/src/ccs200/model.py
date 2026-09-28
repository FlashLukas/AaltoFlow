"""The simulator's physics: what a CCS200 would read with a lamp and a few
emission lines on its fibre. Pure numpy, no instrument, no Qt -- testable.

Four ingredients, each a known property of a small CCD spectrometer:

  * the WAVELENGTH GRID. 3648 pixels (Toshiba TCD1304 linear CCD) over
    200-1000 nm. A grating spectrometer's dispersion is not constant across the
    detector, so pixel -> nm is a smooth polynomial, not a linspace; the real
    instrument stores one per unit (its factory calibration).
  * the RESPONSE. A silicon CCD behind a grating sees little below ~300 nm and
    fades again in the near IR; one smooth hump stands in for both.
  * the LIGHT. A tungsten-like blackbody (the lamp) plus Gaussian emission
    lines of Hg and Ar at their tabulated wavelengths, all broadened to the
    instrument resolution. Signal = rate x integration time.
  * the DARK. An electronic offset (the same at any integration time) plus a
    dark current that grows LINEARLY with integration time and has a fixed
    per-pixel pattern. That pattern is why a dark spectrum only fits the
    integration time it was taken at.

Everything is in FULL-SCALE units: 1.0 is a saturated pixel, and a reading is
clipped there, as the ADC does.
"""

from __future__ import annotations

import numpy as np

N_PIXELS = 3648

#: (wavelength nm, relative strength) -- Hg and Ar lines of a pen-lamp style
#: calibration source, strengths chosen to look like one, not measured.
EMISSION_LINES = (
    (404.66, 0.30),   # Hg
    (435.83, 0.75),   # Hg
    (546.07, 1.00),   # Hg, the brightest: the default peak
    (576.96, 0.28),   # Hg (yellow doublet)
    (579.07, 0.30),   # Hg
    (696.54, 0.22),   # Ar
    (763.51, 0.45),   # Ar
    (811.53, 0.50),   # Ar
    (912.30, 0.18),   # Ar
)

_H, _C, _KB = 6.62607015e-34, 2.99792458e8, 1.380649e-23


def pixel_wavelengths(n: int = N_PIXELS) -> np.ndarray:
    """nm of every pixel: 200 -> 1000 nm, slightly nonlinear (the dispersion of
    a grating changes across the detector)."""
    x = np.linspace(0.0, 1.0, n)
    a, c = 199.8, -18.0                 # start, curvature
    b = 1000.2 - a - c                  # so that x = 1 lands on ~1000 nm
    return a + b * x + c * x * x


def response(wl_nm: np.ndarray) -> np.ndarray:
    """Relative sensitivity (0..1) of a Si CCD behind a grating: a broad hump
    around 600 nm, rolled off in the UV."""
    wl = np.asarray(wl_nm, dtype=float)
    hump = np.exp(-((wl - 600.0) / 330.0) ** 2)
    uv = 1.0 / (1.0 + np.exp(-(wl - 290.0) / 18.0))
    return hump * uv


def blackbody(wl_nm: np.ndarray, temperature_K: float) -> np.ndarray:
    """Planck spectral radiance vs wavelength, normalised to its maximum on the
    given grid (only the SHAPE matters here)."""
    wl = np.asarray(wl_nm, dtype=float) * 1e-9
    t = max(float(temperature_K), 100.0)
    with np.errstate(over="ignore"):
        b = 1.0 / (wl ** 5 * np.expm1(_H * _C / (wl * _KB * t)))
    m = float(np.max(b)) if b.size else 1.0
    return b / m if m > 0 else b


def emission(wl_nm: np.ndarray, fwhm_nm: float) -> np.ndarray:
    """The emission lines as Gaussians of the instrument's resolution, the
    strongest at 1.0."""
    wl = np.asarray(wl_nm, dtype=float)
    sigma = max(float(fwhm_nm), 0.05) / 2.3548
    out = np.zeros_like(wl)
    for centre, strength in EMISSION_LINES:
        out += strength * np.exp(-0.5 * ((wl - centre) / sigma) ** 2)
    return out


def dark_pattern(n: int = N_PIXELS, seed: int = 20260927) -> np.ndarray:
    """Per-pixel dark-current multiplier (mean 1, ~30 % spread). FIXED for a
    given detector -- the same every scan -- so it is seeded, not random."""
    rng = np.random.default_rng(seed)
    return np.clip(1.0 + 0.3 * rng.standard_normal(n), 0.2, None)


def light(wl_nm: np.ndarray, sim, integration_s: float) -> np.ndarray:
    """Signal from the light on the fibre (full-scale units), no dark, no noise."""
    if not sim.light_on:
        return np.zeros_like(np.asarray(wl_nm, dtype=float))
    r = response(wl_nm)
    lamp = sim.lamp_level_per_s * blackbody(wl_nm, sim.lamp_temperature_K)
    lines = sim.line_level_per_s * emission(wl_nm, sim.line_fwhm_nm)
    # Lines are specified at the detector (their level is what the brightest one
    # reads), the lamp through the response, which is what shapes its continuum.
    return integration_s * (lamp * r / max(float(np.max(r)), 1e-12) + lines)


def dark(sim, integration_s: float, pattern: np.ndarray) -> np.ndarray:
    """Offset + dark current x integration time, per pixel (no noise)."""
    return sim.offset + sim.dark_rate_per_s * integration_s * pattern


def scan(wl_nm: np.ndarray, sim, integration_s: float, pattern: np.ndarray,
         rng: np.random.Generator) -> np.ndarray:
    """One simulated scan: light + dark + noise, clipped to [0, 1] full scale.

    Noise: read noise (constant) plus shot noise, whose variance grows with the
    signal (~1e-4 full scale per unit signal: ~10^4 electrons at full scale)."""
    clean = light(wl_nm, sim, integration_s) + dark(sim, integration_s, pattern)
    sigma = np.sqrt(sim.read_noise ** 2 + 1e-4 * np.clip(clean, 0.0, None))
    noisy = clean + sigma * rng.standard_normal(clean.shape)
    return np.clip(noisy, 0.0, 1.0)


def scan_duration_s(integration_s: float) -> float:
    """How long one scan takes: the exposure plus the CCD read-out and USB
    transfer (a few ms for 3648 pixels)."""
    return float(integration_s) + 0.004
