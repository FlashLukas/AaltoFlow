"""The Generator: the small "brain" between the wire and the backend.

The SMB100A needs no control loop, so this is far simpler than clMag's
Controller -- no ramp, no PID, no state machine. Its whole job is:

  * hold the DESIRED signal (frequency, power, phase, RF on/off),
  * CLAMP every request to the configured safety limits (and announce a clamp
    as an event, so nothing silently drives the sample too hard),
  * push the accepted value to whichever backend is wired in (sim or real),
  * report a status() snapshot the service/GUI/coordinator can read.

It exposes the same shape clMag's Controller does where it matters -- start(),
shutdown(), status(), get_config()/apply_config(), and an `_on_event` hook the
service replaces to forward events over the wire -- so the networking layer is a
near-copy of clMag's.
"""

from __future__ import annotations

from dataclasses import dataclass

from .backends.base import RFSource
from .config import Config, Signal


@dataclass
class Status:
    """One snapshot of the generator, for status() and the wire."""

    rf_on: bool
    power_dBm: float
    frequency_Hz: float
    phase_deg: float
    connected: bool
    idn: str = ""


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


class Generator:
    def __init__(self, backend: RFSource, cfg: Config | None = None):
        self.backend = backend
        self.cfg = cfg or Config()
        # the desired signal starts from the config's power-on defaults
        s = self.cfg.signal
        self._freq = float(s.frequency_Hz)
        self._power = float(s.power_dBm)
        self._phase = float(s.phase_deg)
        self._rf_on = bool(s.rf_on)
        self._connected = False
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Open the backend and push the start-up signal to it."""
        self.backend.open()
        self._connected = True
        # apply defaults in a safe order: set the CW parameters first, RF last
        self.backend.set_frequency(self._freq)
        self.backend.set_power(self._power)
        self.backend.set_phase(self._phase)
        self.backend.set_output(self._rf_on)
        self._emit("info", f"connected: {self.backend.idn() or 'SMB100A'}")

    def shutdown(self) -> None:
        """RF off, disconnect. Safe to call more than once / on a crash."""
        try:
            if self._connected:
                self.backend.set_output(False)
                self._rf_on = False
        finally:
            try:
                self.backend.close()
            finally:
                self._connected = False
                self._emit("info", "disconnected")

    # ---- commands (each clamps, then pushes) -----------------------------

    def set_rf(self, on: bool) -> None:
        self._rf_on = bool(on)
        if self._connected:
            self.backend.set_output(self._rf_on)
        self._emit("info", f"RF {'ON' if self._rf_on else 'OFF'}")

    def set_power(self, dBm: float) -> None:
        lim = self.cfg.limits
        value, clamped = _clamp(float(dBm), lim.power_min_dBm, lim.power_max_dBm)
        self._power = value
        if self._connected:
            self.backend.set_power(value)
        if clamped:
            self._emit("warn", f"power clamped to {value:g} dBm "
                               f"(limit {lim.power_min_dBm:g}..{lim.power_max_dBm:g})")
        else:
            self._emit("info", f"power = {value:g} dBm")

    def set_frequency(self, hz: float) -> None:
        lim = self.cfg.limits
        value, clamped = _clamp(float(hz), lim.freq_min_Hz, lim.freq_max_Hz)
        self._freq = value
        if self._connected:
            self.backend.set_frequency(value)
        if clamped:
            self._emit("warn", f"frequency clamped to {value:g} Hz "
                               f"(limit {lim.freq_min_Hz:g}..{lim.freq_max_Hz:g})")
        else:
            self._emit("info", f"frequency = {value:g} Hz")

    def set_phase(self, deg: float) -> None:
        lim = self.cfg.limits
        value, clamped = _clamp(float(deg), lim.phase_min_deg, lim.phase_max_deg)
        self._phase = value
        if self._connected:
            self.backend.set_phase(value)
        if clamped:
            self._emit("warn", f"phase clamped to {value:g} deg "
                               f"(limit {lim.phase_min_deg:g}..{lim.phase_max_deg:g})")
        else:
            self._emit("info", f"phase = {value:g} deg")

    # ---- status ----------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. When connected we read back from the instrument (the
        source of truth); otherwise we report the desired values."""
        if self._connected:
            try:
                return Status(
                    rf_on=self.backend.read_output(),
                    power_dBm=self.backend.read_power(),
                    frequency_Hz=self.backend.read_frequency(),
                    phase_deg=self.backend.read_phase(),
                    connected=True,
                    idn=self.backend.idn(),
                )
            except Exception as exc:                 # never let status() throw
                self._emit("error", f"status read failed: {exc}")
        return Status(self._rf_on, self._power, self._freq, self._phase,
                      self._connected, "")

    # ---- settings (Settings dialog / wire use these) ---------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp the desired signal to the (possibly new) limits and push it.
        Called after set_config edits self.cfg in place."""
        self.set_frequency(self._freq)
        self.set_power(self._power)
        self.set_phase(self._phase)

    # ---- internals -------------------------------------------------------

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
