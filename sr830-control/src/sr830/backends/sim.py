"""Simulated hardware: a fake SR830 with real low-pass filter DYNAMICS.

A lock-in simulator that returned a constant would be useless for the thing
this module has to get right -- waiting long enough after a change -- and for
the thing an SR830 user fights most -- choosing a sensitivity that neither
overloads nor buries the signal. So this one models:

  * SIGNALS. Input A (and B, for A-B) carries a sine locked to an
    "experiment" reference at `ext_ref_Hz` (a chopper, a modulated source).
    Complex amplitude in Vrms, slow +-3 % drift, `set_signal()` makes a STEP.
    In current mode (I1M / I100M) the input is a current: the same numbers
    times 1e-6, in amps.
  * DEMODULATION. Only a matching detection frequency (reference x harmonic)
    sees the signal; detuned by df it is multiplied by the filter's transfer
    function at df, so a wrong internal frequency shows nothing, as on the box.
  * FILTER DYNAMICS. 1..4 cascaded RC stages (6..24 dB/oct) that move towards
    their input as wall-clock time passes -- the textbook settling time
    (filters.py). White noise scaled by sqrt(ENBW): longer tau, quieter.
  * SENSITIVITY AND OVERLOAD. X and Y clip at 110 % of full scale and set the
    OUTPUT overload bit above 100 %. The INPUT overloads when the signal
    exceeds full scale times the dynamic reserve (a rough 20/40/60 dB for
    low noise / normal / high -- the real reserve also depends on the
    sensitivity). The bits LATCH until read_lia_status(), as LIAS? does.
  * REFERENCE. External mode locks after max(0.3 s, 20 periods); until then
    the UNLOCK bit is set and the frequency is wherever the PLL was.
  * THE 200 Hz RULE. Time constants above 30 s are not allowed with a
    detection frequency above 200 Hz: the sim applies 30 s and sets the
    "time constant changed" bit, so the brain's read-back path is exercised.
  * AUTO FUNCTIONS. Auto gain / reserve / phase take time and report busy()
    meanwhile, then change the setting the way the real one would. A QUERY
    sent while one runs fails, as the real GPIB read would time out (the
    SR830 does not answer until the auto function is done) -- so a brain that
    polls at the wrong moment shows up as an error here, not only on the rig.

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

#: Output overload threshold and clip level, as fractions of full scale (sim only).
OVERLOAD_AT = 1.0
CLIP_AT = 1.1


class SimulatedSR830:
    """Pretends to be a Stanford Research SR830."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 seed: int | None = None):
        self._clock = clock
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._open = False
        self._t_last = clock()
        self._t0 = self._t_last

        # -- instrument state (GPIB parameters) -------------------------------
        self.internal = True
        self.osc_hz = 1000.0
        self.harmonic = 1
        self.phase_deg = 0.0
        self.trigger = 0
        self.sine_V = 0.004
        self.source = 0          # ISRC
        self.ground = 0
        self.coupling = 0
        self.line = 0
        self.sens = 26           # *RST default 1 V
        self.reserve = 1
        self.tc = 8              # 100 ms
        self.slope = 1           # 12 dB/oct
        self.sync = False
        self.aux_out = [0.0] * 4
        self.stages: list[complex] = [0j] * 4
        self.noise = 0j
        self._lock_t = self._t0          # when the reference last changed
        self._pll_hz = 1000.0            # where the external PLL currently sits
        self._lias = 0                   # latched LIA status bits
        self._auto: dict | None = None   # a running auto function
        self._x = self._y = 0.0          # last clipped outputs

        # -- the "experiment" ---------------------------------------------------
        self.ext_ref_Hz = 1000.0
        self.signal_A = 2e-3 * cmath.exp(1j * math.radians(30.0))   # Vrms
        self.signal_B = 0j
        self.drift = 0.03
        self.noise_V_rtHz = 5e-6
        self.aux_in_override: list[float | None] = [None] * 4
        self.auto_gain_s = 0.4           # how long each auto function "takes"
        self.auto_reserve_s = 0.3
        self.auto_phase_s = 0.05

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        self._open = True
        self._t_last = self._clock()

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "Stanford_Research_Systems,SR830,SIMULATED,ver1.07" if self._open else ""

    # ---- test hooks (not part of the backend interface) --------------------

    def set_signal(self, amplitude_V: float, phase_deg: float, which: str = "A") -> None:
        """Change an input signal instantly -- a STEP the filter must follow."""
        with self._lock:
            self._advance()
            z = amplitude_V * cmath.exp(1j * math.radians(phase_deg))
            if which == "B":
                self.signal_B = z
            else:
                self.signal_A = z

    def set_aux_in(self, index: int, volts: float | None) -> None:
        """Pin an AUX IN (0-based) to a value (None = back to the built-in waveform)."""
        self.aux_in_override[index] = volts

    # ---- setters -------------------------------------------------------------

    def set_ref_source(self, internal: bool) -> None:
        with self._lock:
            self._advance()
            self.internal = bool(internal)
            self._lock_t = self._clock()
            self._pll_hz = self.osc_hz

    def set_frequency(self, hz: float) -> None:
        with self._lock:
            self._advance()
            before = self._detect_hz()
            self.osc_hz = float(f"{float(hz):.5g}")      # "rounded to 5 digits"
            self._range_check(before)

    def set_harmonic(self, n: int) -> None:
        with self._lock:
            self._advance()
            before = self._detect_hz()
            self.harmonic = max(1, int(n))
            self._range_check(before)

    def set_phase(self, deg: float) -> None:
        with self._lock:
            self._advance()
            self.phase_deg = _wrap180(round(float(deg), 2))

    def set_trigger(self, i: int) -> None:
        self.trigger = int(i)

    def set_sine_out(self, volts: float) -> None:
        self.sine_V = round(float(volts) / 0.002) * 0.002        # 2 mV steps

    def set_input(self, source: int, ground: int, coupling: int, line: int) -> None:
        with self._lock:
            self._advance()
            self.source, self.ground = int(source), int(ground)
            self.coupling, self.line = int(coupling), int(line)

    def set_sensitivity(self, i: int) -> None:
        with self._lock:
            self._advance()
            self.sens = int(i)

    def set_reserve(self, i: int) -> None:
        with self._lock:
            self._advance()
            self.reserve = int(i)

    def set_time_constant(self, i: int) -> None:
        with self._lock:
            self._advance()
            i = int(i)
            if i >= tables.TC_LONG_FIRST_INDEX and self._detect_hz() > tables.TC_LONG_MAX_FREQ_HZ:
                i = tables.TC_LONG_FIRST_INDEX - 1
                self._lias |= 1 << 5
            self.tc = i

    def set_slope(self, i: int) -> None:
        with self._lock:
            self._advance()
            self.slope = int(i)

    def set_sync(self, on: bool) -> None:
        self.sync = bool(on)

    def set_aux_out(self, k: int, volts: float) -> None:
        self.aux_out[int(k) - 1] = round(float(volts), 3)        # "nearest mV"

    # ---- read-back ---------------------------------------------------------------

    def _refuse_if_busy(self) -> None:
        """Called with _lock held, after _advance()."""
        if self._auto is not None:
            raise TimeoutError("SR830 did not answer: an auto function is running "
                               "(query sent while busy)")

    def read_settings(self) -> dict:
        with self._lock:
            self._advance()
            self._refuse_if_busy()
            return {"sens": self.sens, "reserve": self.reserve, "tc": self.tc,
                    "slope": self.slope, "phase_deg": self.phase_deg,
                    "harmonic": self.harmonic, "sine_out_V": self.sine_V}

    def read_outputs(self) -> dict:
        with self._lock:
            self._advance()
            self._refuse_if_busy()
            return {"x": self._x, "y": self._y, "freq_Hz": self._ref_hz()}

    def read_aux(self) -> list[float]:
        with self._lock:
            self._advance()
            self._refuse_if_busy()
        t = self._clock() - self._t0
        base = [0.25 + 0.05 * math.sin(2 * math.pi * 0.1 * t), -1.0, 0.0,
                2.5 + 0.2 * math.sin(2 * math.pi * 0.03 * t)]
        out = []
        for i in range(4):
            v = self.aux_in_override[i] if self.aux_in_override[i] is not None else base[i]
            v += self._rng.gauss(0.0, 3e-4)
            out.append(round(v * 3000.0) / 3000.0)               # 1/3 mV resolution
        return out

    def read_lia_status(self) -> int:
        with self._lock:
            self._advance()
            self._refuse_if_busy()
            bits, self._lias = self._lias, 0
            return bits

    # ---- auto functions -----------------------------------------------------------

    def auto(self, name: str) -> None:
        with self._lock:
            self._advance()
            dur = {"gain": self.auto_gain_s, "reserve": self.auto_reserve_s,
                   "phase": self.auto_phase_s}[name]
            # "AGAN does nothing if the time constant is greater than 1 second"
            if name == "gain" and tables.TC_SECONDS[self.tc] > 1.0:
                dur = 0.0
            self._auto = {"name": name, "t_end": self._clock() + dur,
                          "noop": name == "gain" and dur == 0.0}

    def busy(self) -> bool:
        with self._lock:
            self._advance()
            return self._auto is not None

    def _finish_auto(self) -> None:
        a, self._auto = self._auto, None
        if a is None or a.get("noop"):
            return
        z = self._steady_state()
        src = tables.INPUT_SOURCES[self.source]
        if a["name"] == "gain":
            # the smallest range that holds the signal with some headroom
            r = abs(z)
            for i, _ in enumerate(tables.SENS_VOLTS):
                if tables.sens_full_scale(i, src) >= 1.25 * r:
                    self.sens = i
                    break
            else:
                self.sens = len(tables.SENS_VOLTS) - 1
        elif a["name"] == "reserve":
            # the lowest reserve (least noise) that does not overload the input
            for i in (2, 1, 0):
                if not self._input_overloaded(i):
                    self.reserve = i
                    break
            else:
                self.reserve = 0
        elif a["name"] == "phase":
            # rotate the reference so the signal lands on +X
            if abs(z) > 0:
                self.phase_deg = _wrap180(round(self.phase_deg + math.degrees(cmath.phase(z)), 2))

    # ---- the physics -----------------------------------------------------------

    def _ref_hz(self) -> float:
        """Reference frequency as FREQ? reports it."""
        if self.internal:
            return self.osc_hz
        return self._pll_hz

    def _locked(self) -> bool:
        if self.internal:
            return True
        lock_s = max(0.3, 20.0 / max(self.ext_ref_Hz, 1e-3))
        return self._clock() - self._lock_t >= lock_s

    def _detect_hz(self) -> float:
        return self._ref_hz() * self.harmonic

    def _range_check(self, before_hz: float) -> None:
        """The 200 Hz rule: crossing it switches range (LIAS bit 4), and above it
        a time constant longer than 30 s is cut to 30 s (bit 5)."""
        after = self._detect_hz()
        lim = tables.TC_LONG_MAX_FREQ_HZ
        if (before_hz <= lim) != (after <= lim):
            self._lias |= 1 << 4
        if after > lim and self.tc >= tables.TC_LONG_FIRST_INDEX:
            self.tc = tables.TC_LONG_FIRST_INDEX - 1
            self._lias |= 1 << 5

    def _input_signal(self) -> complex:
        """The signal at the input, in the source's unit (V, or A in current mode)."""
        src = tables.INPUT_SOURCES[self.source]
        if src == "A-B":
            z = self.signal_A - self.signal_B
        else:
            z = self.signal_A
        if tables.is_current(src):
            z *= tables.CURRENT_PER_VOLT
        return z

    def _input_overloaded(self, reserve: int | None = None) -> bool:
        src = tables.INPUT_SOURCES[self.source]
        res_db = tables.RESERVE_DB[tables.RESERVES[self.reserve if reserve is None else reserve]]
        fs = tables.sens_full_scale(self.sens, src)
        return abs(self._input_signal()) > fs * 10.0 ** (res_db / 20.0)

    def _steady_state(self, t: float | None = None) -> complex:
        """Filter output once settled, for the current settings (no noise)."""
        t = (self._clock() - self._t0) if t is None else t
        order = self.slope + 1
        tau = tables.TC_SECONDS[self.tc]
        if self.internal:
            ref = self.osc_hz
        else:
            ref = self.ext_ref_Hz if self._locked() else self._pll_hz
        df = self.ext_ref_Hz - ref * self.harmonic
        wobble = 1.0 + self.drift * math.sin(2 * math.pi * 0.05 * t)
        z = self._input_signal() * wobble * cmath.exp(-1j * math.radians(self.phase_deg))
        return z * filters.transfer(df, tau, order) * cmath.exp(2j * math.pi * df * t)

    def _advance(self) -> None:
        """Move the filter forward to 'now', update outputs and status bits."""
        now = self._clock()
        dt = now - self._t_last
        if self._auto is not None and now >= self._auto["t_end"]:
            self._finish_auto()
        if dt <= 0:
            return
        self._t_last = now
        t = now - self._t0
        if not self.internal:
            if self._locked():
                self._pll_hz = self.ext_ref_Hz
            else:
                self._lias |= 1 << 3
        order = self.slope + 1
        tau = tables.TC_SECONDS[self.tc]
        steps = max(1, min(200, int(math.ceil(dt / (0.25 * tau)))))
        h = dt / steps
        a = 1.0 - math.exp(-h / tau)
        target = self._steady_state(t)
        if self._input_overloaded():
            self._lias |= 1 << 0
        for _ in range(steps):
            prev = target
            for k in range(order):
                self.stages[k] += a * (prev - self.stages[k])
                prev = self.stages[k]
        src = tables.INPUT_SOURCES[self.source]
        scale = tables.CURRENT_PER_VOLT if tables.is_current(src) else 1.0
        sigma = self.noise_V_rtHz * scale * math.sqrt(filters.enbw_Hz(tau, order))
        rho = math.exp(-dt / (tau * order))
        kick = math.sqrt(max(0.0, 1.0 - rho * rho)) * sigma
        self.noise = rho * self.noise + complex(self._rng.gauss(0, kick),
                                                self._rng.gauss(0, kick))
        z = self.stages[order - 1] + self.noise
        fs = tables.sens_full_scale(self.sens, src)
        if abs(z) > OVERLOAD_AT * fs:
            self._lias |= 1 << 2
        lim = CLIP_AT * fs
        self._x = max(-lim, min(lim, z.real))
        self._y = max(-lim, min(lim, z.imag))


def _wrap180(deg: float) -> float:
    """-360..729.99 folded to (-180, 180], as PHAS does (541 -> -179)."""
    d = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d
