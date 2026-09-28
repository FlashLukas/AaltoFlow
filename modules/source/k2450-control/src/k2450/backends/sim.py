"""Simulated hardware: a fake Keithley 2450 with a pretend sample on its leads.

It implements SourceMeterBackend from `base`, so the brain cannot tell it apart
from the real instrument. The physics is simple but not fake -- it behaves the
way an SMU on a bench does in the ways that matter for testing:

  * The sample is a resistor, a diode (Shockley equation + series resistance)
    or an open circuit (config group [sim]).
  * COMPLIANCE works like the real thing. Sourcing voltage into a load that
    would draw more than the current limit, the SMU stops being a voltage
    source and becomes a current source AT the limit: the current reads the
    limit, the voltage readback drops below the setpoint, and `tripped` is set.
    The same, mirrored, when sourcing current with a voltage limit.
  * 2-wire vs 4-wire: in 2-wire the lead resistance sits in series with the
    sample and ends up in V (and so in R = V/I); 4-wire senses at the sample
    and it drops out.
  * Noise is a fraction of the measure RANGE (as on a real meter: the small
    ranges are the quiet ones) and falls as 1/sqrt(NPLC).
  * A reading TAKES TIME: NPLC / line frequency plus a little overhead, so a
    10-NPLC acquisition is visibly slower than a 0.1-NPLC one, as on the bench.
  * A fixed measure range that is too small overflows (reading NaN + flag).
"""

from __future__ import annotations

import math
import random
import threading
import time

from ..config import Sim
from .base import (FUNCS, OVERRANGE, InstrumentState, Reading, auto_range_for,
                   other, snap_range)

#: kT/q at room temperature, the diode's thermal voltage.
_VT = 0.025852


class SimulatedK2450:
    """Pretends to be a Keithley 2450 SourceMeter wired to a sample."""

    def __init__(self, sim: Sim | None = None, line_freq_Hz: float = 50.0,
                 seed: int | None = None, realtime: bool = True):
        # `sim` is the LIVE config group, read at each reading, so a change of
        # the pretend sample over set_config takes effect at once.
        self.sim = sim or Sim()
        self.line_freq_Hz = float(line_freq_Hz)
        self.realtime = realtime        # False: skip the integration sleep (fast tests)
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._open = False
        # remembered instrument state (the 2450 powers up sourcing 0 V, output off)
        self._fn = "voltage"
        self._level = {"voltage": 0.0, "current": 0.0}
        self._limit = {"voltage": 1.05e-4, "current": 21.0}   # ILIM / VLIM
        self._src_auto = {"voltage": True, "current": True}
        self._src_range = {"voltage": 20.0, "current": 1e-2}
        self._meas_auto = {"voltage": True, "current": True}
        self._meas_range = {"voltage": 2.0, "current": 1e-4}
        self._nplc = {"voltage": 1.0, "current": 1.0}
        self._four_wire = False
        self._output = False
        self._terminals = "front"
        self._sense = "current"         # what it measures (the real one can differ)
        # Every call that CHANGES the instrument is logged here, so a test can
        # prove that connecting wrote nothing (the adopt-on-start rule).
        self.writes: list[tuple] = []

    def preset(self, *, function: str | None = None, level: dict | None = None,
               limit: dict | None = None, output: bool | None = None,
               nplc: float | None = None, four_wire: bool | None = None,
               src_auto: bool | None = None, src_range: dict | None = None,
               meas_auto: bool | None = None, meas_range: dict | None = None,
               terminals: str | None = None, sense: str | None = None) -> None:
        """Put the pretend instrument in a state BEFORE the module connects --
        as if someone had used the front panel, or a previous session left it
        sourcing. Tests use it to check the brain ADOPTS that state instead of
        overwriting it. Not logged in `writes` (it is not the module talking)."""
        if function is not None:
            self._fn = function
            self._sense = other(function)
        if level:
            self._level.update({k: float(v) for k, v in level.items()})
        if limit:
            self._limit.update({k: abs(float(v)) for k, v in limit.items()})
        if output is not None:
            self._output = bool(output)
        if nplc is not None:
            self._nplc = {f: float(nplc) for f in FUNCS}
        if four_wire is not None:
            self._four_wire = bool(four_wire)
        if src_auto is not None:
            self._src_auto = {f: bool(src_auto) for f in FUNCS}
        if src_range:
            self._src_range.update({k: snap_range(k, v) for k, v in src_range.items()})
        if meas_auto is not None:
            self._meas_auto = {f: bool(meas_auto) for f in FUNCS}
        if meas_range:
            self._meas_range.update({k: snap_range(k, v) for k, v in meas_range.items()})
        if terminals is not None:
            self._terminals = terminals
        if sense is not None:
            self._sense = sense

    # ---- lifecycle -----------------------------------------------------------
    def open(self) -> None:
        # Like the real one: connecting changes nothing (no output off).
        self._open = True

    def read_state(self) -> InstrumentState:
        return InstrumentState(
            function=self._fn, sense_function=self._sense,
            level=dict(self._level), limit=dict(self._limit),
            src_auto=dict(self._src_auto),
            src_range={f: self.get_source_range(f) for f in FUNCS},
            meas_auto=dict(self._meas_auto), meas_range=dict(self._meas_range),
            nplc=dict(self._nplc), four_wire={f: self._four_wire for f in FUNCS},
            output=self._output, terminals=self._terminals, readback=True)

    def close(self) -> None:
        self._output = False            # output off on the way out
        self._open = False

    def idn(self) -> str:
        return ("KEITHLEY INSTRUMENTS,MODEL 2450,SIMULATED,0.0"
                if self._open else "")

    # ---- source --------------------------------------------------------------
    def set_source_function(self, fn: str) -> None:
        self.writes.append(("function", fn))
        self._sense = other(fn)
        self._fn = fn

    def set_limit(self, fn: str, value: float) -> None:
        self.writes.append(("limit", fn, value))
        self._limit[fn] = abs(float(value))

    def set_level(self, fn: str, value: float) -> None:
        self.writes.append(("level", fn, value))
        self._level[fn] = float(value)

    def set_source_range(self, fn: str, auto: bool, value: float) -> None:
        self.writes.append(("source_range", fn, auto, value))
        self._src_auto[fn] = bool(auto)
        if not auto:
            self._src_range[fn] = snap_range(fn, value)

    def get_source_range(self, fn: str) -> float:
        if self._src_auto[fn]:
            return auto_range_for(fn, self._level[fn])
        return self._src_range[fn]

    # ---- measure ---------------------------------------------------------------
    def set_measure_range(self, mfn: str, auto: bool, value: float) -> None:
        self.writes.append(("measure_range", mfn, auto, value))
        self._meas_auto[mfn] = bool(auto)
        if not auto:
            self._meas_range[mfn] = snap_range(mfn, value)

    def get_measure_range(self, mfn: str) -> float:
        return self._meas_range[mfn]

    def set_nplc(self, mfn: str, nplc: float) -> None:
        self.writes.append(("nplc", mfn, nplc))
        self._nplc[mfn] = float(nplc)

    def set_four_wire(self, on: bool) -> None:
        self.writes.append(("four_wire", on))
        self._four_wire = bool(on)

    # ---- output and readings -----------------------------------------------------
    def set_output(self, on: bool) -> None:
        self.writes.append(("output", on))
        self._output = bool(on)

    def set_terminals(self, where: str) -> None:
        self.writes.append(("terminals", where))
        self._terminals = "rear" if str(where).lower() == "rear" else "front"
        self._output = False            # the 2450 drops the output on a terminal change

    def get_output(self) -> bool:
        return self._output

    def measure(self) -> Reading:
        fn = self._fn
        mfn = other(fn)
        nplc = self._nplc[mfn]
        if self.realtime:
            # one integration of NPLC mains cycles, plus ~2 ms of overhead
            time.sleep(nplc / self.line_freq_Hz + 0.002)
        if not self._output:
            # The real 2450 measures with the output off too (it reads ~0).
            v_true, i_true, tripped = 0.0, 0.0, False
        else:
            v_true, i_true, tripped = self._operating_point()

        meas_true = i_true if fn == "voltage" else v_true
        src_true = v_true if fn == "voltage" else i_true

        # the range this reading is taken on
        if self._meas_auto[mfn]:
            mrange = auto_range_for(mfn, meas_true)
            self._meas_range[mfn] = mrange
        else:
            mrange = self._meas_range[mfn]
        srange = self.get_source_range(fn)

        noise = self.sim.noise_ppm * 1e-6 / math.sqrt(max(nplc, 1e-3))
        overflow = abs(meas_true) > mrange * OVERRANGE
        measured = (float("nan") if overflow
                    else meas_true + self._rng.gauss(0.0, noise * mrange))
        source = src_true + self._rng.gauss(0.0, noise * srange)
        return Reading(measured=measured, source=source, tripped=tripped,
                       overflow=overflow, measure_range=mrange, source_range=srange)

    # ---- the pretend sample -----------------------------------------------------------

    def _series_ohm(self) -> float:
        """Resistance in series with the junction, as seen by the sense point."""
        s = self.sim
        r = float(s.diode_rs_ohm) if s.load == "diode" else 0.0
        if not self._four_wire:
            r += float(s.lead_resistance_ohm)   # 2-wire: the leads are in the loop
        return r

    def _f(self, vd: float) -> float:
        """Current through the sample for a voltage `vd` across its core."""
        s = self.sim
        if s.load == "diode":
            x = min(vd / (float(s.diode_n) * _VT), 200.0)     # no overflow of exp()
            # + a 1 Gohm shunt: keeps the curve strictly monotonic in reverse,
            # like any real junction's leakage
            return float(s.diode_is_A) * math.expm1(x) + vd / 1e9
        if s.load == "open":
            return vd / 1e12
        return vd / max(float(s.resistance_ohm), 1e-6)

    def _g(self, vd: float) -> float:
        """Sensed voltage for a core voltage vd (adds the series drop)."""
        return vd + self._f(vd) * self._series_ohm()

    def _operating_point(self) -> tuple[float, float, bool]:
        """(sensed V, I, in compliance) for the present settings."""
        fn = self._fn
        if fn == "voltage":
            vs = self._level["voltage"]
            ilim = self._limit["voltage"]
            vd = _solve(self._g, vs, min(0.0, vs), max(0.0, vs))
            i = self._f(vd)
            if abs(i) <= ilim:
                return vs, i, False
            # compliance: the SMU becomes a current source at +-ILIM
            i = math.copysign(ilim, i)
            vd = _solve(self._f, i, min(0.0, vd), max(0.0, vd))
            return self._g(vd), i, True
        # sourcing current
        iset = self._level["current"]
        vlim = self._limit["current"]
        # the largest currents the load can take within +-VLIM
        vd_hi = _solve(self._g, vlim, 0.0, vlim)
        vd_lo = _solve(self._g, -vlim, -vlim, 0.0)
        if iset > self._f(vd_hi):
            return vlim, self._f(vd_hi), True
        if iset < self._f(vd_lo):
            return -vlim, self._f(vd_lo), True
        vd = _solve(self._f, iset, vd_lo, vd_hi)
        return self._g(vd), iset, False


def _solve(fun, target: float, lo: float, hi: float, n: int = 100) -> float:
    """Bisection for an INCREASING function: fun(x) = target within [lo, hi]."""
    for _ in range(n):
        mid = 0.5 * (lo + hi)
        if fun(mid) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
