"""The physics of a swept spectrum analyser, and the couplings between its knobs.

Two halves:

1. RESOLVING the settings (used by the brain on both backends): the front-panel
   "auto" couplings -- RBW follows the span, VBW follows RBW, the input
   attenuation follows the reference level, the sweep time follows
   span / (RBW * VBW). `resolve(cfg)` turns the config into ONE frozen
   `SweepSettings` with every auto value replaced by a number.

2. THE SIMULATOR's measurement (`simulate`): what a GSP-818 would display for
   that setting, with a few carriers on the input and -- when the tracking
   generator is on -- a device under test between GEN OUTPUT and RF INPUT.

Why the pieces look the way they do (the physics a spectrum analyser user
knows by heart, written down so a reader can check the code against it):

  * Noise floor. Thermal noise is -174 dBm/Hz; the analyser's own noise figure
    lifts it to the DANL of the data sheet (-130 dBm/Hz above 1 MHz, preamp
    off; -150 dBm/Hz with the 20 dB preamp). What is DISPLAYED is that density
    times the RBW -> DANL + 10 log10(RBW), and every dB of input attenuation
    moves the noise UP by a dB (the signal is attenuated, then gained back).
  * Noise statistics. The detected envelope of noise is Rayleigh, so its power
    is exponentially distributed. The video filter averages about RBW/VBW
    independent samples (VBW << RBW = a smooth floor). A display point covers
    span/(points-1) of spectrum, i.e. several RBW-wide chunks: a positive-peak
    detector shows the MAX of them (the floor sits a few dB higher), negative
    peak the MIN, sample just one, and "normal" alternates the two (the
    "rosenfell" detector the manual describes).
  * Carriers. A CW line is drawn with the RBW filter's shape (close to
    Gaussian on the GSP-818). A peak detector sees a carrier anywhere inside
    the display bin; the sample detector only at the point's exact frequency,
    so a narrow-RBW wide-span sample trace can miss it entirely.
  * Compression. The mixer compresses 1 dB at about +2 dBm (0 dB attenuation);
    the preamp moves that down by its 20 dB. `overload` says so.
  * Tracking generator. A CW that follows the sweep: every point sees it at
    full level whatever the RBW. Its output is not flat (+-3 dB spec) and the
    cables lose more at high frequency -- which is exactly why a THRU
    reference is taken and subtracted (in dB) before reading the DUT.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

#: detector modes, in the module's words (the backend maps them to SCPI)
DETECTORS = ("auto", "normal", "pos_peak", "neg_peak", "sample")

#: devices under test the simulated bench knows
DUTS = ("thru", "bandpass", "lowpass", "open")

#: RBW values the auto coupling chooses from (a 1-3 sequence; the data sheet:
#: 10 Hz ... 500 kHz in steps, 1 MHz, 3 MHz)
RBW_STEPS = (10.0, 30.0, 100.0, 300.0, 1e3, 3e3, 10e3, 30e3, 100e3, 300e3, 1e6, 3e6)

DANL_OFF_LOW, DANL_OFF = -117.0, -130.0   # dBm/Hz, preamp off (below / above 1 MHz)
DANL_ON_LOW, DANL_ON = -140.0, -150.0     # dBm/Hz, preamp on
PREAMP_GAIN_DB = 20.0
P1DB_MIXER_DBM = 2.0                      # 1 dB compression at the mixer, 0 dB attenuation
TG_MIN_HZ = 100e3                         # the tracking generator starts at 100 kHz
MAX_BIN_SAMPLES = 64                      # noise samples a peak detector looks at per point


@dataclass(frozen=True)
class SweepSettings:
    """Everything one sweep is taken with, every auto value resolved. Frozen:
    a sweep's settings are latched when it starts."""

    start_Hz: float
    stop_Hz: float
    points: int
    rbw_Hz: float
    vbw_Hz: float
    ref_level_dBm: float
    atten_dB: float
    sweep_time_s: float
    detector: str            # as set (may be "auto")
    preamp: bool
    tg_on: bool
    tg_level_dBm: float
    rbw_auto: bool = True
    vbw_auto: bool = True
    atten_auto: bool = True
    sweep_time_auto: bool = True

    @property
    def span_Hz(self) -> float:
        return self.stop_Hz - self.start_Hz

    def freqs(self) -> np.ndarray:
        return np.linspace(self.start_Hz, self.stop_Hz, int(self.points))


# ---- the auto couplings ---------------------------------------------------------------

def auto_rbw(span_Hz: float, lo: float = 10.0, hi: float = 3e6) -> float:
    """RBW ~ span/100, rounded DOWN to the 1-3 sequence (the instrument's
    preset: full 1.8 GHz span -> 3 MHz)."""
    target = span_Hz / 100.0
    pick = RBW_STEPS[0]
    for r in RBW_STEPS:
        if r <= target:
            pick = r
    return float(min(max(pick, lo), hi))


def auto_atten(ref_level_dBm: float, atten_max: float = 40.0) -> float:
    """Attenuation that puts the reference level at -10 dBm on the mixer
    (the preset: ref 0 dBm -> 10 dB), in whole dB."""
    return float(min(max(round(ref_level_dBm + 10.0), 0.0), atten_max))


def auto_sweep_time(span_Hz: float, rbw_Hz: float, vbw_Hz: float, points: int,
                    lo: float = 0.01, hi: float = 3000.0) -> float:
    """The shortest sweep that lets the RBW filter settle at each frequency:
    t ~ k * span / (RBW * min(RBW, VBW)), with k ~ 2 for a Gaussian filter.
    Plus a floor from the ADC reading `points` samples. Narrow RBW over a wide
    span is SLOW -- 1 kHz over 1.8 GHz is an hour."""
    k = 2.0
    t = k * span_Hz / (rbw_Hz * min(rbw_Hz, vbw_Hz))
    t = max(t, points * 1e-5, lo)
    return float(min(t, hi))


def effective_detector(detector: str, span_Hz: float) -> str:
    """What "auto" means on the GSP-818 (user manual, Detector): normal when
    the span is above 1 MHz, positive peak at 1 MHz or below."""
    if detector != "auto":
        return detector
    return "normal" if span_Hz > 1e6 else "pos_peak"


def resolve(cfg) -> SweepSettings:
    """cfg (Config) -> the settings the next sweep uses, autos resolved."""
    sw, lim, tg = cfg.sweep, cfg.limits, cfg.tracking
    span = sw.stop_Hz - sw.start_Hz
    rbw = auto_rbw(span, lim.rbw_min_Hz, lim.rbw_max_Hz) if sw.rbw_auto else float(sw.rbw_Hz)
    vbw = min(max(rbw, lim.vbw_min_Hz), lim.vbw_max_Hz) if sw.vbw_auto else float(sw.vbw_Hz)
    att = auto_atten(sw.ref_level_dBm, lim.atten_max_dB) if sw.atten_auto else float(sw.atten_dB)
    t = (auto_sweep_time(span, rbw, vbw, int(sw.points), lim.sweep_time_min_s,
                         lim.sweep_time_max_s)
         if sw.sweep_time_auto else float(sw.sweep_time_s))
    return SweepSettings(
        start_Hz=float(sw.start_Hz), stop_Hz=float(sw.stop_Hz), points=int(sw.points),
        rbw_Hz=rbw, vbw_Hz=vbw, ref_level_dBm=float(sw.ref_level_dBm), atten_dB=att,
        sweep_time_s=t, detector=str(sw.detector), preamp=bool(sw.preamp),
        tg_on=bool(tg.tg_on), tg_level_dBm=float(tg.level_dBm),
        rbw_auto=bool(sw.rbw_auto), vbw_auto=bool(sw.vbw_auto),
        atten_auto=bool(sw.atten_auto), sweep_time_auto=bool(sw.sweep_time_auto))


# ---- reading a trace --------------------------------------------------------------------

def find_peak(freqs_Hz: np.ndarray, dBm: np.ndarray) -> tuple[float, float]:
    """(frequency, level) of the highest point -- the "peak search" marker."""
    y = np.asarray(dBm, dtype=float)
    if y.size == 0 or not np.isfinite(y).any():
        return math.nan, math.nan
    i = int(np.nanargmax(y))
    return float(freqs_Hz[i]), float(y[i])


def noise_floor(dBm: np.ndarray) -> float:
    """The median of the trace: with a few carriers on a wide span, most
    points are noise, and the median ignores the carriers."""
    y = np.asarray(dBm, dtype=float)
    y = y[np.isfinite(y)]
    return float(np.median(y)) if y.size else math.nan


def power_average_dBm(traces_dBm) -> np.ndarray:
    """Average traces in LINEAR power (mW), return dBm. Averaging the dB
    numbers instead would bias noise low by 2.5 dB (the log of an exponential
    variable); averaging power keeps a noise floor where the noise really is."""
    lin = np.mean([10.0 ** (np.asarray(t, dtype=float) / 10.0) for t in traces_dBm], axis=0)
    with np.errstate(divide="ignore"):
        return 10.0 * np.log10(lin)


# ---- the simulated bench ----------------------------------------------------------------

def parse_carriers(text: str) -> list[tuple[float, float]]:
    """"100e6:-20, 433.92e6:-45" -> [(1e8, -20.0), (4.3392e8, -45.0)].
    Bad entries raise ValueError (the brain refuses them)."""
    out = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        f, _, p = part.partition(":")
        fv, pv = float(f), float(p)
        if not (math.isfinite(fv) and math.isfinite(pv)) or fv <= 0:
            raise ValueError(f"bad carrier {part!r}: want freq_Hz:level_dBm")
        out.append((fv, pv))
    return out


def dut_gain(freqs_Hz: np.ndarray, bench) -> np.ndarray:
    """|S21|^2 (linear power) of the simulated device under test, cables included."""
    f = np.maximum(np.asarray(freqs_Hz, dtype=float), 1.0)
    iso = 10.0 ** (-float(bench.dut_isolation_dB) / 10.0)
    n = max(1, int(bench.dut_order))
    loss = 10.0 ** (-float(bench.dut_loss_dB) / 10.0)
    fc = max(float(bench.dut_center_Hz), 1.0)
    if bench.dut == "thru":
        g = np.ones_like(f)
    elif bench.dut == "open":
        g = np.full_like(f, iso)
    elif bench.dut == "lowpass":
        g = loss / (1.0 + (f / fc) ** (2 * n)) + iso
    else:                                    # bandpass: the lowpass prototype, mapped
        bw = max(float(bench.dut_bw_Hz), 1.0)
        x = (f / fc - fc / f) * fc / bw
        g = loss / (1.0 + x ** (2 * n)) + iso
    cable = 10.0 ** (-float(bench.cable_loss_dB_at_1GHz) * np.sqrt(f / 1e9) / 10.0)
    return g * cable


def tg_flatness_dB(freqs_Hz: np.ndarray, ripple_dB: float) -> np.ndarray:
    """The tracking generator's output error vs frequency: deterministic, so a
    thru reference cancels it (that is the whole point of normalising)."""
    f = np.asarray(freqs_Hz, dtype=float)
    return ripple_dB * (0.7 * np.sin(2 * np.pi * f / 350e6 + 0.7)
                        + 0.3 * np.sin(2 * np.pi * f / 97e6))


def simulate(s: SweepSettings, bench, rng: np.random.Generator) -> tuple[np.ndarray, dict]:
    """One simulated sweep: the displayed trace in dBm, and {"overload": bool}."""
    f = s.freqs()
    npts = f.size
    span = s.span_Hz
    bin_w = span / (npts - 1) if npts > 1 else 0.0
    det = effective_detector(s.detector, span)

    # -- noise ---------------------------------------------------------------------
    if s.preamp:
        danl = np.where(f < 1e6, DANL_ON_LOW, DANL_ON)
    else:
        danl = np.where(f < 1e6, DANL_OFF_LOW, DANL_OFF)
    n_mW = 10.0 ** ((danl + 10.0 * math.log10(s.rbw_Hz) + s.atten_dB) / 10.0)
    k_video = int(min(max(round(s.rbw_Hz / max(s.vbw_Hz, 1e-9)), 1), 100))
    m = 1 if det == "sample" else int(min(max(round(bin_w / s.rbw_Hz), 1), MAX_BIN_SAMPLES))
    # mean of k exponentials = gamma(k, 1/k): the video filter's smoothing
    draws = rng.gamma(k_video, 1.0 / k_video, size=(npts, m))
    if det == "pos_peak":
        g = draws.max(axis=1)
    elif det == "neg_peak":
        g = draws.min(axis=1)
    elif det == "normal":
        g = np.where(np.arange(npts) % 2 == 0, draws.max(axis=1), draws.min(axis=1))
    else:
        g = draws[:, 0]
    p_mW = n_mW * g

    # -- carriers on the input -------------------------------------------------------
    peak_input = -np.inf
    for fc, pc in parse_carriers(bench.carriers):
        d = np.abs(f - fc)
        if det != "sample":
            d = np.maximum(d - bin_w / 2.0, 0.0)     # anywhere in the bin counts
        shape = np.exp(-4.0 * math.log(2.0) * (d / s.rbw_Hz) ** 2)
        p_mW = p_mW + 10.0 ** (pc / 10.0) * shape
        if f[0] - s.rbw_Hz <= fc <= f[-1] + s.rbw_Hz:
            peak_input = max(peak_input, pc)

    # -- the tracking generator through the DUT ------------------------------------------
    if s.tg_on:
        tg_dBm = s.tg_level_dBm + tg_flatness_dB(f, bench.tg_ripple_dB)
        tg_mW = np.where(f >= TG_MIN_HZ, 10.0 ** (tg_dBm / 10.0) * dut_gain(f, bench), 0.0)
        p_mW = p_mW + tg_mW
        peak_input = max(peak_input, s.tg_level_dBm + bench.tg_ripple_dB)

    trace = 10.0 * np.log10(p_mW)

    # -- compression: the mixer sees input - attenuation (+ preamp gain) ----------------
    gain = PREAMP_GAIN_DB if s.preamp else 0.0
    mixer = trace - s.atten_dB + gain
    over = mixer - P1DB_MIXER_DBM
    trace = np.where(over > 0, trace - over * 0.9, trace)   # the display stops growing
    overload = bool(np.isfinite(peak_input)
                    and peak_input - s.atten_dB + gain > P1DB_MIXER_DBM)
    return trace, {"overload": overload}
