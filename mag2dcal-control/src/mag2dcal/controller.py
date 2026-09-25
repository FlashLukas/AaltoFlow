"""The Controller: the brain of the calibrated 2-axis vector magnet.

WHAT IT DOES. It holds a field setpoint (magnitude + angle, or Bx + By), reaches
it with a MEASURED calibration plus a short PI trim, freezes the output once it
is there, keeps it there with a slow stabilizer, reports whether the field has
been stable, and enforces the interlocks (cooling water, optional temperature).

It speaks exactly the same wire contract as `mag2d-control` -- same verbs, same
status keys, same describe ids -- so it is a drop-in alternative that vna-control,
scan-core and the launcher can talk to without knowing which of the two is
running. What differs is inside: mag2d runs a PI continuously, this one applies
clMag-control's philosophy (measure the plant, jump, trim, FREEZE, then trim
slowly forever). See pid.py for why the freeze matters, and calibration.py for
why the calibration has two legs per axis.

THE SETPOINT MODEL (unchanged from mag2d). Four numbers are stored together,
all at once, under one lock: field_mT (signed), angle_deg, bx_mT, by_mT.
  * set_field / set_angle / zero store the POLAR pair as given and derive
    Bx = B cos a, By = B sin a;
  * set_vector / set_bx / set_by store the CARTESIAN pair as given and derive
    B = hypot, a = atan2.
Whatever was commanded is stored EXACTLY -- never rounded, never normalised
(angle 370 stays 370). The scan engine waits until the status shows the value it
sent (tolerance 1e-6) before it trusts field_stable, and a setpoint the service
quietly tidied up would never be "adopted".

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
  SEEK        energized and moving: the calibrated jump, then the one-way PI
              trim. The output is free to change.
  HOLD        energized, the output is FROZEN inside tolerance/2, and the
              stability dwell is counting down. This is the state that proves
              the freeze happened: the drive is not moving and the field is
              being watched.
  STABLE      the dwell completed: field_stable is True. The output stays frozen
              and only the slow long-term stabilizer may nudge it.
  RAMP_DOWN   output switched off: AO slewing to 0 V, then enable False -> OFF
  CALIBRATE   measuring B(V) on both axes, both legs. No setpoint is regulated
              while this runs; it ends with the coils back at 0 V.
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
from dataclasses import asdict, dataclass, field

from . import calibration as cal_mod
from .backends.base import VectorMagnetBackend
from .calibration import Calibration, build_calibration, sweep_plan
from .config import Config, Hall
from .pid import FROZEN, IDLE, TRIM, AxisSeek, ramp_toward_zero, step_toward

_NAN = float("nan")

OFF, SEEK, HOLD, STABLE = "OFF", "SEEK", "HOLD", "STABLE"
RAMP_DOWN, CALIBRATE, FAULT = "RAMP_DOWN", "CALIBRATE", "FAULT"
STATES = [OFF, SEEK, HOLD, STABLE, RAMP_DOWN, CALIBRATE, FAULT]

#: States in which the field loop is running (the magnet is trying to hold a
#: setpoint). Used in a dozen places, so it gets a name.
REGULATING_STATES = (SEEK, HOLD, STABLE)


class Refused(Exception):
    """A command the magnet will not carry out now (e.g. a setpoint during a
    FAULT). The service turns it into {"ok": false, "error": <message>}."""


class WaterInterlockError(Refused):
    """Raised by start() when the cooling water is off and not bypassed."""


@dataclass
class Status:
    """One snapshot. The first block is EXACTLY mag2d-control's status contract
    (same names, same meanings), so a client written for that module works here
    unchanged; the last block is this module's additions."""

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
    # ---- this module's extras ----
    hw_error: str = ""                      # last failed hardware call
    frozen: bool = False                    # both axes holding their output still
    stabilizer: bool = True                 # the slow long-term trim is enabled
    calibrated: bool = False                # a measured B(V) calibration is loaded
    calibration_progress: float = 0.0       # 0..1 while CALIBRATE runs


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
        # Set by every setpoint change; the NEXT tick starts a fresh seek from
        # it. Deferring means the approach direction is decided from a live
        # measurement on the control thread, not from whatever the caller's
        # thread happened to see.
        self._seek_pending = False

        # ---- written by the control thread ----
        self._state = OFF
        self._enable_want = False     # what the loop should put on the enable line
        self._enable_hw = False       # what the enable line actually is
        self._seek = (AxisSeek(), AxisSeek())
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
        self._last_stab = -1e9
        self._stab_busy = [False, False]   # per axis: already reported drifting

        # ---- calibration ----
        self.calibration: Calibration | None = None
        self.stabilizer_enabled = bool(self.cfg.stabilizer.enabled)
        self._cal_job: dict | None = None

        self._opened = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # =================================================================== lifecycle

    def start(self, run_thread: bool = True) -> None:
        """Open the hardware, check the water, load a calibration, energize if
        configured, run the loop.

        Raises WaterInterlockError (after closing the hardware again) when the
        water is off and not bypassed -- run_service.py turns that into a clear
        message and exit code 3.

        `run_thread=False` leaves the loop to the caller (tests call tick()).
        """
        self._sanitise_config()
        self._load_newest_calibration()
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
        if not self.is_calibrated:
            self._emit("warn", "no calibration loaded: the jump uses the straight line "
                               f"B / {self.cfg.control.ff_mT_per_V:g} mT per volt. Run "
                               "`calibrate` (or load a saved curve) for the real magnet.")
        if run_thread:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="mag2dcal-loop",
                                            daemon=True)
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
                # A calibration in progress is abandoned: leaving the coils at
                # +5 V because someone closed the window is not acceptable.
                if self._cal_job is not None:
                    self._cal_job = None
                if self._state in REGULATING_STATES + (CALIBRATE,):
                    self._state = RAMP_DOWN
                    self._stable = False
                    self._stable_since = None
            c = self.cfg.control
            worst = max(abs(s.output) for s in self._seek)
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
                for s in self._seek:
                    s.reset(0.0)
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
            self._refuse_while_calibrating("set_field")
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
            self._refuse_while_calibrating("set_angle")
            a, warn_a = self._clamp_angle(a)
            b = self._sp_field
            self._apply_polar_locked(b, a)
        self._report(None, warn_a, f"angle -> {a:g} deg (field {b:g} mT)")

    def set_vector(self, bx_mT: float, by_mT: float) -> None:
        bx = _finite(bx_mT, "bx_mT")
        by = _finite(by_mT, "by_mT")
        with self._lock:
            self._refuse_in_fault("set_vector")
            self._refuse_while_calibrating("set_vector")
            bx, by, warn = self._apply_vector_locked(bx, by)
        self._report(warn, None, f"vector -> Bx {bx:g} mT, By {by:g} mT")

    def set_bx(self, bx_mT: float) -> None:
        """Set Bx, keep the By setpoint."""
        bx = _finite(bx_mT, "bx_mT")
        with self._lock:
            self._refuse_in_fault("set_bx")
            self._refuse_while_calibrating("set_bx")
            bx, by, warn = self._apply_vector_locked(bx, self._sp_by)
        self._report(warn, None, f"Bx -> {bx:g} mT (By {by:g} mT)")

    def set_by(self, by_mT: float) -> None:
        """Set By, keep the Bx setpoint."""
        by = _finite(by_mT, "by_mT")
        with self._lock:
            self._refuse_in_fault("set_by")
            self._refuse_while_calibrating("set_by")
            bx, by, warn = self._apply_vector_locked(self._sp_bx, by)
        self._report(warn, None, f"By -> {by:g} mT (Bx {bx:g} mT)")

    def zero(self) -> None:
        """Field setpoint 0 mT, angle kept. Allowed during a FAULT (it only
        lowers), and it ABORTS a calibration -- it is the panic button."""
        aborted = False
        with self._lock:
            if self._cal_job is not None:
                self._abort_calibration_locked()
                aborted = True
            self._apply_polar_locked(0.0, self._sp_angle)
        if aborted:
            self._emit("warn", "calibration aborted by `zero`")
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
                    msg = "output ON -- seeking the setpoint"
            elif self._state in REGULATING_STATES + (CALIBRATE,):
                if self._cal_job is not None:
                    self._abort_calibration_locked()
                    msg = "calibration aborted; output OFF -- ramping to 0 V"
                else:
                    msg = "output OFF -- ramping to 0 V"
                self._state = RAMP_DOWN
                self._stable = False
                self._stable_since = None
        if msg:
            self._emit("info", msg)

    def set_water_bypass(self, enabled: bool) -> None:
        self.cfg.interlock.water_bypass = bool(enabled)
        if enabled:
            self._emit("warn", "WATER INTERLOCK BYPASSED -- the coils are not protected "
                               "against running without cooling")
        else:
            self._emit("info", "water interlock active")

    def set_stabilizer(self, enabled: bool) -> None:
        """Turn the slow long-term trim on or off (see config.Stabilizer)."""
        self.stabilizer_enabled = bool(enabled)
        self.cfg.stabilizer.enabled = self.stabilizer_enabled
        self._emit("info", "long-term stabilizer "
                           + ("enabled" if enabled else "disabled"))

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

    # ================================================================= calibration

    @property
    def is_calibrated(self) -> bool:
        return self.calibration is not None and not self.calibration.is_empty

    def get_calibration(self) -> Calibration | None:
        return self.calibration

    def set_calibration(self, cal: Calibration | None) -> None:
        """Install a calibration (or None to go back to the straight line).

        The field limits follow it, so describe's `revision` moves and every
        client re-fetches its bounds.
        """
        self.calibration = cal if (cal is not None and not cal.is_empty) else None
        with self._lock:
            # A standing setpoint may be outside what the new curve covers.
            b, warn_b = self._clamp_field(self._sp_field)
            if warn_b:
                self._apply_polar_locked(b, self._sp_angle)
            else:
                self._seek_pending = True      # re-jump on the new curve
        if self.calibration is None:
            self._emit("warn", "calibration cleared: back to the straight-line jump")
        else:
            self._emit("info", f"calibration loaded: {self.calibration.summary()}")
        if warn_b:
            self._emit("warn", warn_b)

    def calibrate(self, n_per_leg: int | None = None, dwell_s: float | None = None,
                  v_max: float | None = None) -> None:
        """Measure B(V) on both axes, both legs. Returns at once: the sweep runs
        in the control loop and the state is CALIBRATE until it finishes.

        The magnet is driven over its FULL range while this runs, so it refuses
        unless the output is already energized and healthy. `zero`, switching the
        output off, a fault or a shutdown all abort it and bring the coils back
        to 0 V.
        """
        c = self.cfg.calibration
        n = int(c.n_per_leg if n_per_leg is None else n_per_leg)
        dwell = float(c.dwell_s if dwell_s is None else dwell_s)
        vmax = abs(float(c.v_max_V if v_max is None else v_max))
        if n < 2:
            raise ValueError("n_per_leg must be at least 2")
        if vmax <= 0.0:
            raise ValueError("v_max must be greater than 0 V")
        vmax = min(vmax, abs(self.cfg.limits.ao_limit_V))
        with self._lock:
            self._refuse_in_fault("calibrate")
            if self._cal_job is not None:
                raise Refused("a calibration is already running")
            if self._state not in REGULATING_STATES:
                raise Refused("calibrate refused: energize the output first "
                              "(set_output true)")
            plan = sweep_plan(n, vmax, limit_V=abs(self.cfg.limits.ao_limit_V))
            self._cal_job = {"plan": plan, "i": 0, "phase": "move",
                             "t": self._clock(), "dwell_s": max(0.0, dwell),
                             "points": []}
            # No field is being regulated during the sweep; say so honestly.
            self._apply_polar_locked(0.0, self._sp_angle)
            self._seek_pending = False
            self._state = CALIBRATE
        self._emit("info", f"calibration start: 2 axes x 2 legs x {n} points, "
                           f"+-{vmax:g} V, {dwell:g} s dwell "
                           f"({len(plan)} steps)")

    def _abort_calibration_locked(self) -> None:
        """Stop the sweep and hand the axes back to the field loop at 0 mT."""
        self._cal_job = None
        self._sp_field = 0.0
        self._sp_bx = self._sp_by = 0.0
        self._seek_pending = True
        if self._state == CALIBRATE:
            self._state = SEEK

    def _load_newest_calibration(self) -> None:
        if self.calibration is not None or not self.cfg.calibration.load_newest_on_start:
            return
        try:
            path = cal_mod.newest_calibration(self.cfg.calibration.directory)
            if path is None:
                return
            self.calibration = Calibration.load(path)
            self._emit("info", f"calibration loaded from {path.name}: "
                               f"{self.calibration.summary()}")
        except Exception as exc:              # a bad file must not stop the service
            self._emit("error", f"could not load a saved calibration: "
                                f"{type(exc).__name__}: {exc}")

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
            job = self._cal_job
            progress = 0.0 if job is None else job["i"] / max(1, len(job["plan"]))
            return Status(
                state=self._state, energized=self._enable_hw,
                setpoint_field_mT=self._sp_field, setpoint_angle_deg=self._sp_angle,
                setpoint_bx_mT=self._sp_bx, setpoint_by_mT=self._sp_by,
                measured_bx_mT=bx, measured_by_mT=by,
                measured_field_mT=along, measured_magnitude_mT=mag,
                measured_angle_deg=ang, error_mT=err,
                field_stable=self._stable and self._enable_hw and self._state == STABLE,
                output_V=[self._seek[0].output, self._seek[1].output],
                hall_V=list(self._hall), temp_C=list(self._temps),
                water_ok=self._water, water_bypass=bool(ilk.water_bypass),
                temp_monitor=bool(ilk.temp_monitor),
                fault=self._fault, hw_error=self._hw_error,
                frozen=all(s.frozen for s in self._seek),
                stabilizer=bool(self.stabilizer_enabled),
                calibrated=self.is_calibrated,
                calibration_progress=progress,
            )

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-check cfg after it was edited in place (Settings, set_config).

        The seek reads its gains from cfg on every tick, so gains need nothing
        here. Limits do: a setpoint outside a NEW, narrower envelope is clamped.
        """
        self._sanitise_config()
        self.stabilizer_enabled = bool(self.cfg.stabilizer.enabled)
        with self._lock:
            b, wb = self._clamp_field(self._sp_field)
            a, wa = self._clamp_angle(self._sp_angle)
            changed = (wb or wa) is not None
            if changed:
                self._apply_polar_locked(b, a)
        if changed:
            self._emit("warn", f"setpoint re-clamped to the new limits: {b:g} mT at {a:g} deg")
        self._emit("info", "settings applied")

    def field_envelope_mT(self) -> float:
        """The largest |B| this module will accept, in mT.

        With a calibration loaded this is the narrower of the configured hard
        limit and what the calibration was actually measured over -- there is no
        honest way to command a field the magnet has never been seen to reach.
        Every bound in describe() comes from here, so loading a calibration moves
        the sliders (and the manifest revision) with no second edit.
        """
        m = abs(self.cfg.limits.field_max_mT)
        if self.is_calibrated:
            m = min(m, self.calibration.field_max_mT())
        return m

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
        """One loop cycle: read, check interlocks, seek / sweep / ramp, write.

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
            out = [s.output for s in self._seek]
            if state == CALIBRATE and not read_error and self._enable_hw:
                out = self._tick_calibrate_locked(now, dt, events)
            elif state in REGULATING_STATES and not read_error:
                # Regulate only once the enable line is really on; until then
                # the coils cannot respond and the integral would wind up.
                if self._enable_hw:
                    out = self._tick_seek_locked(now, dt, events)
                self._update_stability_locked(now, events)
            elif self._enable_hw or state in (RAMP_DOWN, FAULT):
                out = [ramp_toward_zero(s.output, dt, c.slew_V_per_s) for s in self._seek]
                for s, o in zip(self._seek, out):
                    s.output = o
                    s.phase = IDLE
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

    # ------------------------------------------------------------ the field seek

    def _tick_seek_locked(self, now: float, dt: float, events) -> list:
        c = self.cfg.control
        limit = abs(self.cfg.limits.ao_limit_V)
        sp = (self._sp_bx, self._sp_by)
        meas = (self._bx, self._by)

        if self._seek_pending:
            self._begin_seek_locked(events)

        for i, s in enumerate(self._seek):
            err = sp[i] - meas[i]
            # A genuine overshoot past the far side of the band means the
            # approach direction was wrong (or the calibration is): start again
            # from the other leg rather than sitting there one-way clamped.
            if (c.freeze_enabled and s.phase in (TRIM, FROZEN)
                    and err * s.approach < -abs(c.tolerance_mT)):
                self._begin_axis_seek_locked(i, events, note="overshot, re-approaching")
                err = sp[i] - meas[i]
            # The JUMP may run at the fast hardware rate only when a measured
            # calibration says where it is going. Uncalibrated, the destination
            # is the straight-line guess (a few percent out), and racing toward
            # a guess is exactly how the overshoot this module exists to avoid
            # gets back in -- so that case keeps the conservative slew.
            jump_slew = (c.jump_slew_V_per_s if self.is_calibrated
                         else c.slew_V_per_s)
            s.update(err, dt, kp=c.kp_V_per_mT, ki=c.ki_V_per_mT_s, limit_V=limit,
                     slew_V_per_s=jump_slew, tolerance_mT=c.tolerance_mT,
                     trim_slew_V_per_s=c.trim_slew_V_per_s,
                     measured_mT=meas[i],
                     settle_rate_mT_per_s=c.settle_rate_mT_per_s,
                     settle_window_s=c.settle_window_s,
                     margin_V=c.seek_margin_V, margin_frac=c.seek_margin_frac,
                     # freeze_enabled turns the whole clMag philosophy on or
                     # off in one switch: with it off the trim is an ordinary
                     # two-way PI that never stops, i.e. mag2d-control.
                     freeze=c.freeze_enabled, one_way=c.freeze_enabled)

        self._run_stabilizer_locked(now, events)
        return [s.output for s in self._seek]

    def _begin_seek_locked(self, events) -> None:
        self._seek_pending = False
        for i in range(2):
            self._begin_axis_seek_locked(i, events)
        events.append(("info", "seek: jump to "
                               f"X {self._seek[0].jump_V:+.3f} V, "
                               f"Y {self._seek[1].jump_V:+.3f} V, then trim"))

    def _begin_axis_seek_locked(self, i: int, events, note: str = "") -> None:
        """Plan one axis's approach: which leg, which voltage, how far short."""
        c = self.cfg.control
        target = (self._sp_bx, self._sp_by)[i]
        measured = (self._bx, self._by)[i]
        if not math.isfinite(measured):
            measured = 0.0
        approach = 1 if target >= measured else -1
        # Stop SHORT of the target on the approach side, so the last stretch is
        # walked in one direction only and we stay on one hysteresis branch.
        detuned = target - approach * abs(c.field_step_mT)
        jump_V = self._volts_for_field(i, detuned, approach)
        target_V = self._volts_for_field(i, target, approach)
        self._seek[i].begin(jump_V, target_V, approach,
                            settle_s=c.jump_settle_s)
        if note:
            events.append(("info", f"axis {'XY'[i]}: {note}"))

    def _volts_for_field(self, axis: int, field_mT: float, approach: int) -> float:
        """The drive voltage for a field on this axis, on the leg we approach from.

        With no calibration this is the straight line B / ff_mT_per_V -- honest,
        and good enough for the sim, but on the real magnet it is exactly the
        approximation the calibration replaces.
        """
        limit = abs(self.cfg.limits.ao_limit_V)
        if self.is_calibrated:
            v = self.calibration.volts_for_field(axis, field_mT, approach)
        else:
            ff = self.cfg.control.ff_mT_per_V
            v = field_mT / ff if ff else 0.0
        return max(-limit, min(limit, v))

    def _run_stabilizer_locked(self, now: float, events) -> None:
        """The slow long-term trim (see config.Stabilizer).

        It runs ONLY while the field is being held (HOLD/STABLE), at most once
        every period_s, and only outside a deadband. It writes the output
        directly, bypassing the freeze, because the freeze exists to stop the
        FAST loop -- this is the one thing allowed to move a frozen output, and
        it moves it by a few millivolts at a time.
        """
        st = self.cfg.stabilizer
        if not self.stabilizer_enabled or self._state not in (HOLD, STABLE):
            return
        if now - self._last_stab < max(0.0, st.period_s):
            return
        self._last_stab = now
        limit = abs(self.cfg.limits.ao_limit_V)
        sp = (self._sp_bx, self._sp_by)
        meas = (self._bx, self._by)
        for i, s in enumerate(self._seek):
            err = sp[i] - meas[i]
            if not math.isfinite(err) or abs(err) <= abs(st.deadband_mT):
                self._stab_busy[i] = False
                continue
            dV = max(-abs(st.max_step_V), min(abs(st.max_step_V),
                                              err * st.gain_V_per_mT))
            s.nudge(dV, limit)
            # One line per drift EXCURSION, not per nudge: a magnet that is
            # slowly warming up would otherwise write a line a second into the
            # launcher log for as long as the scan lasts.
            if not self._stab_busy[i]:
                self._stab_busy[i] = True
                events.append(("info", f"stabilizer: axis {'XY'[i]} drifted "
                                       f"{err:+.3f} mT, trimming the drive "
                                       f"({dV:+.4f} V per {st.period_s:g} s)"))

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
            self._state = SEEK
            return
        if self._stable_since is None:
            self._stable_since = now
        if not self._stable and now - self._stable_since >= c.stable_time_s:
            self._stable = True
            self._state = STABLE
            events.append(("info", f"field stable at {self._sp_field:g} mT, "
                                   f"{self._sp_angle:g} deg "
                                   + ("(output frozen)" if all(s.frozen for s in self._seek)
                                      else "")))
        elif not self._stable:
            # Inside the band but still dwelling: HOLD once the output has
            # actually stopped moving, otherwise we are still seeking.
            self._state = HOLD if all(s.frozen for s in self._seek) else SEEK

    # ------------------------------------------------------- the calibration sweep

    def _tick_calibrate_locked(self, now: float, dt: float, events) -> list:
        """One step of the measurement sweep. See calibration.sweep_plan().

        Two phases per step: "move" ramps the swept axis to the step's voltage
        (and the other axis to 0) at the normal slew rate, then "dwell" waits
        dwell_s for the iron and the probes to settle before recording.
        """
        job = self._cal_job
        if job is None:                        # aborted between ticks
            self._state = SEEK
            return [s.output for s in self._seek]
        c = self.cfg.control
        step = job["plan"][job["i"]]
        limit = abs(self.cfg.limits.ao_limit_V)
        want = [0.0, 0.0]
        want[step.axis] = max(-limit, min(limit, step.volts))
        max_step = max(0.0, c.slew_V_per_s) * dt

        moved = []
        for i, s in enumerate(self._seek):
            s.output = step_toward(s.output, want[i], max_step)
            s.phase = IDLE
            moved.append(abs(s.output - want[i]) <= 1e-12)

        if job["phase"] == "move":
            if all(moved):
                job["phase"] = "dwell"
                job["t"] = now
        elif now - job["t"] >= job["dwell_s"]:
            if step.record:
                # The Hall read is already the mean of many samples, so one
                # settled reading is the measurement. The voltage recorded is
                # the COMMANDED one (the ramp has arrived, so they are equal).
                field_mT = (self._bx, self._by)[step.axis]
                job["points"].append((step.axis, step.leg, float(step.volts), field_mT))
            job["i"] += 1
            job["phase"] = "move"
            if job["i"] >= len(job["plan"]):
                self._finish_calibrate_locked(events)
        return [s.output for s in self._seek]

    def _finish_calibrate_locked(self, events) -> None:
        job = self._cal_job
        self._cal_job = None
        # A COPY of the Hall constants, not the live config object: this records
        # what the curve was taken with, and editing Settings later must not
        # rewrite the provenance of a measurement already made.
        cal = build_calibration(job["points"], hall=Hall(**asdict(self.cfg.hall)),
                                note="measured by mag2dcal-control")
        self.calibration = cal
        events.append(("info", f"calibration done: {cal.summary()}"))
        if self.cfg.calibration.auto_save:
            try:
                d = cal_mod.calibration_dir(self.cfg.calibration.directory)
                d.mkdir(parents=True, exist_ok=True)
                path = d / cal_mod.default_filename()
                cal.save(path)
                events.append(("info", f"calibration saved to {path}"))
            except Exception as exc:
                events.append(("error", f"could not save the calibration: "
                                        f"{type(exc).__name__}: {exc}"))
        # Back to the field loop at 0 mT, on the fresh curve.
        self._sp_field = 0.0
        self._sp_bx = self._sp_by = 0.0
        self._seek_pending = True
        self._state = SEEK

    # ================================================================== internals

    def _energize_locked(self) -> None:
        for s in self._seek:
            s.reset(s.output)
        self._state = SEEK
        self._stable = False
        self._stable_since = None
        self._seek_pending = True
        self._enable_want = True

    def _enter_fault_locked(self, reason: str, events) -> None:
        """Latch a fault: zero the setpoint (so clearing it later can never make
        the field jump back), stop regulating, ramp down."""
        self._fault = reason
        self._cal_job = None
        self._state = FAULT
        self._stable = False
        self._stable_since = None
        self._sp_field = 0.0
        self._sp_bx = self._sp_by = 0.0
        self._seek_pending = True
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

    def _refuse_while_calibrating(self, verb: str) -> None:
        if self._cal_job is not None:
            raise Refused(f"{verb} refused: a calibration is running. Wait for it, "
                          f"or abort it with `zero`.")

    def _clamp_field(self, b: float):
        m = self.field_envelope_mT()
        how = "the calibration" if self.is_calibrated else "the configured limit"
        if b > m:
            return m, f"field {b:g} mT clamped to +{m:g} mT ({how})"
        if b < -m:
            return -m, f"field {b:g} mT clamped to -{m:g} mT ({how})"
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
        m = self.field_envelope_mT()
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
        self._seek_pending = True
        if self._state in (STABLE, HOLD):
            self._state = SEEK

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
        c.field_step_mT = abs(float(c.field_step_mT))
        c.jump_settle_s = max(0.0, float(c.jump_settle_s))
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
