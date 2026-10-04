"""The Generator: the small "brain" between the wire and the backend.

The SMB100A needs no control loop, so this is far simpler than clMag's
Controller -- no ramp, no PID, no state machine. Its whole job is:

  * hold the DESIRED signal (frequency, power, phase, RF on/off),
  * CLAMP every request to the configured safety limits (and announce a clamp
    as an event, so nothing silently drives the sample too hard),
  * push the accepted value to whichever backend is wired in (sim or real),
  * report a status() snapshot the service/GUI/coordinator can read.

ADOPT ON START (Lukas's rule, 2026-09-27: "all modules should read the
instrument state on startup, not to change anything"). start() only READS the
generator -- RF on/off, frequency, level, phase -- and takes those as the
desired signal. Nothing is written at start, so a generator that is already
feeding a running experiment keeps doing exactly that when the service is
(re)started. The [signal] group of the config is therefore NOT a power-on
state any more: it is a set of defaults that is applied only when the user
changes it explicitly (Settings dialog / set_config), see apply_config().

It exposes the same shape clMag's Controller does where it matters -- start(),
shutdown(), status(), get_config()/apply_config(), and an `_on_event` hook the
service replaces to forward events over the wire -- so the networking layer is a
near-copy of clMag's.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

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
        # Placeholders until start() adopts the instrument's own values: they
        # are what status() reports while not connected, nothing more.
        s = self.cfg.signal
        self._freq = float(s.frequency_Hz)
        self._power = float(s.power_dBm)
        self._phase = float(s.phase_deg)
        self._rf_on = bool(s.rf_on)
        self._connected = False
        # The [signal] group as it was last seen, so apply_config() can tell
        # which default the user actually CHANGED (only those get applied).
        self._signal_seen = asdict(s)
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Open the backend and ADOPT what the generator is doing right now.

        Queries only: the RF output is NOT switched off, and no frequency,
        level or phase is written. A value outside our configured limits is
        left alone on the instrument (we only warn); the limits apply to the
        next thing the user sets.
        """
        self.backend.open()
        self._connected = True
        readers = (("_rf_on", self.backend.read_output, bool),
                   ("_freq", self.backend.read_frequency, float),
                   ("_power", self.backend.read_power, float),
                   ("_phase", self.backend.read_phase, float))
        for attr, read, cast in readers:
            try:
                setattr(self, attr, cast(read()))
            except Exception as exc:           # keep the placeholder, say so
                self._emit("warn", f"could not read {attr.lstrip('_')} at start "
                                   f"({exc}); showing the config default")
        self._signal_seen = asdict(self.cfg.signal)
        self._emit("info", f"connected: {self.backend.idn() or 'SMB100A'}")
        self._emit("info", f"adopted from the instrument: RF "
                           f"{'ON' if self._rf_on else 'off'}, {self._freq:g} Hz, "
                           f"{self._power:g} dBm, {self._phase:g} deg (nothing written)")
        lim = self.cfg.limits
        for name, value, lo, hi, unit in (
                ("frequency", self._freq, lim.freq_min_Hz, lim.freq_max_Hz, "Hz"),
                ("power", self._power, lim.power_min_dBm, lim.power_max_dBm, "dBm"),
                ("phase", self._phase, lim.phase_min_deg, lim.phase_max_deg, "deg")):
            if not lo <= value <= hi:
                self._emit("warn", f"instrument {name} {value:g} {unit} is outside the "
                                   f"limits {lo:g}..{hi:g}; left as is (not clamped)")

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

    def rf_off(self) -> None:
        """RF off. Same as set_rf(False); a name of its own because over the
        wire it is the SAFETY verb a viewer may always send (net/service.py,
        control)."""
        self.set_rf(False)

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
        """Called after set_config / the Settings dialog edited self.cfg in place.

        Two jobs, both triggered by an explicit user action:
          * a [signal] default the user CHANGED (compared with the last copy we
            saw) is applied now -- frequency, power and phase. `rf_on` is never
            switched from here: RF is switched only by set_rf, on purpose, so
            saving the Settings dialog can never key the output.
          * a value that no longer fits (possibly new) limits is re-clamped.
        Anything unchanged and inside the limits is NOT re-sent, so e.g. a
        theme change never writes to the instrument.
        """
        new = asdict(self.cfg.signal)
        old = self._signal_seen
        self._signal_seen = new
        lim = self.cfg.limits
        for key, current, setter, lo, hi in (
                ("frequency_Hz", self._freq, self.set_frequency,
                 lim.freq_min_Hz, lim.freq_max_Hz),
                ("power_dBm", self._power, self.set_power,
                 lim.power_min_dBm, lim.power_max_dBm),
                ("phase_deg", self._phase, self.set_phase,
                 lim.phase_min_deg, lim.phase_max_deg)):
            if new.get(key) != old.get(key):
                setter(float(new[key]))          # the user changed this default
            elif not lo <= current <= hi:
                setter(current)                  # re-clamp to the new limits

    # ---- internals -------------------------------------------------------

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
