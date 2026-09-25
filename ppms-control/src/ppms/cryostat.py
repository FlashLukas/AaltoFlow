"""The Cryostat: the brain between the wire and the backend (simulated or MultiVu).

MultiVu runs the DynaCool's own control loops -- the magnet supply and the
temperature controller -- so this is a SET-AND-FORGET brain with one extra job
that matters a great deal for a scan: saying honestly when a setpoint has been
REACHED. Its jobs:

  * hold the desired field and temperature, CLAMPED to the safety envelope (a
    clamp is announced as a warn event);
  * push them to MultiVu with the configured rate and approach;
  * poll field, temperature and chamber, and decide `field_stable` /
    `temperature_stable` with the old program's rule, made explicit:
        field       |set - measured| <= tolerance_mT  AND  MultiVu says it holds
        temperature |set - measured| <= tolerance_K   AND  MultiVu says Stable
    both held continuously for `stable_time_s`. MultiVu's own word alone is not
    enough (a status can lag a new setpoint); the numbers alone are not enough
    either (a field passing through the band on its way elsewhere is inside it).

What it deliberately does NOT do: touch the field or the temperature at start
or at shutdown. A Kepco electromagnet is ramped to zero when its service dies,
because a coil left driven heats up; a DynaCool is a self-protecting system,
and a sample left at 5 T and 2 K on purpose should still be there after a
restart. At start the brain ADOPTS whatever MultiVu is already set to.

Threads and locks (the rules the other modules learned the hard way):

  * ONE polling thread reads the hardware. `status()` only copies what that
    thread stored and never touches MultiVu, so a slow COM call cannot stall
    the status publisher, and a lost link shows as `hw_error` instead of a
    healthy-looking panel full of old numbers.
  * EVERY backend call runs under `_hw` (an RLock): the command thread and the
    polling thread would otherwise talk to MultiVu at once.
  * A setter clears the stable flag and bumps a command GENERATION in the SAME
    critical section in which it stores the new setpoint (gotcha #1 and the
    adopt-then-flag rule, docs/DEVELOPER_NOTES.md section 8). So no status frame
    can ever show the new setpoint next to the old point's `field_stable = True`,
    and a poll that started before the command cannot declare the new point
    reached with readings taken before it was even sent.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from .backends.base import CryostatBackend
from .config import FIELD_APPROACHES, TEMPERATURE_APPROACHES, Config

_NAN = float("nan")

#: MultiVu field statuses that mean "the magnet is at its setpoint". The
#: DynaCool is driven-only and reports "Holding (driven)" (the LabVIEW driver
#: called the same state StableDriven); "Stable" is the persistent-mode word of
#: the classic PPMS, harmless to accept.
FIELD_HOLDING = frozenset({"Holding (driven)", "Stable"})
#: MultiVu temperature statuses that mean "at the setpoint".
TEMPERATURE_STABLE = frozenset({"Stable"})


@dataclass
class Status:
    """One snapshot of the cryostat, for status() and the wire."""

    connected: bool
    simulated: bool = True
    idn: str = ""
    hw_error: str = ""
    # field
    setpoint_field_mT: float = _NAN
    measured_field_mT: float = _NAN
    field_error_mT: float = _NAN
    field_status: str = ""
    field_stable: bool = False
    field_rate_mT_per_s: float = _NAN
    field_approach: str = ""
    # temperature
    setpoint_temperature_K: float = _NAN
    temperature_K: float = _NAN
    temperature_error_K: float = _NAN
    temperature_status: str = ""
    temperature_stable: bool = False
    temperature_rate_K_per_min: float = _NAN
    temperature_approach: str = ""
    # chamber
    chamber: str = ""
    # housekeeping
    readings: int = 0
    poll_ms: float = _NAN


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def _finite(value, what: str) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return v


class _Band:
    """"Inside the band, and MultiVu agrees" -- held continuously for a time.

    `since` is the clock time at which the condition last became true; it is
    reset (None) the moment the condition fails or a new setpoint arrives."""

    def __init__(self):
        self.since = None
        self.stable = False

    def reset(self):
        self.since = None
        self.stable = False

    def update(self, ok: bool, now: float, hold_s: float) -> None:
        if not ok:
            self.reset()
            return
        if self.since is None:
            self.since = now
        self.stable = (now - self.since) >= hold_s


class Cryostat:
    def __init__(self, backend: CryostatBackend, cfg: Config | None = None,
                 clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards setpoints, readings, flags
        self._connected = False
        self._idn = ""
        self._hw_error = ""
        # what we ask for (under _lock)
        self._field_sp = _NAN
        self._temp_sp = _NAN
        self._field_gen = 0                 # bumped by every field command
        self._temp_gen = 0
        # what the poll thread last read (under _lock)
        self._field = _NAN
        self._field_status = ""
        self._temp = _NAN
        self._temp_status = ""
        self._chamber = ""
        self._readings = 0
        self._poll_ms = _NAN
        self._field_band = _Band()
        self._temp_band = _Band()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- lifecycle -------------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Connect, ADOPT MultiVu's current setpoints (command nothing), and
        start polling. `poll=False` is for tests that step `poll_once()`."""
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            try:
                f_sp, _, _ = self.backend.read_field_setpoint()
                t_sp, _, _ = self.backend.read_temperature_setpoint()
            except Exception as exc:            # adopt the readings instead
                self._emit("warn", f"could not read MultiVu's setpoints ({exc}); "
                                   "adopting the measured values")
                f_sp = self.backend.read_field()[0]
                t_sp = self.backend.read_temperature()[0]
        with self._lock:
            self._field_sp = float(f_sp)
            self._temp_sp = float(t_sp)
            self._connected = True
        self._emit("info", f"connected: {self._idn}; adopted {f_sp:.2f} mT, {t_sp:.3f} K "
                           "(nothing commanded)")
        self.poll_once()
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="ppms-poll", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop polling and disconnect. Field and temperature are LEFT AS THEY
        ARE (see the module docstring). Safe to call more than once."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5.0)
        self._thread = None
        try:
            with self._hw:
                self.backend.close()
        finally:
            with self._lock:
                was = self._connected
                self._connected = False
            if was:
                self._emit("info", "disconnected (field and temperature left as they are)")

    # ---- commands (each clamps, then pushes) -------------------------------------

    def set_field(self, field_mT: float) -> None:
        """Drive to `field_mT` at the configured rate and approach."""
        lim, f = self.cfg.limits, self.cfg.field
        value, clamped = _clamp(_finite(field_mT, "field_mT"),
                                -lim.field_max_mT, lim.field_max_mT)
        self._check_approach(f.approach, FIELD_APPROACHES, "field approach")
        rate, _ = _clamp(float(f.rate_mT_per_s), lim.field_rate_min_mT_per_s,
                         lim.field_rate_max_mT_per_s)
        with self._hw:
            self._require_connected()
            self.backend.set_field(value, rate, f.approach)
            with self._lock:                # setpoint + flag reset: ONE section
                self._field_sp = value
                self._field_gen += 1
                self._field_band.reset()
        if clamped:
            self._emit("warn", f"field clamped to {value:g} mT (limit +-{lim.field_max_mT:g})")
        else:
            self._emit("info", f"field -> {value:g} mT at {rate:g} mT/s ({f.approach})")

    def set_temperature(self, temperature_K: float) -> None:
        """Drive to `temperature_K` at the configured rate and approach."""
        lim, t = self.cfg.limits, self.cfg.temperature
        value, clamped = _clamp(_finite(temperature_K, "temperature_K"),
                                lim.temperature_min_K, lim.temperature_max_K)
        self._check_approach(t.approach, TEMPERATURE_APPROACHES, "temperature approach")
        rate, _ = _clamp(float(t.rate_K_per_min), lim.temperature_rate_min_K_per_min,
                         lim.temperature_rate_max_K_per_min)
        with self._hw:
            self._require_connected()
            self.backend.set_temperature(value, rate, t.approach)
            with self._lock:
                self._temp_sp = value
                self._temp_gen += 1
                self._temp_band.reset()
        if clamped:
            self._emit("warn", f"temperature clamped to {value:g} K "
                               f"(limit {lim.temperature_min_K:g}..{lim.temperature_max_K:g})")
        else:
            self._emit("info", f"temperature -> {value:g} K at {rate:g} K/min ({t.approach})")

    # Rates and approaches are SETTINGS: MultiVu only takes them together with
    # a setpoint, so they apply from the next set_field / set_temperature. They
    # are echoed in status at once, which is what a scan waits for.

    def set_field_rate(self, rate_mT_per_s: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(rate_mT_per_s, "rate_mT_per_s"),
                            lim.field_rate_min_mT_per_s, lim.field_rate_max_mT_per_s)
        self.cfg.field.rate_mT_per_s = v
        self._emit("warn" if clamped else "info",
                   f"field rate {'clamped to' if clamped else '='} {v:g} mT/s "
                   "(applies from the next field setpoint)")

    def set_field_approach(self, approach: str) -> None:
        self._check_approach(approach, FIELD_APPROACHES, "field approach")
        self.cfg.field.approach = approach
        self._emit("info", f"field approach = {approach} (applies from the next setpoint)")

    def set_temperature_rate(self, rate_K_per_min: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(rate_K_per_min, "rate_K_per_min"),
                            lim.temperature_rate_min_K_per_min,
                            lim.temperature_rate_max_K_per_min)
        self.cfg.temperature.rate_K_per_min = v
        self._emit("warn" if clamped else "info",
                   f"temperature rate {'clamped to' if clamped else '='} {v:g} K/min "
                   "(applies from the next temperature setpoint)")

    def set_temperature_approach(self, approach: str) -> None:
        self._check_approach(approach, TEMPERATURE_APPROACHES, "temperature approach")
        self.cfg.temperature.approach = approach
        self._emit("info", f"temperature approach = {approach} "
                           "(applies from the next setpoint)")

    # ---- status ------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot of what the poll thread last read. Never touches MultiVu."""
        f, t = self.cfg.field, self.cfg.temperature
        with self._lock:
            return Status(
                connected=self._connected,
                simulated=bool(getattr(self.backend, "simulated", True)),
                idn=self._idn,
                hw_error=self._hw_error,
                setpoint_field_mT=self._field_sp,
                measured_field_mT=self._field,
                field_error_mT=self._field - self._field_sp,
                field_status=self._field_status,
                field_stable=self._field_band.stable,
                field_rate_mT_per_s=float(f.rate_mT_per_s),
                field_approach=f.approach,
                setpoint_temperature_K=self._temp_sp,
                temperature_K=self._temp,
                temperature_error_K=self._temp - self._temp_sp,
                temperature_status=self._temp_status,
                temperature_stable=self._temp_band.stable,
                temperature_rate_K_per_min=float(t.rate_K_per_min),
                temperature_approach=t.approach,
                chamber=self._chamber,
                readings=self._readings,
                poll_ms=self._poll_ms,
            )

    # ---- settings (Settings dialog / wire use these) -------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-check the settings after set_config edited self.cfg in place.
        Nothing is commanded: a changed limit or rate applies from the next
        setpoint, and a stored setpoint outside a NEW envelope is announced
        rather than silently re-driven (re-driving a magnet is not a settings
        side effect anyone expects)."""
        lim = self.cfg.limits
        f, t = self.cfg.field, self.cfg.temperature
        f.rate_mT_per_s = _clamp(float(f.rate_mT_per_s), lim.field_rate_min_mT_per_s,
                                 lim.field_rate_max_mT_per_s)[0]
        t.rate_K_per_min = _clamp(float(t.rate_K_per_min), lim.temperature_rate_min_K_per_min,
                                  lim.temperature_rate_max_K_per_min)[0]
        if f.approach not in FIELD_APPROACHES:
            f.approach = FIELD_APPROACHES[0]
        if t.approach not in TEMPERATURE_APPROACHES:
            t.approach = TEMPERATURE_APPROACHES[0]
        with self._lock:
            fsp, tsp = self._field_sp, self._temp_sp
        if math.isfinite(fsp) and abs(fsp) > lim.field_max_mT:
            self._emit("warn", f"field setpoint {fsp:g} mT is outside the new limit "
                               f"+-{lim.field_max_mT:g} mT (not changed)")
        if math.isfinite(tsp) and not lim.temperature_min_K <= tsp <= lim.temperature_max_K:
            self._emit("warn", f"temperature setpoint {tsp:g} K is outside the new limits "
                               "(not changed)")

    # ---- polling -------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            t0 = self._clock()
            self.poll_once()
            wait = max(0.05, float(self.cfg.hardware.poll_s) - (self._clock() - t0))
            self._stop.wait(wait)

    def poll_once(self) -> None:
        """Read field, temperature and chamber once and update the flags."""
        with self._lock:
            fgen, tgen = self._field_gen, self._temp_gen
        t0 = self._clock()
        try:
            with self._hw:
                field, fstat = self.backend.read_field()
                temp, tstat = self.backend.read_temperature()
                chamber = self.backend.read_chamber()
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            with self._lock:
                new = msg != self._hw_error
                self._hw_error = msg
                # nothing read -> nothing can be claimed as reached
                self._field_band.reset()
                self._temp_band.reset()
            if new:
                self._emit("error", f"MultiVu read failed: {msg}")
            return
        now = self._clock()
        f, t = self.cfg.field, self.cfg.temperature
        with self._lock:
            recovered = bool(self._hw_error)
            self._hw_error = ""
            self._field, self._field_status = float(field), str(fstat)
            self._temp, self._temp_status = float(temp), str(tstat)
            self._chamber = str(chamber)
            self._readings += 1
            self._poll_ms = (now - t0) * 1000.0
            # A command that arrived WHILE we were reading makes these readings
            # older than the setpoint: they must not count towards it.
            if fgen == self._field_gen:
                ok = (abs(self._field - self._field_sp) <= float(f.tolerance_mT)
                      and self._field_status in FIELD_HOLDING)
                self._field_band.update(ok, now, float(f.stable_time_s))
            if tgen == self._temp_gen:
                ok = (abs(self._temp - self._temp_sp) <= float(t.tolerance_K)
                      and self._temp_status in TEMPERATURE_STABLE)
                self._temp_band.update(ok, now, float(t.stable_time_s))
        if recovered:
            self._emit("info", "MultiVu readings recovered")

    # ---- internals -------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("not connected to MultiVu")

    @staticmethod
    def _check_approach(name: str, allowed, what: str) -> None:
        if name not in allowed:
            raise ValueError(f"{what} must be one of {', '.join(allowed)}; got {name!r}")

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
