"""Simulated hardware: a fake HF2LI with real low-pass filter DYNAMICS.

A lock-in simulator that just returned a constant would be useless for the one
thing this module has to get right -- waiting long enough after a change. So
this one models what matters:

  * SIGNALS. Each signal input carries a sine at a frequency locked to one of
    two external reference sources (as in a real modulation experiment: the
    signal is modulated by whatever produces the reference). Its complex
    amplitude drifts slowly. `set_signal()` lets a test apply a STEP.
  * DEMODULATION. A demodulator only sees the signal if its frequency matches.
    Detuned by df, the output is multiplied by the filter's transfer function
    at df, which rolls off as (2 pi df tau)^-n -- so in internal mode you have
    to set the right frequency to see anything, exactly like the real box.
  * FILTER DYNAMICS. Each demodulator is n cascaded first-order stages that
    move towards their input as wall-clock time passes. Change the input and
    the output takes the textbook settling time (see `filters.py`).
  * NOISE. White noise at the input, scaled by sqrt(noise bandwidth), with a
    correlation time of about the filter's own. Longer tau = quieter output.
  * REFERENCE. In external mode the oscillator's PLL takes `lock_time_s` to
    lock; until then `pll_locked()` is False.
  * TIME CONSTANT ROUNDING. The real instrument does not apply exactly the
    requested tau. The sim rounds to 4 significant figures so the brain's
    "requested vs applied" bookkeeping is exercised.

The state advances lazily, whenever something is read, from a clock you can
inject -- so tests can run in fake time if they want to.
"""

from __future__ import annotations

import cmath
import math
import random
import threading
import time
from typing import Callable, Sequence

from ..config import Channel
from .. import filters

N_DEMODS = 6
N_OSCS = 2


class _Demod:
    def __init__(self):
        self.tc = 0.01
        self.order = 4
        self.osc = 0
        self.input = 0
        self.harmonic = 1
        self.phase_deg = 0.0
        self.enabled = False
        self.stages: list[complex] = [0j] * 8      # filter state, one per stage
        self.noise = 0j                            # correlated noise state


class SimulatedHF2:
    """Pretends to be a Zurich Instruments HF2LI."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 seed: int | None = None):
        self._clock = clock
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._open = False
        self._t_last = clock()
        self._t0 = self._t_last

        self.demods = [_Demod() for _ in range(N_DEMODS)]
        self.osc_freq = [1000.0, 1000.0]
        self.osc_external = [False, False]
        self.osc_ref_input = [0, 1]
        self._lock_t = [0.0, 0.0]          # when the PLL was (re)started

        # -- the "experiment" ------------------------------------------------
        # Two reference sources, e.g. a chopper and a modulated RF source.
        self.ext_ref_Hz = [1234.5, 7777.0]
        # The signal on input i sits at reference i's frequency.
        self.signal_ref = [0, 1]
        # Complex amplitude (V, peak) per input; slow +-3 % drift on top.
        self.signal = [2e-3 * cmath.exp(1j * math.radians(30.0)),
                       0.5e-3 * cmath.exp(1j * math.radians(-60.0))]
        self.drift = 0.03
        self.noise_V_rtHz = 5e-6           # input-referred white noise density
        self.aux_override: list[float | None] = [None, None]
        self.lock_time_s = 0.3

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        self._open = True
        self._t_last = self._clock()

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "Zurich Instruments,HF2LI,SIMULATED,dev0000" if self._open else ""

    # ---- test hooks (not part of the backend interface) --------------------

    def set_signal(self, signal_input: int, amplitude_V: float, phase_deg: float) -> None:
        """Change the input signal instantly -- a STEP the filter must follow."""
        with self._lock:
            self._advance()
            self.signal[signal_input] = amplitude_V * cmath.exp(1j * math.radians(phase_deg))

    def set_aux(self, index: int, volts: float | None) -> None:
        """Pin an AUX input to a value (None = back to the built-in waveform)."""
        self.aux_override[index] = volts

    # ---- set-up ------------------------------------------------------------

    def setup_channel(self, ch: Channel, rate_Sa_s: float) -> None:
        with self._lock:
            self._advance()
            d = self.demods[ch.demod]
            d.input = int(ch.signal_input)
            d.osc = int(ch.oscillator)
            d.harmonic = max(1, int(ch.harmonic))
            d.phase_deg = float(ch.phase_deg)
            d.enabled = True

    def set_reference(self, oscillator: int, external: bool, ref_input: int) -> None:
        with self._lock:
            self._advance()
            self.osc_external[oscillator] = bool(external)
            self.osc_ref_input[oscillator] = int(ref_input)
            self._lock_t[oscillator] = self._clock()

    def pll_locked(self, oscillator: int) -> bool:
        with self._lock:
            return (self.osc_external[oscillator]
                    and self._clock() - self._lock_t[oscillator] >= self.lock_time_s)

    def set_oscillator_frequency(self, oscillator: int, hz: float) -> None:
        with self._lock:
            self._advance()
            self.osc_freq[oscillator] = float(hz)

    def set_time_constant(self, demod: int, tc_s: float) -> None:
        with self._lock:
            self._advance()
            # the real hardware rounds; 4 significant figures stands in for that
            self.demods[demod].tc = float(f"{float(tc_s):.4g}")

    def get_time_constant(self, demod: int) -> float:
        return self.demods[demod].tc

    def set_order(self, demod: int, order: int) -> None:
        with self._lock:
            self._advance()
            self.demods[demod].order = int(order)

    def get_order(self, demod: int) -> int:
        return self.demods[demod].order

    # ---- data ----------------------------------------------------------------

    def read_demods(self, demods: Sequence[int]) -> list[dict]:
        with self._lock:
            self._advance()
            out = []
            for i in demods:
                d = self.demods[i]
                z = d.stages[d.order - 1] + d.noise
                out.append({"x": z.real, "y": z.imag,
                            "freq_Hz": self._osc_hz(d.osc) * d.harmonic})
            return out

    def read_aux(self) -> list[float]:
        t = self._clock() - self._t0
        base = [0.25 + 0.05 * math.sin(2 * math.pi * 0.1 * t), -1.0]
        out = []
        for i in range(2):
            if self.aux_override[i] is not None:
                base[i] = self.aux_override[i]
            out.append(base[i] + self._rng.gauss(0.0, 1e-3))
        return out

    # ---- the physics -----------------------------------------------------------

    def _osc_hz(self, osc: int) -> float:
        """What the oscillator is really doing. Locked PLL = the reference."""
        if self.osc_external[osc]:
            locked = self._clock() - self._lock_t[osc] >= self.lock_time_s
            if locked:
                return self.ext_ref_Hz[self.osc_ref_input[osc]]
        return self.osc_freq[osc]

    def _target(self, d: _Demod, t: float) -> complex:
        """Steady-state filter output for the signal on this demod's input."""
        ref = self.signal_ref[d.input]
        f_signal = self.ext_ref_Hz[ref]
        df = f_signal - self._osc_hz(d.osc) * d.harmonic
        wobble = 1.0 + self.drift * math.sin(2 * math.pi * 0.05 * t + d.input)
        z = self.signal[d.input] * wobble
        z *= cmath.exp(-1j * math.radians(d.phase_deg))
        # A detuned demodulator sees a rotating phasor, attenuated by the filter.
        return z * filters.transfer(df, d.tc, d.order) * cmath.exp(2j * math.pi * df * t)

    def _advance(self) -> None:
        """Move every enabled demodulator's filter forward to 'now'."""
        now = self._clock()
        dt = now - self._t_last
        if dt <= 0:
            return
        self._t_last = now
        t = now - self._t0
        for d in self.demods:
            if not d.enabled:
                continue
            # Sub-step so one long gap between reads still integrates the
            # cascade properly instead of jumping each stage to its input.
            steps = max(1, min(200, int(math.ceil(dt / (0.25 * d.tc)))))
            h = dt / steps
            a = 1.0 - math.exp(-h / d.tc)
            target = self._target(d, t)
            for _ in range(steps):
                prev = target
                for k in range(d.order):
                    d.stages[k] += a * (prev - d.stages[k])
                    prev = d.stages[k]
            # Correlated noise: std = density * sqrt(ENBW) per quadrature,
            # decorrelating over roughly the filter's response time.
            sigma = self.noise_V_rtHz * math.sqrt(filters.enbw_Hz(d.tc, d.order))
            rho = math.exp(-dt / (d.tc * d.order))
            kick = math.sqrt(max(0.0, 1.0 - rho * rho)) * sigma
            d.noise = rho * d.noise + complex(self._rng.gauss(0, kick),
                                              self._rng.gauss(0, kick))
