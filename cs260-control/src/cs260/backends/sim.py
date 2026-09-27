"""Simulated hardware: a fake Cornerstone 260.

It implements MonochromatorBackend, so the brain cannot tell it apart from the
real instrument. What it gets right, because the rest of the suite depends on it:

  * moves TAKE TIME. The drive slews at a finite speed (205 nm/s with a
    1200 l/mm grating, datasheet; faster in nm/s for coarser gratings, because
    the motor turns the grating at a fixed angular rate), a grating swap takes
    seconds, the filter wheel and the exit mirror take about a second. So a
    scan that did not wait for `moving` to fall would visibly measure at the
    wrong wavelength here too.
  * the drive stops at the motor STEP closest to the request, not exactly on it.
  * a grating change parks the drive where the manual says the real one does:
    grating 1 at its maximum wavelength, gratings 2 and 3 at zero order.
  * ABORT stops the drive where it is.
  * asking for a filter wheel or a second port that is not fitted sets error 6
    (accessory not present); a wavelength beyond the grating's mechanical
    maximum sets error 3 (destination not allowed) -- the brain clamps first,
    so these are only reached by a misconfigured envelope.

Time comes from an injectable `clock` so tests can step it instead of sleeping.
"""

from __future__ import annotations

import time

from ..config import Config
from .base import MonoState

#: Motor resolution of the sim in nm per step at 1200 l/mm (scales as
#: 1200/lines). A plausible order of magnitude for a 1/4 m instrument; the real
#: value never matters to this code, which reads WAVE? on hardware.
_NM_PER_STEP_AT_1200 = 0.01


def mech_max_nm(lines: int) -> float:
    """Mechanical maximum wavelength: ~1600 nm at 1200 l/mm (manual, GOWAVE)."""
    return 1600.0 * 1200.0 / max(1, int(lines))


class SimulatedCS260:
    """Pretends to be an Oriel Cornerstone 260 monochromator."""

    simulated = True

    def __init__(self, cfg: Config | None = None, clock=time.monotonic):
        self.cfg = cfg or Config()
        self._clock = clock
        s = self.cfg.sim
        self._open = False
        self._grating = 1
        self._wl = self._quantise(float(s.start_nm))
        self._shutter = bool(s.start_shutter_open)
        self._filter = 1 if self.cfg.accessories.filter_wheel else 0
        self._port = 1
        self._error: int | None = None
        # the one move in progress: (kind, t0, t1, from, to) or None
        self._move = None

    # ---- helpers -------------------------------------------------------------

    def _lines(self, n: int | None = None) -> int:
        return self.cfg.gratings.of(n or self._grating)[0]

    def _nm_per_step(self) -> float:
        return _NM_PER_STEP_AT_1200 * 1200.0 / self._lines()

    def _quantise(self, nm: float) -> float:
        q = _NM_PER_STEP_AT_1200 * 1200.0 / self._lines(getattr(self, "_grating", 1))
        return round(nm / q) * q

    def _slew(self) -> float:
        return float(self.cfg.sim.slew_nm_per_s_at_1200) * 1200.0 / self._lines()

    def _advance(self) -> None:
        """Bring the simulated mechanics up to the current clock time."""
        if self._move is None:
            return
        kind, t0, t1, a, b = self._move
        now = self._clock()
        if kind == "wave":
            if now >= t1:
                self._wl = b
                self._move = None
            else:
                frac = (now - t0) / max(1e-9, t1 - t0)
                self._wl = self._quantise(a + (b - a) * frac)
        elif now >= t1:
            if kind == "grating":
                self._grating = int(b)
                # manual: grating 1 stops at its maximum, 2/3 at zero order
                self._wl = self._quantise(mech_max_nm(self._lines()) if b == 1 else 0.0)
            elif kind == "filter":
                self._filter = int(b)
            elif kind == "port":
                self._port = int(b)
            self._move = None

    def _start(self, kind, duration, a, b):
        self._advance()
        now = self._clock()
        self._move = (kind, now, now + max(0.0, duration), a, b)

    # ---- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def idn(self) -> str:
        return "Oriel,Cornerstone 260 (SIMULATED),SN0,V0.0" if self._open else ""

    def grating_info(self, n: int) -> tuple[int, str] | None:
        lines, label, _, _ = self.cfg.gratings.of(n)
        return lines, label

    def read_state(self) -> MonoState:
        self._advance()
        err, self._error = self._error, None      # reading the error clears it
        return MonoState(
            wavelength_nm=float(self._wl),
            grating=self._grating,
            shutter_open=self._shutter,
            filter=self._filter,
            port=self._port,
            step_position=int(round(self._wl / self._nm_per_step())),
            moving=self._move is not None,
            error_code=err,
        )

    # ---- moves ---------------------------------------------------------------

    def goto(self, nm: float) -> None:
        self._advance()
        if nm < 0 or nm > mech_max_nm(self._lines()):
            self._error = 3                       # destination not allowed
            return
        target = self._quantise(float(nm))
        dt = float(self.cfg.sim.move_overhead_s) + abs(target - self._wl) / self._slew()
        self._start("wave", dt, self._wl, target)

    def set_grating(self, n: int) -> None:
        if not 1 <= int(n) <= int(self.cfg.gratings.count):
            self._error = 2                       # bad parameter
            return
        self._start("grating", float(self.cfg.sim.grating_change_s), self._grating, int(n))

    def set_filter(self, n: int) -> None:
        if not self.cfg.accessories.filter_wheel:
            self._error = 6                       # accessory not present
            return
        self._start("filter", float(self.cfg.sim.filter_move_s), self._filter, int(n))

    def set_port(self, n: int) -> None:
        if not self.cfg.accessories.dual_port:
            self._error = 6
            return
        self._start("port", float(self.cfg.sim.port_move_s), self._port, int(n))

    def step(self, n: int) -> None:
        self._advance()
        target = self._wl + int(n) * self._nm_per_step()
        target = min(max(0.0, target), mech_max_nm(self._lines()))
        dt = float(self.cfg.sim.move_overhead_s) + abs(target - self._wl) / self._slew()
        self._start("wave", dt, self._wl, target)

    def set_shutter(self, open_: bool) -> None:
        self._shutter = bool(open_)

    def abort(self) -> None:
        self._advance()
        if self._move is not None and self._move[0] == "wave":
            self._move = None                     # stop where the drive is now

    def calibrate(self, nm: float) -> None:
        self._advance()
        self._wl = float(nm)
