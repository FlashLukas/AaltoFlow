"""Simulated hardware: a fake Model 7230 with real output-filter DYNAMICS.

A lock-in simulator that returned a constant would be useless for the one thing
this module has to get right -- waiting long enough after a change -- and for
the thing people most often get wrong at the bench: the sensitivity. So this one
models what matters:

  * THE EXPERIMENT. Input A carries two things:
      - a signal locked to an EXTERNAL source at `signal_Hz` (a chopper, a
        modulated laser...), with some 2nd and 3rd harmonic content, as a real
        sample's non-linear response has;
      - a response to the lock-in's OWN oscillator: `osc_response` volts per
        volt of OSC OUT, at the oscillator frequency. With the oscillator at
        0 V (the safe default) that part is simply absent.
    `set_signal()` lets a test apply a STEP.
  * THE INPUT. "A" and "A-B" see the signal; "-B" and "ground" see only noise
    (nothing is connected to B); the two current modes see the sample as a
    current source, signal / `transimpedance_ohm`, in amps.
  * DEMODULATION. The detector sits at harmonic x reference. Every component
    of the input is multiplied by the output filter's transfer function at its
    distance from there, (1 + i 2 pi df tau)^-n -- so a wrong frequency or a
    wrong harmonic shows nothing, exactly like the real box.
  * FILTER DYNAMICS. n = slope/6 cascaded first-order stages that move towards
    their input as clock time passes, so a change takes the textbook settling
    time (filters.py).
  * NOISE. Input noise density (FET inputs noisier than bipolar at low source
    impedance) through the filter's noise bandwidth, plus a floor proportional
    to the full-scale SENSITIVITY: a 1 V range cannot resolve a microvolt.
  * OVERLOADS. |X| or |Y| beyond 300 % of full scale clips there and sets the
    output-overload bits, as the fixed-point outputs do; an input far above the
    sensitivity sets the input-overload bit (a crude stand-in for the real
    front end's dynamic reserve).
  * REFERENCE. An external reference takes `lock_time_s` to lock; until then
    the frequency meter reads 0 and status bit 3 (reference unlock) is set --
    both as the manual describes for FRQ and ST.
  * AUTO OPERATIONS. Auto-phase rotates the phase so Y is zero; auto-
    sensitivity picks the range that puts R between 30 and 90 % of full scale.

The state advances lazily, whenever something is read, from a clock you can
inject -- so tests can run in fake time.
"""

from __future__ import annotations

import cmath
import math
import random
import threading
import time
from typing import Callable

from .. import filters, tables


class Simulated7230:
    """Pretends to be a Signal Recovery Model 7230."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 seed: int | None = None):
        self._clock = clock
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._open = False
        self._t_last = clock()
        self._t0 = self._t_last

        # -- instrument state (power-up-like values) ----------------------------
        self.ref_source = 0
        self.osc_f = 1000.0
        self.osc_amp = 0.0
        self.phase_deg = 0.0
        self.harmonic = 1
        self.imode, self.vmode = 0, 1
        self.dc = False
        self.fet = False
        self.floating = False
        self.line_filter = (0, True)
        self.auto_ac_gain = True
        self.sen_index = 24
        self.fast = False
        self.tc_index = 12
        self.slope_index = 1
        self.stages: list[complex] = [0j] * 4
        self.noise = 0j
        self._lock_t = self._t_last          # when the reference last (re)started locking

        # -- the "experiment" ------------------------------------------------------
        self.signal_Hz = 1000.0
        # rms amplitude + phase of the externally referenced signal on input A
        self.signal = 2e-3 * cmath.exp(1j * math.radians(30.0))
        self.harmonics = {1: 1.0, 2: 0.15, 3: 0.04}    # relative content at k x signal_Hz
        self.osc_response = 5e-3 * cmath.exp(1j * math.radians(-40.0))  # V per V of OSC OUT
        self.transimpedance_ohm = 1e6
        self.drift = 0.02
        self.noise_V_rtHz = 8e-9             # bipolar input
        self.noise_fet_V_rtHz = 20e-9        # FET input: higher voltage noise
        self.floor_fraction = 2e-5           # noise floor as a fraction of full scale
        self.lock_time_s = 0.4
        self.adc_override: list[float | None] = [None, None]
        self.fail_reads = False              # test hook: make read_outputs raise

    # ---- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        self._open = True
        self._t_last = self._clock()

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "7230 (SIMULATED) firmware 2.20" if self._open else ""

    def read_settings(self) -> dict:
        """The simulated front panel, as the real backend's queries report it."""
        with self._lock:
            return {
                "ref_source": self.ref_source, "osc_frequency_Hz": self.osc_f,
                "osc_amplitude_V": self.osc_amp, "phase_deg": self.phase_deg,
                "harmonic": self.harmonic, "imode": self.imode, "vmode": self.vmode,
                "dc_coupled": self.dc, "fet": self.fet, "float_shield": self.floating,
                "auto_ac_gain": self.auto_ac_gain, "sensitivity_index": self.sen_index,
                "fast_mode": self.fast,
                "time_constant_s": tables.TIME_CONSTANTS_S[self.tc_index],
                "slope_index": self.slope_index,
                "line_filter_mode": self.line_filter[0], "line_50Hz": self.line_filter[1],
                "unread": [],
            }

    # ---- test hooks (not part of the backend interface) ----------------------------

    def set_signal(self, amplitude_V: float, phase_deg: float) -> None:
        """Change the external-source signal instantly -- a STEP."""
        with self._lock:
            self._advance()
            self.signal = amplitude_V * cmath.exp(1j * math.radians(phase_deg))

    def set_adc(self, index: int, volts: float | None) -> None:
        """Pin ADC input 1 or 2 (index 0/1) to a value; None = built-in waveform."""
        self.adc_override[index] = volts

    # ---- reference + oscillator -----------------------------------------------------

    def set_ref_source(self, index: int) -> None:
        with self._lock:
            self._advance()
            self.ref_source = int(index)
            self._lock_t = self._clock()

    def set_osc_frequency(self, hz: float) -> None:
        with self._lock:
            self._advance()
            self.osc_f = float(hz)

    def set_osc_amplitude(self, volts_rms: float) -> None:
        with self._lock:
            self._advance()
            self.osc_amp = float(volts_rms)

    def set_phase(self, deg: float) -> None:
        with self._lock:
            self._advance()
            self._rotate(float(deg))

    def get_phase(self) -> float:
        return self.phase_deg

    def set_harmonic(self, n: int) -> None:
        with self._lock:
            self._advance()
            self.harmonic = int(n)

    # ---- signal channel ----------------------------------------------------------------

    def set_input(self, imode: int, vmode: int) -> None:
        with self._lock:
            self._advance()
            self.imode, self.vmode = int(imode), int(vmode)

    def set_coupling(self, dc: bool) -> None:
        self.dc = bool(dc)

    def set_fet(self, fet: bool) -> None:
        self.fet = bool(fet)

    def set_float(self, floating: bool) -> None:
        self.floating = bool(floating)

    def set_line_filter(self, mode: int, fifty_hz: bool) -> None:
        self.line_filter = (int(mode), bool(fifty_hz))

    def set_auto_ac_gain(self, on: bool) -> None:
        self.auto_ac_gain = bool(on)

    def set_sensitivity_index(self, index: int) -> None:
        with self._lock:
            self._advance()
            self.sen_index = int(index)

    def get_sensitivity_index(self) -> int:
        return self.sen_index

    # ---- output filter ---------------------------------------------------------------------

    def set_fast_mode(self, on: bool) -> None:
        with self._lock:
            self._advance()
            self.fast = bool(on)

    def set_tc_index(self, index: int) -> None:
        with self._lock:
            self._advance()
            self.tc_index = int(index)

    def get_time_constant(self) -> float:
        return tables.TIME_CONSTANTS_S[self.tc_index]

    def set_slope_index(self, index: int) -> None:
        with self._lock:
            self._advance()
            self.slope_index = int(index)

    # ---- data --------------------------------------------------------------------------------

    def read_outputs(self, read_adc: bool = True) -> dict:
        if self.fail_reads:
            raise ConnectionError("simulated link failure")
        with self._lock:
            self._advance()
            fs = self._full_scale()
            z = self.stages[self.slope_index] + self.noise
            x, y = z.real, z.imag
            overload = 0
            # the fixed-point outputs span +-300 % of full scale; beyond that
            # they clip and the overload byte says which one
            if abs(x) > 3 * fs:
                overload |= 0b01
                x = math.copysign(3 * fs, x)
            if abs(y) > 3 * fs:
                overload |= 0b10
                y = math.copysign(3 * fs, y)
            status = 0b1                                # command complete
            if overload:
                status |= 1 << 4
            locked = self._locked()
            if not locked:
                status |= 1 << 3
            if abs(self._input_rms()) > 100 * fs or abs(self._input_rms()) > 3.0:
                status |= 1 << 6
            freq = self._ref_hz() if locked else 0.0
            adc = self._adc() if read_adc else [math.nan, math.nan]
            return {"x": x, "y": y, "freq_Hz": freq, "adc": adc,
                    "status": status, "overload": overload}

    # ---- automatic operations ----------------------------------------------------------------

    def auto_phase(self) -> None:
        with self._lock:
            self._advance()
            z = self._steady()
            if abs(z) > 0:
                # rotate by the signal's own angle: afterwards it lies on +X
                self._rotate(self.phase_deg + math.degrees(cmath.phase(z)))

    def auto_sensitivity(self) -> None:
        with self._lock:
            self._advance()
            r = abs(self._steady())
            table = tables.sensitivity_table(self._mode_name())
            # the smallest range that keeps R below 90 % of full scale
            fitting = [i for i, fs in sorted(table.items()) if r <= 0.9 * fs]
            self.sen_index = fitting[0] if fitting else max(table)

    def auto_measure(self) -> None:
        self.auto_sensitivity()
        self.auto_phase()

    # ---- the physics ---------------------------------------------------------------------------

    def _mode_name(self) -> str:
        if self.imode == 1:
            return "I high-BW"
        if self.imode == 2:
            return "I low-noise"
        return {0: "ground", 1: "A", 2: "-B", 3: "A-B"}[self.vmode]

    def _full_scale(self) -> float:
        table = tables.sensitivity_table(self._mode_name())
        return table.get(self.sen_index, max(table.values()))

    def _locked(self) -> bool:
        if self.ref_source == 0:
            return True
        return self._clock() - self._lock_t >= self.lock_time_s

    def _ref_hz(self) -> float:
        """What the reference channel follows: our oscillator, or the source."""
        if self.ref_source == 0:
            return self.osc_f
        if self._locked():
            return self.signal_Hz
        # unlocked: the reference wanders somewhere near -- garbage, detuned
        return self.signal_Hz * 1.013

    def _input_scale(self) -> float:
        """How much of the sample's signal this input configuration sees."""
        if self.imode in (1, 2):
            return 1.0 / self.transimpedance_ohm      # amps
        return 1.0 if self.vmode in (1, 3) else 0.0   # A, A-B: yes; -B, ground: no

    def _components(self, t: float) -> list[tuple[float, complex]]:
        """(frequency, complex rms amplitude) of everything on the input."""
        k_in = self._input_scale()
        wobble = 1.0 + self.drift * math.sin(2 * math.pi * 0.05 * t)
        out = [(k * self.signal_Hz, self.signal * rel * wobble * k_in)
               for k, rel in self.harmonics.items()]
        if self.osc_amp > 0:
            out.append((self.osc_f, self.osc_response * self.osc_amp * k_in))
        return out

    def _input_rms(self) -> float:
        return math.sqrt(sum(abs(z) ** 2 for _, z in self._components(self._clock() - self._t0)))

    def _target(self, t: float) -> complex:
        """Steady-state filter output: every component, detuned and filtered."""
        tc = tables.TIME_CONSTANTS_S[self.tc_index]
        order = self.slope_index + 1
        f_det = self.harmonic * self._ref_hz()
        z = 0j
        for f, amp in self._components(t):
            df = f - f_det
            z += amp * filters.transfer(df, tc, order) * cmath.exp(2j * math.pi * df * t)
        return z * cmath.exp(-1j * math.radians(self.phase_deg))

    def _steady(self) -> complex:
        return self._target(self._clock() - self._t0)

    def _rotate(self, new_phase_deg: float) -> None:
        """Change the reference phase. The DSP rotates its output at once, so
        the filter state is rotated too (no settling for a pure phase change)."""
        rot = cmath.exp(-1j * math.radians(new_phase_deg - self.phase_deg))
        self.stages = [s * rot for s in self.stages]
        self.phase_deg = (new_phase_deg + 180.0) % 360.0 - 180.0

    def _adc(self) -> list[float]:
        t = self._clock() - self._t0
        base = [1.25 + 0.05 * math.sin(2 * math.pi * 0.1 * t), -0.5]
        out = []
        for i in range(2):
            if self.adc_override[i] is not None:
                base[i] = self.adc_override[i]
            out.append(base[i] + self._rng.gauss(0.0, 1e-3))
        return out

    def _advance(self) -> None:
        """Move the output filter forward to 'now'."""
        now = self._clock()
        dt = now - self._t_last
        if dt <= 0:
            return
        self._t_last = now
        t = now - self._t0
        tc = tables.TIME_CONSTANTS_S[self.tc_index]
        order = self.slope_index + 1
        # Sub-step so one long gap between reads still integrates the cascade
        # properly instead of jumping each stage to its input.
        steps = max(1, min(200, int(math.ceil(dt / (0.25 * tc)))))
        h = dt / steps
        a = 1.0 - math.exp(-h / tc)
        target = self._target(t)
        for _ in range(steps):
            prev = target
            for k in range(order):
                self.stages[k] += a * (prev - self.stages[k])
                prev = self.stages[k]
        # Noise: input density through the noise bandwidth, plus a floor tied
        # to the full scale; correlated over roughly the filter's response time.
        density = self.noise_fet_V_rtHz if self.fet else self.noise_V_rtHz
        density *= max(self._input_scale(), 1.0 / self.transimpedance_ohm)
        if self.imode == 0 and self.vmode in (0, 2):
            density = self.noise_V_rtHz            # nothing connected: amplifier noise only
        sigma = math.hypot(density * math.sqrt(filters.enbw_Hz(tc, order)),
                           self.floor_fraction * self._full_scale())
        rho = math.exp(-dt / (tc * order))
        kick = math.sqrt(max(0.0, 1.0 - rho * rho)) * sigma
        self.noise = rho * self.noise + complex(self._rng.gauss(0, kick),
                                                self._rng.gauss(0, kick))
