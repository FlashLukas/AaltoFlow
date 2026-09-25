"""The Controller: the brain of the 2-axis vector magnet.

WHAT IT DOES. It holds a field setpoint (magnitude + angle, or Bx + By), runs a
PI loop per axis in calibrated millitesla CONTINUOUSLY while the output is
energized, reports whether the field has been stable, and enforces the
interlocks (cooling water, optional temperature).

THE SETPOINT MODEL. Four numbers are stored together, all at once, under one
lock: field_mT (signed), angle_deg, bx_mT, by_mT.
  * set_field / set_angle / zero store the POLAR pair as given and derive
    Bx = B cos a, By = B sin a;
  * set_vector / set_bx / set_by store the CARTESIAN pair as given and derive
    B = hypot, a = atan2.
Whatever was commanded is stored EXACTLY -- never rounded, never normalised
(angle 370 stays 370). That matters for the scan engine: it waits until the
status shows the value it sent (tolerance 1e-6) before it trusts field_stable,
and a setpoint the service quietly tidied up would never be "adopted".

THREADS (the hf2/pm16 rules):
  * ONE control thread owns the hardware: every read and write happens inside
    tick(). Setters only change numbers under `_lock`; the next tick acts on
    them. So a command never waits for the DAQ, and two threads never talk to
    the card at once.
  * status() copies what tick() stored and NEVER touches the hardware.
  * Live control state is in brain attributes that the tick COPIES into the
    snapshot; setters never edit a snapshot (docs/DEVELOPER_NOTES.md gotcha #1).

STATES
  OFF         output de-energized (AO 0 V, enable line False)
  REGULATING  energized, PI running, not (yet) stable
  STABLE      energized, PI running, every axis inside tolerance for stable_time
  RAMP_DOWN   output switched off: AO slewing to 0 V, then enable False -> OFF
  FAULT       an interlock tripped: setpoint zeroed, AO slewing to 0 V, then
              enable False; stays FAULT until the cause is gone AND clear_fault()

A new setpoint resets the stability timer in the same critical section that
stores it, so no status frame can show the new setpoint together with the old
point's field_stable=True.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .backends.base import VectorMagnetBackend
from .config import Config
from .pid import AxisPI, ramp_toward_zero

_NAN = float("nan")

OFF, REGULATING, STABLE, RAMP_DOWN, FAULT = "OFF", "REGULATING", "STABLE", "RAMP_DOWN", "FAULT"
STATES = [OFF, REGULATING, STABLE, RAMP_DOWN, FAULT]


class Refused(Exception):
    """A command the magnet will not carry out now (e.g. a setpoint during a
    FAULT). The service turns it into {"ok": false, "error": <message>}."""


class WaterInterlockError(Refused):
    """Raised by start() when the cooling water is off and not bypassed."""


@dataclass
class Status:
    """One snapshot, with EXACTLY the status keys of the suite contract."""

    state: str = OFF
    energized: bool = False
    setpoint_field_mT: float = 0.0
    setpoint_angle_deg: float = 0.0
    setpoint_bx_mT: float = 0.0
    setpoint_by_mT: float = 0.0
    measured_bx_mT: float = _NAN
    measured_by_mT: float = _NAN
    measured_field_mT: float = _NAN        # component along the setpoint direction
    measured_magnitude_mT: float = _NAN
    measured_angle_deg: float = _NAN
    error_mT: float = _NAN
    field_stable: bool = False
    output_V: list = field(default_factory=lambda: [0.0, 0.0])
    hall_V: list = field(default_factory=lambda: [_NAN, _NAN])
    temp_C: list = field(default_factory=lambda: [_NAN, _NAN])
    water_ok: bool = False
    water_bypass: bool = False
    temp_monitor: bool = False
    fault: str = ""
    hw_error: str = ""                      # extra: last failed hardware call


def _finite(value, what: str) -> float:
    """float(value), refusing NaN/inf: NaN passes every < and > clamp and would
    otherwise go straight to the coils."""
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return v


class Controller:
    def __init__(self, backend: VectorMagnetBackend, cfg: Config | None = None, *,
                 clock=time.monotonic, sleep=time.sleep):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()

        # ---- setpoint (the four numbers change together, under _lock) ----
        self._sp_field = 0.0
        self._sp_angle = 0.0
        self._sp_bx = 0.0
        self._sp_by = 0.0

        # ---- written by the control thread ----
        self._state = OFF
        self._enable_want = False     # what the loop should put on the enable line
        self._enable_hw = False       # what the enable line actually is
        self._pi = (AxisPI(), AxisPI())
        self._bx = self._by = _NAN
        self._hall = [_NAN, _NAN]
        self._temps = [_NAN, _NAN]
        self._water = False
        self._stable = False
        self._stable_since: float | None = None
        self._fault = ""
        self._hw_error = ""
        self._last_err_emit = -1e9
        self._last_tick: float | None = None

        self._opened = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # =================================================================== lifecycle

    def start(self, run_thread: bool = True) -> None:
        """Open the hardware, check the water, energize if configured, run the loop.

        Raises WaterInterlockError (after closing the hardware again) when the
        water is off and not bypassed -- run_service.py turns that into a clear
        message and exit code 3.

        `run_thread=False` leaves the loop to the caller (tests call tick()).
        """
        self._sanitise_config()
        self.backend.open()
        self._opened = True
        try:
            # Safe state first, whatever the card was left at.
            self.backend.write_ao(0.0, 0.0)
            self.backend.set_enable(False)
            water = bool(self.backend.read_water())
        except Exception:
            self._close_backend()
            raise
        if not water and not self.cfg.interlock.water_bypass:
            self._close_backend()
            raise WaterInterlockError(
                "cooling water is OFF (flow switch reads False). Start the water, "
                "or run with --bypass-water (interlock.water_bypass = True).")
        with self._lock:
            self._water = water
            if self.cfg.control.energize_on_start:
                self._energize_locked()
        if not water:
            self._emit("warn", "water interlock BYPASSED and the water is off")
        self._emit("info", "magnet started" + (", output energized at 0 mT"
                                               if self.cfg.control.energize_on_start else
                                               ", output off"))
        if run_thread:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="mag2d-loop", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Ramp the output to 0 V at the slew rate, disable, close. Idempotent.

        The loop thread is stopped FIRST, so from here on this (calling) thread
        is the only one touching the hardware -- and it keeps calling tick(),
        which does the ramp exactly as it would in RAMP_DOWN.
        """
        if not self._opened:
            return
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        self._thread = None
        try:
            with self._lock:
                if self._state in (REGULATING, STABLE):
                    self._state = RAMP_DOWN
                    self._stable = False
                    self._stable_since = None
            c = self.cfg.control
            worst = max(abs(p.output) for p in self._pi)
            deadline = self._clock() + worst / max(c.slew_V_per_s, 1e-3) + 3.0
            dt = 1.0 / max(1.0, c.loop_hz)
            while self._clock() < deadline:
                with self._lock:
                    done = not self._enable_hw and not self._enable_want
                if done:
                    break
                self.tick()
                self._sleep(dt)
            else:
                self._emit("error", "shutdown ramp did not finish in time; forcing 0 V")
        finally:
            try:
                self.backend.write_ao(0.0, 0.0)
                self.backend.set_enable(False)
            except Exception:
                pass
            with self._lock:
                self._enable_hw = self._enable_want = False
                for p in self._pi:
                    p.reset(0.0)
                if self._state != FAULT:
                    self._state = OFF
            self._close_backend()
            self._emit("info", "magnet shut down (output ramped to 0 V, disabled)")

    def _close_backend(self) -> None:
        try:
            self.backend.close()
        finally:
            self._opened = False

    # ==================================================================== setpoints

    def set_field(self, field_mT: float, angle_deg: float | None = None) -> None:
        """Signed field magnitude; the angle is kept when not given."""
        b = _finite(field_mT, "field_mT")
        a_in = None if angle_deg is None else _finite(angle_deg, "angle_deg")
        with self._lock:
            self._refuse_in_fault("set_field")
            a = self._sp_angle if a_in is None else a_in
            b, warn_b = self._clamp_field(b)
            a, warn_a = self._clamp_angle(a)
            self._apply_polar_locked(b, a)
        self._report(warn_b, warn_a, f"field -> {b:g} mT at {a:g} deg")

    def set_angle(self, angle_deg: float) -> None:
        """Rotate: keeps the (signed) field magnitude."""
        a = _finite(angle_deg, "angle_deg")
        with self._lock:
            self._refuse_in_fault("set_angle")
            a, warn_a = self._clamp_angle(a)
            b = self._sp_field
            self._apply_polar_locked(b, a)
        self._report(None, warn_a, f"angle -> {a:g} deg (field {b:g} mT)")

    def set_vector(self, bx_mT: float, by_mT: float) -> None:
        bx = _finite(bx_mT, "bx_mT")
        by = _finite(by_mT, "by_mT")
        with self._lock:
            self._refuse_in_fault("set_vector")
            bx, by, warn = self._apply_vector_locked(bx, by)
        self._report(warn, None, f"vector -> Bx {bx:g} mT, By {by:g} mT")

    def set_bx(self, bx_mT: float) -> None:
        """Set Bx, keep the By setpoint."""
        bx = _finite(bx_mT, "bx_mT")
        with self._lock:
            self._refuse_in_fault("set_bx")
            bx, by, warn = self._apply_vector_locked(bx, self._sp_by)
        self._report(warn, None, f"Bx -> {bx:g} mT (By {by:g} mT)")

    def set_by(self, by_mT: float) -> None:
        """Set By, keep the Bx setpoint."""
        by = _finite(by_mT, "by_mT")
        with self._lock:
            self._refuse_in_fault("set_by")
            bx, by, warn = self._apply_vector_locked(self._sp_bx, by)
        self._report(warn, None, f"By -> {by:g} mT (Bx {bx:g} mT)")

    def zero(self) -> None:
        """Field setpoint 0 mT, angle kept. Allowed during a FAULT (it only lowers)."""
        with self._lock:
            self._apply_polar_locked(0.0, self._sp_angle)
        self._emit("info", "field -> 0 mT")

    # ==================================================================== output

    def set_output(self, enabled: bool) -> None:
        """Energize (the loop starts from the present output, no kick) or
        de-energize (ramp to 0 V at the slew rate, then enable off)."""
        on = bool(enabled)
        msg = ""
        with self._lock:
            if on:
                self._refuse_in_fault("set_output")
                if self._state in (OFF, RAMP_DOWN):
                    self._energize_locked()
                    msg = "output ON -- regulating"
            elif self._state in (REGULATING, STABLE):
                self._state = RAMP_DOWN
                self._stable = False
                self._stable_since = None
                msg = "output OFF -- ramping to 0 V"
        if msg:
            self._emit("info", msg)

    def set_water_bypass(self, enabled: bool) -> None:
        self.cfg.interlock.water_bypass = bool(enabled)
        if enabled:
            self._emit("warn", "WATER INTERLOCK BYPASSED -- the coils are not protected "
                               "against running without cooling")
        else:
            self._emit("info", "water interlock active")

    def clear_fault(self) -> None:
        with self._lock:
            if self._state != FAULT:
                return
            reason = self._interlock_reason_locked()
            if not reason and self._hw_error:
                reason = f"the hardware is still failing ({self._hw_error})"
            if reason:
                raise Refused(f"cannot clear the fault: {reason}")
            old = self._fault
            self._fault = ""
            # still winding down -> finish the ramp; otherwise simply off
            self._state = RAMP_DOWN if self._enable_hw else OFF
        self._emit("info", f"fault cleared ({old}); output stays off until switched on")

    # ================================================================== reporting

    def status(self) -> Status:
        """A snapshot. Never touches the hardware."""
        with self._lock:
            bx, by = self._bx, self._by
            a = math.radians(self._sp_angle)
            if math.isfinite(bx) and math.isfinite(by):
                along = bx * math.cos(a) + by * math.sin(a)
                mag = math.hypot(bx, by)
                ang = math.degrees(math.atan2(by, bx))
                err = math.hypot(self._sp_bx - bx, self._sp_by - by)
            else:
                along = mag = ang = err = _NAN
            ilk = self.cfg.interlock
            return Status(
                state=self._state, energized=self._enable_hw,
                setpoint_field_mT=self._sp_field, setpoint_angle_deg=self._sp_angle,
                setpoint_bx_mT=self._sp_bx, setpoint_by_mT=self._sp_by,
                measured_bx_mT=bx, measured_by_mT=by,
                measured_field_mT=along, measured_magnitude_mT=mag,
                measured_angle_deg=ang, error_mT=err,
                field_stable=self._stable and self._enable_hw and self._state == STABLE,
                output_V=[self._pi[0].output, self._pi[1].output],
                hall_V=list(self._hall), temp_C=list(self._temps),
                water_ok=self._water, water_bypass=bool(ilk.water_bypass),
                temp_monitor=bool(ilk.temp_monitor),
                fault=self._fault, hw_error=self._hw_error,
            )

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-check cfg after it was edited in place (Settings, set_config).

        The PI reads its gains from cfg on every tick, so gains need nothing
        here. Limits do: a setpoint outside a NEW, narrower envelope is clamped.
        """
        self._sanitise_config()
        with self._lock:
            b, wb = self._clamp_field(self._sp_field)
            a, wa = self._clamp_angle(self._sp_angle)
            changed = (wb or wa) is not None
            if changed:
                self._apply_polar_locked(b, a)
        if changed:
            self._emit("warn", f"setpoint re-clamped to the new limits: {b:g} mT at {a:g} deg")
        self._emit("info", "settings applied")

    # ================================================================ control loop

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = self._clock()
            try:
                self.tick()
            except Exception as exc:                  # the loop must never die
                self._trip(f"control loop error: {type(exc).__name__}: {exc}")
            period = 1.0 / max(1.0, self.cfg.control.loop_hz)
            self._stop.wait(max(0.001, period - (self._clock() - t0)))

    def tick(self) -> None:
        """One loop cycle: read, check interlocks, regulate or ramp, write.

        Public so tests (with a fake clock) and shutdown() can drive it.
        """
        now = self._clock()
        c = self.cfg.control
        nominal = 1.0 / max(1.0, c.loop_hz)
        dt = nominal if self._last_tick is None else now - self._last_tick
        # A stalled tick (GC pause, slow USB) must not become one giant
        # integration step.
        dt = min(max(dt, 0.0), 5 * nominal)
        self._last_tick = now

        # ---- 1. read (hardware, outside the lock) ------------------------
        read_error = ""
        try:
            vx, vy = self.backend.read_hall()
            v1, v2 = self.backend.read_temps()
            water = bool(self.backend.read_water())
        except Exception as exc:
            read_error = f"{type(exc).__name__}: {exc}"
            vx = vy = v1 = v2 = _NAN
            water = self._water

        # ---- 2. decide (state, under the lock) ---------------------------
        events = []
        with self._lock:
            if read_error:
                self._hw_error = read_error
                if self._enable_hw or self._enable_want:
                    # Blind while energized: never regulate on stale numbers.
                    self._enter_fault_locked(f"hardware read failed: {read_error}", events)
            else:
                if self._hw_error:
                    events.append(("info", "hardware reads recovered"))
                self._hw_error = ""
                self._hall = [vx, vy]
                self._bx, self._by = self.cfg.hall.volts_to_mT(vx, vy)
                tc = self.cfg.temperature
                self._temps = [tc.t1_C_per_V * v1 + tc.t1_offset_C,
                               tc.t2_C_per_V * v2 + tc.t2_offset_C]
                self._water = water
                reason = self._interlock_reason_locked()
                if reason and self._state != FAULT:
                    self._enter_fault_locked(reason, events)

            state = self._state
            out = [p.output for p in self._pi]
            if state in (REGULATING, STABLE) and not read_error:
                sp = (self._sp_bx, self._sp_by)
                meas = (self._bx, self._by)
                kw = dict(kp=c.kp_V_per_mT, ki=c.ki_V_per_mT_s, ff_mT_per_V=c.ff_mT_per_V,
                          limit_V=abs(self.cfg.limits.ao_limit_V), slew_V_per_s=c.slew_V_per_s)
                # Regulate only once the enable line is really on; until then
                # the coils cannot respond and the integral would wind up.
                if self._enable_hw:
                    out = [self._pi[i].update(sp[i], meas[i], dt, **kw) for i in (0, 1)]
                self._update_stability_locked(now, events)
            elif self._enable_hw or state in (RAMP_DOWN, FAULT):
                out = [ramp_toward_zero(p.output, dt, c.slew_V_per_s) for p in self._pi]
                for p, o in zip(self._pi, out):
                    p.output = o
                if out == [0.0, 0.0]:
                    self._enable_want = False
                    if state == RAMP_DOWN:
                        self._state = OFF
                        events.append(("info", "output off (0 V, enable released)"))
            enable_want = self._enable_want
            enable_hw = self._enable_hw

        # ---- 3. act (hardware, outside the lock) --------------------------
        # Order matters: enable BEFORE driving, drive to 0 BEFORE disabling.
        try:
            if enable_want and not enable_hw:
                self.backend.set_enable(True)
            self.backend.write_ao(out[0], out[1])
            if enable_hw and not enable_want:
                self.backend.set_enable(False)
            with self._lock:
                self._enable_hw = enable_want
        except Exception as exc:
            with self._lock:
                self._hw_error = f"{type(exc).__name__}: {exc}"
                if self._state != FAULT:
                    self._enter_fault_locked(f"hardware write failed: {self._hw_error}", events)

        for level, msg in events:
            if level == "error":
                self._emit_rate_limited(msg)
            else:
                self._emit(level, msg)

    # ================================================================== internals

    def _update_stability_locked(self, now: float, events) -> None:
        c = self.cfg.control
        tol = abs(c.tolerance_mT)
        inside = (self._enable_hw
                  and abs(self._sp_bx - self._bx) <= tol
                  and abs(self._sp_by - self._by) <= tol)
        if not inside:
            if self._stable:
                events.append(("info", "field left the tolerance band"))
            self._stable = False
            self._stable_since = None
            self._state = REGULATING
            return
        if self._stable_since is None:
            self._stable_since = now
        if not self._stable and now - self._stable_since >= c.stable_time_s:
            self._stable = True
            self._state = STABLE
            events.append(("info", f"field stable at {self._sp_field:g} mT, "
                                   f"{self._sp_angle:g} deg"))

    def _energize_locked(self) -> None:
        c = self.cfg.control
        for i, p in enumerate(self._pi):
            meas = (self._bx, self._by)[i]
            sp = (self._sp_bx, self._sp_by)[i]
            if math.isfinite(meas) and (self._enable_hw or p.output != 0.0):
                # picking up a ramp in progress: continue from where it is
                p.bumpless(sp, meas, p.output, c.kp_V_per_mT, c.ki_V_per_mT_s, c.ff_mT_per_V)
            else:
                p.reset(p.output)
        self._state = REGULATING
        self._stable = False
        self._stable_since = None
        self._enable_want = True

    def _enter_fault_locked(self, reason: str, events) -> None:
        """Latch a fault: zero the setpoint (so clearing it later can never make
        the field jump back), stop regulating, ramp down."""
        self._fault = reason
        self._state = FAULT
        self._stable = False
        self._stable_since = None
        self._sp_field = 0.0
        self._sp_bx = self._sp_by = 0.0
        events.append(("error", f"FAULT: {reason} -- ramping the output to 0 V"))

    def _trip(self, reason: str) -> None:
        events = []
        with self._lock:
            if self._state != FAULT:
                self._enter_fault_locked(reason, events)
        for level, msg in events:
            self._emit(level, msg)

    def _interlock_reason_locked(self) -> str:
        ilk = self.cfg.interlock
        if not self._water and not ilk.water_bypass:
            return "cooling water lost (flow switch reads False)"
        if ilk.temp_monitor:
            for i, t in enumerate(self._temps):
                if math.isfinite(t) and t > ilk.max_temp_C:
                    return f"temperature {i + 1} is {t:.1f} C, above {ilk.max_temp_C:g} C"
        return ""

    def _refuse_in_fault(self, verb: str) -> None:
        if self._state == FAULT:
            raise Refused(f"{verb} refused: FAULT ({self._fault}). Fix the cause, "
                          f"then clear_fault.")

    def _clamp_field(self, b: float):
        m = abs(self.cfg.limits.field_max_mT)
        if b > m:
            return m, f"field {b:g} mT clamped to +{m:g} mT"
        if b < -m:
            return -m, f"field {b:g} mT clamped to -{m:g} mT"
        return b, None

    def _clamp_angle(self, a: float):
        lim = self.cfg.limits
        if a < lim.angle_min_deg:
            return lim.angle_min_deg, f"angle {a:g} deg clamped to {lim.angle_min_deg:g} deg"
        if a > lim.angle_max_deg:
            return lim.angle_max_deg, f"angle {a:g} deg clamped to {lim.angle_max_deg:g} deg"
        return a, None

    def _apply_polar_locked(self, b: float, a: float) -> None:
        rad = math.radians(a)
        self._sp_field, self._sp_angle = b, a
        self._sp_bx, self._sp_by = b * math.cos(rad), b * math.sin(rad)
        self._new_setpoint_locked()

    def _apply_vector_locked(self, bx: float, by: float):
        warn = None
        m = abs(self.cfg.limits.field_max_mT)
        mag = math.hypot(bx, by)
        if mag > m:
            # keep the DIRECTION, shorten the vector
            s = m / mag
            warn = f"|B| {mag:g} mT clamped to {m:g} mT (direction kept)"
            bx, by = bx * s, by * s
            mag = m
        # A zero vector has no direction; keep the current angle rather than
        # snapping to atan2(0, 0) = 0 deg.
        a = math.degrees(math.atan2(by, bx)) if mag > 0 else self._sp_angle
        self._sp_field, self._sp_angle = mag, a
        self._sp_bx, self._sp_by = bx, by
        self._new_setpoint_locked()
        return bx, by, warn

    def _new_setpoint_locked(self) -> None:
        # Same critical section as the setpoint itself: no frame can show the
        # new setpoint with the previous point's field_stable.
        self._stable = False
        self._stable_since = None
        if self._state == STABLE:
            self._state = REGULATING

    def _report(self, warn1, warn2, info: str) -> None:
        for w in (warn1, warn2):
            if w:
                self._emit("warn", w)
        if not (warn1 or warn2):
            self._emit("info", info)
        if self._state == OFF:
            self._emit("info", "output is OFF: the setpoint is stored and applies when "
                               "the output is switched on")

    def _sanitise_config(self) -> None:
        c = self.cfg.control
        c.slew_V_per_s = max(1e-3, float(c.slew_V_per_s))   # 0 would never ramp down
        c.loop_hz = min(1000.0, max(1.0, float(c.loop_hz)))
        c.stable_time_s = max(0.0, float(c.stable_time_s))
        lim = self.cfg.limits
        if lim.angle_min_deg > lim.angle_max_deg:
            lim.angle_min_deg, lim.angle_max_deg = lim.angle_max_deg, lim.angle_min_deg

    def _emit_rate_limited(self, msg: str) -> None:
        now = self._clock()
        if msg.startswith("FAULT") or now - self._last_err_emit >= 5.0:
            self._last_err_emit = now
            self._emit("error", msg)

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
