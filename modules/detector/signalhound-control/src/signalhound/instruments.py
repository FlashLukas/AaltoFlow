"""What the hardware can do: model ranges, allowed RBWs, the bin grid, and
the settings of one sweep. Pure data and arithmetic -- no hardware, no Qt --
so the brain, both backends, describe and the GUI share ONE copy.

Numbers from the Signal Hound SA API header (sa_api.h: SA44_MIN_FREQ,
SA124_MIN_FREQ, SA_MAX_RBW ...) and the product data sheets. The header
allows the SA124 up to 13 GHz; the data sheet specifies 12.4 GHz, and that is
what we clamp to.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

#: model -> (lowest frequency, highest frequency, widest RBW), in Hz.
#: The first-generation SA44 / SA124A share their successor's ranges.
MODELS = {
    "SA44B": (1.0, 4.4e9, 250e3),
    "SA124B": (100e3, 12.4e9, 6e6),
}
MODEL_ALIASES = {"SA44": "SA44B", "SA124A": "SA124B"}

#: the USB-TG44A tracking generator: frequency range and output level range,
#: from its data sheet. The TG verbs (tg_cw, tg_sweep_acquire) REFUSE values
#: outside them. # VERIFY both on the TG44A (the CW level with a power meter).
TG_RANGE_HZ = (10.0, 4.4e9)
TG_LEVEL_DBM = (-30.0, -10.0)

#: RBW is continuous up to this; above it only these fixed values exist
#: (sa_api docs: 0.1 Hz - 100 kHz, 250 kHz, and 6 MHz on the SA124).
RBW_CONTINUOUS_MAX_HZ = 100e3
RBW_FIXED_HZ = (250e3, 6e6)

DETECTORS = ("average", "peak")


def model_range(model: str) -> tuple[float, float, float]:
    """(f_min, f_max, rbw_max) of a model name; unknown -> the SA44B's."""
    return MODELS.get(MODEL_ALIASES.get(model, model), MODELS["SA44B"])


def snap_rbw(rbw_Hz: float, rbw_max_Hz: float) -> float:
    """The nearest RBW the analyser can do.

    Up to 100 kHz any value works. Above, only 250 kHz (and 6 MHz on the
    SA124) exist, so the request snaps to the nearest of those in LOG terms
    (150 kHz is closer to 100k than to 250k by ratio). Never above the model's
    widest RBW.
    """
    allowed = [RBW_CONTINUOUS_MAX_HZ] + [r for r in RBW_FIXED_HZ if r <= rbw_max_Hz]
    if rbw_Hz <= RBW_CONTINUOUS_MAX_HZ:
        return float(rbw_Hz)
    return float(min(allowed, key=lambda r: abs(math.log(r / rbw_Hz))))


@dataclass(frozen=True)
class Grid:
    """The frequency bins of a sweep: bin i sits at start_Hz + i * bin_Hz.
    This is what the API reports (saQuerySweepInfo), not what we asked for."""

    start_Hz: float
    bin_Hz: float
    points: int

    def freqs(self) -> np.ndarray:
        return self.start_Hz + self.bin_Hz * np.arange(int(self.points))

    @property
    def stop_Hz(self) -> float:
        return self.start_Hz + self.bin_Hz * (int(self.points) - 1)


@dataclass(frozen=True)
class SweepSettings:
    """Everything that makes one sweep what it is. Frozen, so the brain can
    compare "what is configured" with "what is asked for" with a plain ==.

    Two kinds of sweep use it (since 2026-09-28, when the tracking-generator
    MEASUREMENT moved to the shsna module):
      * spectrum sweeps -- `from_config`: the settings in cfg.sweep, TG off.
      * TG sweeps       -- `for_tg_sweep`: what a client module (shsna) asked
                           for; this module runs them as the TG's owner.
    The tg_* fields stay in this one class because the backends' `configure`
    reads them to decide which mode to initiate."""

    center_Hz: float
    span_Hz: float
    ref_level_dBm: float
    rbw_Hz: float
    vbw_Hz: float
    reject: bool
    detector: str
    tg_on: bool
    tg_level_dBm: float
    tg_points: int
    tg_high_dynamic_range: bool
    tg_passive_device: bool
    atten: int
    gain: int
    preamp: bool

    @classmethod
    def from_config(cls, cfg) -> "SweepSettings":
        """The spectrum sweep the config asks for. The TG fields are fixed
        placeholders (not NaN: NaN != NaN would make two identical settings
        compare unequal and the analyser would be reconfigured every sweep)."""
        s, h = cfg.sweep, cfg.hardware
        return cls(float(s.center_Hz), float(s.span_Hz), float(s.ref_level_dBm),
                   float(s.rbw_Hz), float(s.vbw_Hz), bool(s.reject), str(s.detector),
                   False, TG_LEVEL_DBM[1], 0, True, True,
                   int(h.atten), int(h.gain), bool(h.preamp))

    @classmethod
    def for_tg_sweep(cls, cfg, start_Hz: float, stop_Hz: float, level_dBm: float | None,
                     rbw_Hz: float, points: int) -> "SweepSettings":
        """A tracking-generator sweep from start to stop.

        MEASURED on the TG44A (2026-09-28): a TG sweep IGNORES the level set
        with saSetTg (-30 and -20 dBm gave identical traces) and returns the
        transmission in dB relative to the TG's calibrated output, not dBm.
        So `level_dBm` is optional; None puts the TG44A's maximum in the (unused)
        field. The reference level is 10 dB above that level (30 dB for an
        amplifying device); # VERIFY whether TG sweep mode uses it at all."""
        h, lim = cfg.hardware, cfg.limits
        lvl = TG_LEVEL_DBM[1] if level_dBm is None else float(level_dBm)
        head = 10.0 if h.tg_passive_device else 30.0
        ref = min(lim.ref_max_dBm, lvl + head)
        vbw = min(float(cfg.sweep.vbw_Hz), float(rbw_Hz))
        return cls((start_Hz + stop_Hz) / 2, stop_Hz - start_Hz, ref, float(rbw_Hz), vbw,
                   bool(cfg.sweep.reject), "average", True, lvl, int(points),
                   bool(h.tg_high_dynamic_range), bool(h.tg_passive_device),
                   int(h.atten), int(h.gain), bool(h.preamp))


def sim_grid(s: SweepSettings, max_bins: int) -> Grid:
    """The grid the SIMULATED analyser uses.

    Spectrum mode: bins of RBW/2 (two bins per resolution bandwidth, so a tone
    is never lost between bins), coarser only past `max_bins`. TG sweep:
    the requested point count, exactly. The real analyser chooses its own --
    the brain always uses whatever the backend reports.
    """
    start = s.center_Hz - s.span_Hz / 2
    if s.tg_on:
        n = max(2, int(s.tg_points))
        return Grid(start, s.span_Hz / (n - 1), n)
    bin_Hz = s.rbw_Hz / 2
    n = int(math.floor(s.span_Hz / bin_Hz)) + 1
    if n > max_bins:
        n = int(max_bins)
        bin_Hz = s.span_Hz / (n - 1)
    elif n < 3:
        n = 3
        bin_Hz = s.span_Hz / 2
    return Grid(start, bin_Hz, n)


def estimate_sweep_time_s(s: SweepSettings, points: int) -> float:
    """A rough duration of one sweep, used for the progress bar and the sim.

    Spectrum mode: the SA44B/SA124B sweep ~1 GHz/s with a wide RBW; a narrow
    RBW needs a longer FFT per step, so the rate falls roughly as RBW squared.
    TG sweep: MEASURED on the SA44B + TG44A (2026-09-28): ~0.2 s + 1.3 ms per
    point (high dynamic range; # VERIFY without it). Capped at 10 minutes.
    Must NOT talk to the instrument: status() calls it ten times a second.
    """
    if s.tg_on:
        t = 0.2 + points * 1.3e-3
    else:
        rate = 1e9 * min(1.0, (s.rbw_Hz / 100e3) ** 2)       # Hz of span per second
        t = 0.02 + s.span_Hz / max(rate, 1.0)
    return float(min(t, 600.0))
