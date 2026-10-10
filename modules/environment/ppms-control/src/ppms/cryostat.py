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
from .stream import StreamRecorder

_NAN = float("nan")

#: MultiVu field statuses that mean "the magnet is at its setpoint". The
#: DynaCool is driven-only and reports "Holding (driven)" (the LabVIEW driver
#: called the same state StableDriven); "Stable" is the persistent-mode word of
#: the classic PPMS, harmless to accept.
FIELD_HOLDING = frozenset({"Holding (driven)", "Stable"})
#: MultiVu temperature statuses that mean "at the setpoint".
TEMPERATURE_STABLE = frozenset({"Stable"})
#: ... and the ones that END a temperature SWEEP (ramp_temperature): "Near"
#: comes as soon as MultiVu is close to the setpoint, before it has settled
#: to "Stable". A fly row needs the sweep to be OVER, not settled -- settling
#: is what temperature_stable (with its hold time) is for.
TEMPERATURE_ARRIVED = frozenset({"Near", "Stable"})

#: The approach a temperature SWEEP uses. fast_settle goes at the asked rate
#: to the end; no_overshoot slows down near the target, which would bend the
#: end of every fly row. VERIFY on the DynaCool that fast_settle holds the
#: rate to the end (and how far it overshoots there).
TEMPERATURE_SWEEP_APPROACH = "fast_settle"


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
    # the field SWEEP (ramp_field, fly scans): ramp_id = the newest sweep
    # started; "ramp_id >= mine and not ramping" = my sweep has arrived
    ramping: bool = False
    ramp_id: int = 0
    ramp_target_mT: float = _NAN
    ramp_rate_mT_per_s: float = _NAN
    # the temperature SWEEP (ramp_temperature, fly scans, 2026-10-10): its own
    # number and flag, so a field sweep and a temperature sweep never take
    # each other's "done" (and either may run while the other does)
    temp_ramping: bool = False
    temp_ramp_id: int = 0
    temp_ramp_target_K: float = _NAN
    temp_ramp_rate_K_per_s: float = _NAN


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
        # the field SWEEP (under _lock): MultiVu sweeps the magnet itself (a
        # HARDWARE ramp); the brain only numbers it and says when it arrived
        self._ramp_id = 0
        self._ramping = False
        self._ramp_target = _NAN
        self._ramp_rate = _NAN
        # the temperature SWEEP (under _lock): the same, for MultiVu's
        # temperature controller. Rate kept in K/s (the wire's unit).
        self._temp_ramp_id = 0
        self._temp_ramping = False
        self._temp_ramp_target = _NAN
        self._temp_ramp_rate = _NAN
        # THE STREAM (fly scans): every field AND temperature reading the poll
        # thread takes, with the time it was taken. Recording only while a
        # stream is started. ONE recorder, one stream group ("cryostat"), for
        # both: scan-core starts and drains a group once per row, so a field
        # fly that also records the temperature (or the other way round)
        # must not start and drain the same verbs twice.
        self.recorder = StreamRecorder(["field", "temperature"])
        self._last_full = -1e9               # clock of the last full poll
        # set by a sweep / a stream start: the poll loop stops its (slow)
        # wait and goes over to fast polling at once
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- lifecycle -------------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Connect, ADOPT MultiVu's current setpoints (command nothing), and
        start polling. `poll=False` is for tests that step `poll_once()`."""
        # Everything below is a QUERY. Lukas's rule (2026-09-27, every module):
        # "read the instrument state on startup, not change anything". So the
        # setpoint, the rate AND the approach MultiVu currently holds are all
        # adopted; the rate/approach in ppms.ini are only the starting values
        # used when MultiVu cannot tell us, and otherwise apply only once the
        # user sets a rate/approach explicitly (setter or set_config).
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            f_sp, f_rate, f_appr = self._adopt_setpoint(
                "field", self.backend.read_field_setpoint, FIELD_APPROACHES,
                self.backend.read_field)
            t_sp, t_rate, t_appr = self._adopt_setpoint(
                "temperature", self.backend.read_temperature_setpoint,
                TEMPERATURE_APPROACHES, self.backend.read_temperature)
        lim = self.cfg.limits
        if f_rate is not None:
            self.cfg.field.rate_mT_per_s, self.cfg.field.approach = f_rate, f_appr
            if not lim.field_rate_min_mT_per_s <= f_rate <= lim.field_rate_max_mT_per_s:
                self._emit("warn", f"MultiVu's field rate {f_rate:g} mT/s is outside the "
                                   "limits; the next field setpoint will clamp it")
        if t_rate is not None:
            self.cfg.temperature.rate_K_per_min, self.cfg.temperature.approach = t_rate, t_appr
            if not (lim.temperature_rate_min_K_per_min <= t_rate
                    <= lim.temperature_rate_max_K_per_min):
                self._emit("warn", f"MultiVu's temperature rate {t_rate:g} K/min is outside "
                                   "the limits; the next temperature setpoint will clamp it")
        with self._lock:
            self._field_sp = float(f_sp)
            self._temp_sp = float(t_sp)
            self._connected = True
        self._emit("info", f"connected: {self._idn}; adopted {f_sp:.2f} mT "
                           f"({self.cfg.field.rate_mT_per_s:g} mT/s, {self.cfg.field.approach}), "
                           f"{t_sp:.3f} K ({self.cfg.temperature.rate_K_per_min:g} K/min, "
                           f"{self.cfg.temperature.approach}) -- nothing commanded")
        self.poll_once()
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="ppms-poll", daemon=True)
            self._thread.start()

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Stop polling and disconnect. Field and temperature are LEFT AS THEY
        ARE (see the module docstring). Safe to call more than once.

        keep_outputs (the restart flag of the universal `shutdown` verb,
        2026-10-06) changes nothing here: this shutdown never touches the
        cryostat, so every stop already leaves its outputs as they are."""
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
                self._ramping = False       # a set takes over from a sweep
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
                self._temp_ramping = False  # a set takes over from a sweep
        if clamped:
            self._emit("warn", f"temperature clamped to {value:g} K "
                               f"(limit {lim.temperature_min_K:g}..{lim.temperature_max_K:g})")
        else:
            self._emit("info", f"temperature -> {value:g} K at {rate:g} K/min ({t.approach})")

    # ---- the field SWEEP (fly scans) -------------------------------------------

    def ramp_field(self, field_mT: float, rate_mT_per_s: float) -> int:
        """SWEEP the field to `field_mT` at `rate_mT_per_s`; returns its number.

        A HARDWARE ramp: MultiVu drives the magnet at that rate by itself, in
        the LINEAR approach (constant rate all the way, no overshoot and no
        oscillation at the end -- "oscillate" would demagnetise on the way in,
        which is not a sweep). The rate is this sweep's own: the field-rate
        SETTING for ordinary setpoints is left as it is. A fly scan bins by
        the MEASURED field, read by the poll thread every ramp_poll_s while
        the sweep runs. Clamped to the envelope like every setter (warned)."""
        lim = self.cfg.limits
        value, clamped = _clamp(_finite(field_mT, "field_mT"),
                                -lim.field_max_mT, lim.field_max_mT)
        rate = abs(_finite(rate_mT_per_s, "rate_mT_per_s"))
        if not rate > 0:
            raise ValueError("rate must be > 0")
        r, rclamped = _clamp(rate, lim.field_rate_min_mT_per_s, lim.field_rate_max_mT_per_s)
        with self._hw:
            self._require_connected()
            self.backend.set_field(value, r, "linear")
            with self._lock:                # setpoint, flag, sweep: ONE section
                self._field_sp = value
                self._field_gen += 1
                self._field_band.reset()
                self._ramp_id += 1
                rid = self._ramp_id
                self._ramping = True
                self._ramp_target, self._ramp_rate = value, r
        if clamped or rclamped:
            self._emit("warn", f"field sweep clamped to {value:g} mT at {r:g} mT/s")
        self._wake.set()
        self._emit("info", f"field sweep -> {value:g} mT at {r:g} mT/s (linear)")
        return rid

    def ramp_stop(self) -> bool:
        """Stop a sweep WHERE IT IS: a new setpoint at the present field, at
        the sweep's rate. True if one was running.
        VERIFY on the DynaCool that a setpoint at the present field ends a
        linear sweep smoothly (no step) -- it is how MultiVu's own Stop works
        in the classic PPMS sequence editor."""
        with self._lock:
            was, rate = self._ramping, self._ramp_rate
        if not was:
            return False
        with self._hw:
            here, _ = self.backend.read_field()
            self.backend.set_field(float(here), float(rate), "linear")
            with self._lock:
                self._field_sp = float(here)
                self._field_gen += 1
                self._field_band.reset()
                self._ramping = False
        self._emit("info", f"field sweep stopped at {here:.2f} mT")
        return True

    # ---- the temperature SWEEP (fly scans) ---------------------------------------

    def temperature_ramp_limits(self) -> tuple[float, float]:
        """(min, max) temperature sweep rate in K/s, DERIVED from the K/min
        limits of the config (one envelope, two units: nothing retyped)."""
        lim = self.cfg.limits
        lo = max(1e-9, float(lim.temperature_rate_min_K_per_min) / 60.0)
        return lo, max(lo, float(lim.temperature_rate_max_K_per_min) / 60.0)

    def ramp_temperature(self, temperature_K: float, rate_K_per_s: float) -> int:
        """SWEEP the temperature to `temperature_K` at `rate_K_per_s`; returns
        its number.

        A HARDWARE ramp, like the field's: MultiVu's temperature controller
        sweeps at that rate by itself. MultiVu takes the rate in K/min, so it
        is converted here (x 60). The approach is fast_settle (constant rate
        to the end; no_overshoot would slow down near the target and bend the
        end of the row). The rate is this sweep's own: the temperature-rate
        SETTING for ordinary setpoints is left as it is. A fly scan bins by
        the MEASURED temperature, read by the poll thread every ramp_poll_s
        while the sweep runs. The sweep has arrived when the temperature is
        within tolerance_K AND MultiVu says Near or Stable (no hold time).
        Clamped like every setter (warned)."""
        lim = self.cfg.limits
        value, clamped = _clamp(_finite(temperature_K, "temperature_K"),
                                lim.temperature_min_K, lim.temperature_max_K)
        rate = abs(_finite(rate_K_per_s, "rate_K_per_s"))
        if not rate > 0:
            raise ValueError("rate must be > 0")
        r, rclamped = _clamp(rate, *self.temperature_ramp_limits())
        with self._hw:
            self._require_connected()
            self.backend.set_temperature(value, r * 60.0, TEMPERATURE_SWEEP_APPROACH)
            with self._lock:                # setpoint, flag, sweep: ONE section
                self._temp_sp = value
                self._temp_gen += 1
                self._temp_band.reset()
                self._temp_ramp_id += 1
                rid = self._temp_ramp_id
                self._temp_ramping = True
                self._temp_ramp_target, self._temp_ramp_rate = value, r
        if clamped or rclamped:
            self._emit("warn", f"temperature sweep clamped to {value:g} K at "
                               f"{r * 60.0:g} K/min")
        self._wake.set()
        self._emit("info", f"temperature sweep -> {value:g} K at {r * 60.0:g} K/min "
                           f"({TEMPERATURE_SWEEP_APPROACH})")
        return rid

    def ramp_temperature_stop(self) -> bool:
        """Stop a temperature sweep WHERE IT IS: a new setpoint at the present
        temperature, at the sweep's rate. True if one was running.
        VERIFY on the DynaCool that a setpoint at the present temperature ends
        a sweep without the controller overshooting back and forth."""
        with self._lock:
            was, rate = self._temp_ramping, self._temp_ramp_rate
        if not was:
            return False
        with self._hw:
            here, _ = self.backend.read_temperature()
            self.backend.set_temperature(float(here), float(rate) * 60.0,
                                         TEMPERATURE_SWEEP_APPROACH)
            with self._lock:
                self._temp_sp = float(here)
                self._temp_gen += 1
                self._temp_band.reset()
                self._temp_ramping = False
        self._emit("info", f"temperature sweep stopped at {here:.3f} K")
        return True

    # the stream verbs: the poll thread's field and temperature readings
    def stream_start(self) -> int:
        sid = self.recorder.start()
        self._wake.set()
        return sid

    def stream_read(self) -> dict:
        return self.recorder.read()

    def stream_stop(self) -> dict:
        return self.recorder.stop()

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
                ramping=self._ramping,
                ramp_id=self._ramp_id,
                ramp_target_mT=self._ramp_target,
                ramp_rate_mT_per_s=self._ramp_rate,
                temp_ramping=self._temp_ramping,
                temp_ramp_id=self._temp_ramp_id,
                temp_ramp_target_K=self._temp_ramp_target,
                temp_ramp_rate_K_per_s=self._temp_ramp_rate,
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
            hw = self.cfg.hardware
            # during a sweep -- or while a fly scan records the stream -- field
            # and temperature fast, the chamber at its usual pace
            fast = self._ramping or self._temp_ramping or self.recorder.running
            full = (not fast or t0 - self._last_full >= float(hw.poll_s))
            self.poll_once(full=full)
            period = float(hw.ramp_poll_s) if fast else float(hw.poll_s)
            wait = max(0.01, period - (self._clock() - t0))
            # deadline + time.sleep, not Event.wait(timeout): on Windows a
            # timed wait sleeps at least a 15.6 ms tick (gotcha #34)
            end = time.monotonic() + wait
            self._wake.clear()
            while (not self._stop.is_set() and not self._wake.is_set()
                   and time.monotonic() < end):
                time.sleep(min(0.01, max(0.0, end - time.monotonic())))

    def poll_once(self, full: bool = True) -> None:
        """Read field and temperature (and, when `full`, the chamber) once and
        update the flags. A fast poll keeps the last chamber state."""
        with self._lock:
            fgen, tgen = self._field_gen, self._temp_gen
            chamber = self._chamber
        t0 = self._clock()
        try:
            with self._hw:
                tw0 = time.time()
                field, fstat = self.backend.read_field()
                temp, tstat = self.backend.read_temperature()
                # the readings' time: the middle of the two calls (wall clock,
                # so a coordinator on another PC can line it up). Both readings
                # share it: two local calls of a few ms, far inside a pixel.
                self.recorder.append(0.5 * (tw0 + time.time()), (field, temp))
                if full:
                    chamber = self.backend.read_chamber()
                    self._last_full = t0
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
                # a sweep has ARRIVED when MultiVu holds at its target (no
                # hold time: the fly row ends here; field_stable, with its
                # hold time, is for a scan that wants to measure AT the field)
                if self._ramping and ok:
                    self._ramping = False
            if tgen == self._temp_gen:
                near = abs(self._temp - self._temp_sp) <= float(t.tolerance_K)
                ok = near and self._temp_status in TEMPERATURE_STABLE
                self._temp_band.update(ok, now, float(t.stable_time_s))
                # a temperature sweep has ARRIVED when it is within tolerance
                # and MultiVu says Near or Stable -- no hold time (see
                # TEMPERATURE_ARRIVED); the tgen guard above keeps a reading
                # from before the sweep's command from ending it
                if (self._temp_ramping and near
                        and self._temp_status in TEMPERATURE_ARRIVED):
                    self._temp_ramping = False
        if recovered:
            self._emit("info", "MultiVu readings recovered")

    # ---- internals -------------------------------------------------------------------

    def _adopt_setpoint(self, what, read_setpoint, approaches, read_measured):
        """Ask MultiVu for (setpoint, rate, approach) of one loop, for start().

        Returns (setpoint, rate, approach), or (measured value, None, None) when
        the answer cannot be trusted -- then the measured value stands in for the
        setpoint and the config's rate/approach stay as they are.

        Why the approach name is checked: when MultiVu reports an error,
        MultiPyVu 3.6.1 does not raise -- it returns setpoint 0.0, rate 0.0 and
        the ERROR TEXT in place of the approach name. Adopting that would show
        "setpoint 0 K" on a cryostat sitting at 300 K. An approach name we do
        not know is therefore treated as a failed read."""
        try:
            sp, rate, approach = read_setpoint()
            sp, rate, approach = float(sp), float(rate), str(approach)
            if approach not in approaches or not (math.isfinite(sp) and math.isfinite(rate)):
                raise ValueError(f"MultiVu answered {sp!r}, {rate!r}, {approach!r}")
            return sp, rate, approach
        except Exception as exc:
            self._emit("warn", f"could not read MultiVu's {what} setpoint ({exc}); "
                               "adopting the measured value, rate/approach from the config")
            return float(read_measured()[0]), None, None

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("not connected to MultiVu")

    @staticmethod
    def _check_approach(name: str, allowed, what: str) -> None:
        if name not in allowed:
            raise ValueError(f"{what} must be one of {', '.join(allowed)}; got {name!r}")

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
