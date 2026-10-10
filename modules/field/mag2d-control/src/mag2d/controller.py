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
  OFF         output de-energized (enable line False; AO normally 0 V)
  REGULATING  energized, PI running, not (yet) stable
  STABLE      energized, PI running, every axis inside tolerance for stable_time
  RAMP_DOWN   output switched off: AO slewing to 0 V, then enable False -> OFF
  FAULT       an interlock tripped: setpoint zeroed, AO slewing to 0 V, then
              enable False; stays FAULT until the cause is gone AND clear_fault()

A new setpoint resets the stability timer in the same critical section that
stores it, so no status frame can show the new setpoint together with the old
point's field_stable=True.

SWEEPS (ramp_field / ramp_angle, for fly scans, 2026-10-10). A fly scan
records the detectors while a knob moves CONTINUOUSLY and sorts every sample
into the pixel of the value the knob had at that moment. Here the knob is the
field magnitude (at a fixed angle) or the field ANGLE (at a fixed magnitude:
the angular FMR scan as a fly axis). A sweep moves the SETPOINT along a
straight line in time -- each tick computes it from the elapsed time, so a
late tick just puts the setpoint further along and the pace stays exact --
and the PI, which runs continuously anyway, makes the field follow. No new
state: the magnet is REGULATING while the setpoint moves (never STABLE: the
setpoint changes every tick), and `ramping` says a sweep is on. At the target
the setpoint stops and the ordinary settling (field_stable) takes over.
  * every sweep is NUMBERED (ramp_id, gotcha #17): ramp_field returns the
    number, and "status ramp_id >= mine and not ramping" = my sweep is over;
  * any ordinary set (set_field, set_angle, set_vector, zero, output off)
    takes the knob over: the sweep stops first; ramp_stop ends it where it is;
  * a FAULT ends it too (the fault zeroes the setpoint anyway);
  * the fly scan bins by the MEASURED field: every Hall reading the loop takes
    is recorded with its time (the stream verbs), as the component along the
    setpoint direction ("field") and as the measured direction ("angle").
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .backends.base import VectorMagnetBackend
from .config import Config
from .pid import AxisPI, ramp_toward_zero
from .stream import StreamRecorder

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
    # The SWEEP (ramp_field / ramp_angle, fly scans). ramp_id = the number of
    # the newest sweep started; "ramp_id >= mine and not ramping" = it is over.
    # ramp_knob says WHICH knob sweeps ("field" or "angle"), and ramp_target /
    # ramp_rate are in that knob's units (mT and mT/s, or deg and deg/s).
    ramping: bool = False
    ramp_id: int = 0
    ramp_knob: str = ""
    ramp_target: float = _NAN
    ramp_rate: float = _NAN


@dataclass
class _Sweep:
    """One running sweep: the setpoint goes from `frm` to `to` at `rate`
    (units per second), starting at clock time `t0`."""

    knob: str            # "field" (signed mT, angle kept) or "angle" (deg, magnitude kept)
    frm: float
    to: float
    rate: float
    t0: float


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
        # The last drive the loop wrote (or adopted at start). tick() writes the
        # AO only when the wanted value differs: an OFF magnet is never touched,
        # and a drive adopted at start is not re-written by a rounding of itself.
        self._ao_last: tuple[float, float] | None = None
        self._ao_known = True        # False while the drive is unknown (no read-back)

        # ---- the SWEEP (see the module doc); under _lock ----
        self._sweep: _Sweep | None = None
        self._ramp_id = 0
        self._ramp_knob = ""
        self._ramp_target = _NAN
        self._ramp_rate = _NAN
        # THE STREAM (fly scans): every Hall reading the loop takes, with its
        # time, as the field along the setpoint direction and as the measured
        # angle (plus Bx, By and the setpoint at that moment). The control
        # thread appends (cheap: one lock, one deque append); the service's
        # stream verbs hand it out. Recording only while a stream is started.
        self.recorder = StreamRecorder(["field", "angle", "bx", "by",
                                        "setpoint_field", "setpoint_angle"])

        self._opened = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # =================================================================== lifecycle

    def start(self, run_thread: bool = True) -> None:
        """Open the hardware, READ what the magnet is doing, adopt it, run the loop.

        ADOPT ON START (Lukas, 2026-09-27: "all modules should read the
        instrument state on startup, not to change anything"). start() issues
        NO write to the card. It reads the enable line, the drive voltages (if
        the card can read them back), the Hall probes, the thermometers and the
        water switch, and takes over from there:
          * output OFF  -> state OFF; the setpoint stays 0 mT until commanded.
          * output ON   -> state REGULATING, setpoint = the field measured now,
                           and the PI starts BUMPLESS from the drive that is on
                           the wire -- the loop holds the field where it is.
        Before 2026-09-27 start() forced AO 0 V + enable False and then
        (energize_on_start) switched the output on at 0 mT: a magnet left at
        100 mT would have dropped to zero in one step.

        THE ONE EXCEPTION is the water interlock (a safety interlock, kept on
        purpose): water off and not bypassed -> WaterInterlockError, and closing
        the backend then puts 0 V / enable False on the card (backstop). A
        magnet that is energized without cooling must not be adopted.

        `run_thread=False` leaves the loop to the caller (tests call tick()).
        """
        self._sanitise_config()
        self.backend.open()
        self._opened = True
        try:
            water = bool(self.backend.read_water())
            enable, ao = self.backend.read_output_state()
        except Exception:
            self._close_backend()
            raise
        if not water and not self.cfg.interlock.water_bypass:
            self._close_backend()
            raise WaterInterlockError(
                "cooling water is OFF (flow switch reads False). Start the water, "
                "or run with --bypass-water (interlock.water_bypass = True).")
        # The field and temperatures: a failed read here is not fatal -- the
        # loop's first tick handles it exactly as it would later (a FAULT if
        # energized, never regulating blind).
        try:
            vx, vy = self.backend.read_hall()
            v1, v2 = self.backend.read_temps()
            read_error = ""
        except Exception as exc:
            vx = vy = v1 = v2 = _NAN
            read_error = f"{type(exc).__name__}: {exc}"
        events = []
        with self._lock:
            self._water = water
            if read_error:
                self._hw_error = read_error
            else:
                self._store_reads_locked(vx, vy, v1, v2)
            self._adopt_locked(enable, ao, events)
        if not water:
            self._emit("warn", "water interlock BYPASSED and the water is off")
        for level, msg in events:
            self._emit(level, msg)
        if run_thread:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="mag2d-loop", daemon=True)
            self._thread.start()

    def _adopt_locked(self, enable, ao, events) -> None:
        """Take over the output state read at start (see start()). No hardware."""
        c = self.cfg.control
        if enable is None:
            # The card could not tell us. Assume OFF (the loop then writes
            # nothing until commanded) and say so loudly.
            events.append(("warn", "the enable line could not be read back -- assuming "
                                   "the output is OFF; check the amplifier before energizing"))
            enable = False
        if ao is not None:
            ao = (float(ao[0]), float(ao[1]))
        self._ao_known = ao is not None

        if not enable:
            # OFF. Keep the drive that is on the wire as the loop's output, so
            # the OFF loop re-writes nothing (a value it already holds is
            # skipped); energizing later starts from 0 V (_energize_locked).
            out = ao if ao is not None else (0.0, 0.0)
            for p, o in zip(self._pi, out):
                p.reset(o)
            # Unknown drive -> _ao_last None: the OFF loop still writes nothing
            # (see tick), but the FIRST time the loop wants to drive (energize,
            # fault ramp) it writes for sure -- a 0 V it "already holds" must not
            # be skipped when nobody knows what the card is really putting out.
            self._ao_last = out if ao is not None else None
            self._state = OFF
            self._enable_hw = self._enable_want = False
            msg = "magnet started, output OFF (adopted)"
            if ao is not None and any(abs(v) > 1e-3 for v in ao):
                msg += (f"; the AO still holds {ao[0]:.3f} / {ao[1]:.3f} V with the "
                        f"enable line off (left as it is)")
            elif ao is None:
                msg += "; the AO drive cannot be read back on this card (shown as unknown)"
            events.append(("info", msg))
            return

        # ENERGIZED: adopt. Setpoint = the field there is now, so the loop's
        # first job is to hold it, not to move it.
        ff = c.ff_mT_per_V
        bx, by = self._bx, self._by
        if not (math.isfinite(bx) and math.isfinite(by)):
            # No field reading: estimate from the drive, or 0 if unknown. The
            # first tick re-reads; if the read keeps failing it FAULTs.
            bx, by = ((ao[0] * ff, ao[1] * ff) if ao is not None else (0.0, 0.0))
        bx, by, warn = self._apply_vector_locked(bx, by)
        if warn:
            events.append(("warn", "adopted setpoint " + warn))
        if ao is None:
            # No AO read-back on this card: the best guess of the drive is the
            # feed-forward's. The first PI write may differ from the real AO by
            # the feed-forward error (a few percent of the drive).
            ao = ((self._sp_bx / ff, self._sp_by / ff) if ff else (0.0, 0.0))
            events.append(("warn", "the AO drive could not be read back -- estimated "
                                   f"{ao[0]:.3f} / {ao[1]:.3f} V from the field"))
        meas = (self._bx, self._by)
        sp = (self._sp_bx, self._sp_by)
        for i, p in enumerate(self._pi):
            m = meas[i] if math.isfinite(meas[i]) else sp[i]
            p.bumpless(sp[i], m, ao[i], c.kp_V_per_mT, c.ki_V_per_mT_s, ff)
        self._ao_last = ao
        self._state = REGULATING
        self._stable = False
        self._stable_since = None
        self._enable_hw = self._enable_want = True
        events.append(("info", f"magnet started ENERGIZED (adopted): holding "
                               f"{self._sp_field:g} mT at {self._sp_angle:.2f} deg, "
                               f"drive {ao[0]:.3f} / {ao[1]:.3f} V"))

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Ramp the output to 0 V at the slew rate, disable, close. Idempotent.

        The loop thread is stopped FIRST, so from here on this (calling) thread
        is the only one touching the hardware -- and it keeps calling tick(),
        which does the ramp exactly as it would in RAMP_DOWN.

        keep_outputs=True is a RESTART for a code update (Lukas, 2026-10-06):
        the loop stops and the DAQ is closed and released, but nothing is
        written -- no ramp, no 0 V, enable line untouched. The coils keep the
        drive they have (open loop until the next start), and the next start
        ADOPTS it, as every start does.
        """
        if not self._opened:
            return
        with self._lock:
            self._sweep = None           # no sweep step may follow the ramp down
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        self._thread = None
        if keep_outputs:
            try:
                self.backend.close(output_off=False)
            finally:
                self._opened = False
            self._emit("info", "magnet service closed; output left as it is (restart)")
            return
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
            took = self._cancel_sweep_locked()
            a = self._sp_angle if a_in is None else a_in
            b, warn_b = self._clamp_field(b)
            a, warn_a = self._clamp_angle(a)
            self._apply_polar_locked(b, a)
        self._took_over(took)
        self._report(warn_b, warn_a, f"field -> {b:g} mT at {a:g} deg")

    def set_angle(self, angle_deg: float) -> None:
        """Rotate: keeps the (signed) field magnitude."""
        a = _finite(angle_deg, "angle_deg")
        with self._lock:
            self._refuse_in_fault("set_angle")
            took = self._cancel_sweep_locked()
            a, warn_a = self._clamp_angle(a)
            b = self._sp_field
            self._apply_polar_locked(b, a)
        self._took_over(took)
        self._report(None, warn_a, f"angle -> {a:g} deg (field {b:g} mT)")

    def set_vector(self, bx_mT: float, by_mT: float) -> None:
        bx = _finite(bx_mT, "bx_mT")
        by = _finite(by_mT, "by_mT")
        with self._lock:
            self._refuse_in_fault("set_vector")
            took = self._cancel_sweep_locked()
            bx, by, warn = self._apply_vector_locked(bx, by)
        self._took_over(took)
        self._report(warn, None, f"vector -> Bx {bx:g} mT, By {by:g} mT")

    def set_bx(self, bx_mT: float) -> None:
        """Set Bx, keep the By setpoint."""
        bx = _finite(bx_mT, "bx_mT")
        with self._lock:
            self._refuse_in_fault("set_bx")
            took = self._cancel_sweep_locked()
            bx, by, warn = self._apply_vector_locked(bx, self._sp_by)
        self._took_over(took)
        self._report(warn, None, f"Bx -> {bx:g} mT (By {by:g} mT)")

    def set_by(self, by_mT: float) -> None:
        """Set By, keep the Bx setpoint."""
        by = _finite(by_mT, "by_mT")
        with self._lock:
            self._refuse_in_fault("set_by")
            took = self._cancel_sweep_locked()
            bx, by, warn = self._apply_vector_locked(self._sp_bx, by)
        self._took_over(took)
        self._report(warn, None, f"By -> {by:g} mT (Bx {bx:g} mT)")

    def zero(self) -> None:
        """Field setpoint 0 mT, angle kept. Allowed during a FAULT (it only lowers)."""
        with self._lock:
            took = self._cancel_sweep_locked()
            self._apply_polar_locked(0.0, self._sp_angle)
        self._took_over(took)
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
            if not on and self._cancel_sweep_locked():
                # switching off takes the knob over like any set: the sweep
                # ends where it is (the setpoint stays there, the coils go to 0 V)
                msg = (msg + "; " if msg else "") + "sweep stopped"
        if msg:
            self._emit("info", msg)

    def output_off(self) -> None:
        """Ramp down to 0 V and switch the output off (the GUI's "Ramp down +
        off"). Same as set_output(False); a name of its own because over the
        wire it is the SAFETY verb a viewer may always send (net/service.py,
        control) -- set_output can also switch the coils ON."""
        self.set_output(False)

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

    # ==================================================================== sweeps

    def ramp_field(self, field_mT: float, rate_mT_per_s: float) -> int:
        """SWEEP the (signed) field magnitude to `field_mT` at `rate_mT_per_s`,
        the angle kept. Returns the sweep's number (status `ramp_id`).

        Starts from the present SETPOINT (where the loop is driving the field);
        a sweep already running is replaced from wherever it got to. The
        target is clamped to the field envelope and the rate to the configured
        limits, each with a warning, like every setter here. Refused during a
        FAULT. With the output OFF the setpoint walks as asked (it is stored,
        as a set_field would be) but nothing reaches the coils."""
        b = _finite(field_mT, "field_mT")
        rate = self._sweep_rate(rate_mT_per_s, "rate_mT_per_s",
                                self.cfg.limits.field_rate_min_mT_per_s,
                                self.cfg.limits.field_rate_max_mT_per_s, "mT/s")
        with self._lock:
            self._refuse_in_fault("ramp_field")
            b, warn_b = self._clamp_field(b)
            rid, frm = self._begin_sweep_locked("field", b, rate)
            angle = self._sp_angle
        if warn_b:
            self._emit("warn", "sweep target " + warn_b)
        self._emit("info", f"field sweep #{rid}: {frm:g} -> {b:g} mT at {rate:g} mT/s "
                           f"(angle {angle:g} deg)")
        self._report_off()
        return rid

    def ramp_angle(self, angle_deg: float, rate_deg_per_s: float) -> int:
        """SWEEP the field ANGLE to `angle_deg` at `rate_deg_per_s`, the
        (signed) magnitude kept -- an angular FMR scan as one continuous
        rotation. Returns the sweep's number. Same rules as ramp_field: the
        target is clamped to the angle limits, the rate to its limits (warned).
        The angle is NOT wrapped: 0 -> 400 deg turns more than once around, as
        asked (the setpoint model never normalises an angle)."""
        a = _finite(angle_deg, "angle_deg")
        rate = self._sweep_rate(rate_deg_per_s, "rate_deg_per_s",
                                self.cfg.limits.angle_rate_min_deg_per_s,
                                self.cfg.limits.angle_rate_max_deg_per_s, "deg/s")
        with self._lock:
            self._refuse_in_fault("ramp_angle")
            a, warn_a = self._clamp_angle(a)
            rid, frm = self._begin_sweep_locked("angle", a, rate)
            field = self._sp_field
        if warn_a:
            self._emit("warn", "sweep target " + warn_a)
        self._emit("info", f"angle sweep #{rid}: {frm:g} -> {a:g} deg at {rate:g} deg/s "
                           f"(field {field:g} mT)")
        self._report_off()
        return rid

    def ramp_stop(self) -> bool:
        """End a sweep WHERE IT IS: the setpoint stays at the value it has
        reached now and the loop holds it (a scan's Abort; a safety verb).
        True if a sweep was running."""
        with self._lock:
            if self._sweep is None:
                return False
            knob = self._sweep.knob
            self._advance_sweep_locked(self._clock(), [])
            self._sweep = None
            here = self._sp_field if knob == "field" else self._sp_angle
        unit = "mT" if knob == "field" else "deg"
        self._emit("info", f"{knob} sweep stopped at {here:g} {unit}")
        return True

    # the stream verbs (fly scans): see self.recorder
    def stream_start(self) -> int:
        return self.recorder.start()

    def stream_read(self) -> dict:
        return self.recorder.read()

    def stream_stop(self) -> dict:
        return self.recorder.stop()

    def _sweep_rate(self, rate, what: str, lo: float, hi: float, unit: str) -> float:
        r = abs(_finite(rate, what))
        if not r > 0:
            raise ValueError(f"{what} must be > 0")
        lo, hi = min(lo, hi), max(lo, hi)
        c = max(lo, min(hi, r))
        if c != r:
            self._emit("warn", f"sweep rate {r:g} {unit} clamped to {c:g} {unit} "
                               f"(limits {lo:g}..{hi:g})")
        return c

    def _begin_sweep_locked(self, knob: str, to: float, rate: float):
        """Start a sweep of `knob` (under _lock). Returns (ramp_id, start value)."""
        now = self._clock()
        if self._sweep is not None:
            # replaced: first bring the setpoint to where the old sweep is NOW
            self._advance_sweep_locked(now, [])
            self._sweep = None
        frm = self._sp_field if knob == "field" else self._sp_angle
        self._ramp_id += 1
        self._sweep = _Sweep(knob, frm, to, rate, now)
        self._ramp_knob, self._ramp_target, self._ramp_rate = knob, to, rate
        self._new_setpoint_locked()          # field_stable False from this frame on
        return self._ramp_id, frm

    def _advance_sweep_locked(self, now: float, events) -> None:
        """Put the setpoint where the sweep is at `now` (under _lock). The value
        comes from the ELAPSED time, not from adding a step per tick, so a late
        tick does not slow the sweep down. Clamped to the LIVE limits."""
        sw = self._sweep
        if sw is None:
            return
        span = abs(sw.to - sw.frm)
        travelled = sw.rate * max(0.0, now - sw.t0)
        arrived = travelled >= span
        v = sw.to if arrived else sw.frm + (1.0 if sw.to >= sw.frm else -1.0) * travelled
        if sw.knob == "field":
            b, _ = self._clamp_field(v)
            self._apply_polar_locked(b, self._sp_angle)
        else:
            a, _ = self._clamp_angle(v)
            self._apply_polar_locked(self._sp_field, a)
        if arrived:
            self._sweep = None
            unit = "mT" if sw.knob == "field" else "deg"
            events.append(("info", f"{sw.knob} sweep #{self._ramp_id} reached "
                                   f"{sw.to:g} {unit}; settling"))

    def _cancel_sweep_locked(self) -> bool:
        """An ordinary set takes the knob over: end a running sweep (under
        _lock) where the last tick left the setpoint. True if one ran."""
        was = self._sweep is not None
        self._sweep = None
        return was

    def _took_over(self, took: bool) -> None:
        if took:
            self._emit("info", "sweep stopped: a set takes over")

    def _report_off(self) -> None:
        if self._state == OFF:
            self._emit("info", "output is OFF: the setpoint sweeps, but nothing reaches "
                               "the coils until the output is switched on")

    def _measured_angle_locked(self, bx: float, by: float) -> float:
        """The measured DIRECTION, in the setpoint's own convention (under _lock).

        * A NEGATIVE signed setpoint points the field the other way, so the
          direction is read off -B: (-50 mT at 30 deg) measures as 30, not 210.
        * Unwrapped to within +-180 deg of the setpoint angle: atan2 knows only
          -180..180, but a setpoint may be 350 or 400 deg -- and a fly scan
          over the angle bins by this number, so it must live on the same axis.
        * Below limits.angle_min_field_mT the direction is noise; the setpoint
          angle is reported instead of a random number (see config)."""
        if not (math.isfinite(bx) and math.isfinite(by)):
            return _NAN
        if math.hypot(bx, by) < abs(self.cfg.limits.angle_min_field_mT):
            return self._sp_angle
        s = -1.0 if self._sp_field < 0 else 1.0
        raw = math.degrees(math.atan2(s * by, s * bx))
        return self._sp_angle + ((raw - self._sp_angle + 180.0) % 360.0 - 180.0)

    # ================================================================== reporting

    def status(self) -> Status:
        """A snapshot. Never touches the hardware."""
        with self._lock:
            bx, by = self._bx, self._by
            a = math.radians(self._sp_angle)
            if math.isfinite(bx) and math.isfinite(by):
                along = bx * math.cos(a) + by * math.sin(a)
                mag = math.hypot(bx, by)
                ang = self._measured_angle_locked(bx, by)
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
                # NaN (null on the wire) while the drive is unknown: a card
                # without AO read-back, output OFF, nothing written yet.
                output_V=([self._pi[0].output, self._pi[1].output] if self._ao_known
                          else [_NAN, _NAN]),
                hall_V=list(self._hall), temp_C=list(self._temps),
                water_ok=self._water, water_bypass=bool(ilk.water_bypass),
                temp_monitor=bool(ilk.temp_monitor),
                fault=self._fault, hw_error=self._hw_error,
                ramping=self._sweep is not None, ramp_id=self._ramp_id,
                ramp_knob=self._ramp_knob, ramp_target=self._ramp_target,
                ramp_rate=self._ramp_rate,
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
            # deadline + short time.sleep slices, not Event.wait(timeout): on
            # Windows a timed wait sleeps at least one 15.6 ms tick (gotcha
            # #34), and the "50 Hz" loop ran at ~32 Hz -- which is also the rate
            # a fly scan's field stream gets its samples at
            end = time.monotonic() + max(0.001, period - (self._clock() - t0))
            while not self._stop.is_set():
                left = end - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(left, 0.005))

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
        tw0 = tw1 = time.time()
        try:
            vx, vy = self.backend.read_hall()
            tw1 = time.time()        # the Hall reading is the mean over this window
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
                self._store_reads_locked(vx, vy, v1, v2)
                self._record_locked(0.5 * (tw0 + tw1))
                self._water = water
                reason = self._interlock_reason_locked()
                if reason and self._state != FAULT:
                    self._enter_fault_locked(reason, events)
            # a running sweep moves the setpoint to where it is NOW, before
            # the PI below acts on it (a fault above has already ended it)
            self._advance_sweep_locked(now, events)

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
        # Order matters. Switching ON: the drive is written FIRST (it is the
        # loop's starting value, 0 V from OFF), then the enable line -- so the
        # coils never see a stale AO left on the card. Switching OFF: drive to
        # 0 BEFORE disabling. The AO is written only when the wanted value
        # CHANGES: an adopted drive, or an OFF magnet, is left untouched.
        try:
            out_t = (out[0], out[1])
            if self._ao_last is None:
                # drive unknown (no AO read-back at start): write only once the
                # loop actually wants to drive -- never while simply sitting OFF
                driving = enable_want or enable_hw or state in (RAMP_DOWN, FAULT)
                must_write = driving
            else:
                must_write = out_t != self._ao_last
            if must_write:
                self.backend.write_ao(out[0], out[1])
                self._ao_last = out_t
                with self._lock:
                    self._ao_known = True
            if enable_want and not enable_hw:
                self.backend.set_enable(True)
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
            if math.isfinite(meas) and self._enable_hw:
                # picking up a ramp in progress: continue from where it is
                p.bumpless(sp, meas, p.output, c.kp_V_per_mT, c.ki_V_per_mT_s, c.ff_mT_per_V)
            else:
                # The coils carry no current (enable off), so start from 0 V --
                # even if a stale AO value was adopted at start: energizing onto
                # a leftover drive would be a voltage step on the coils.
                p.reset(0.0)
        self._state = REGULATING
        self._stable = False
        self._stable_since = None
        self._enable_want = True

    def _record_locked(self, t_wall: float) -> None:
        """One Hall reading into the stream (under _lock; a no-op unless a
        stream is started). Stamped with the wall clock at the MIDDLE of the
        read (time.time(): a coordinator on another PC lines it up), with the
        setpoint the loop was driving at that moment."""
        if not self.recorder.running:
            return
        bx, by = self._bx, self._by
        a = math.radians(self._sp_angle)
        along = (bx * math.cos(a) + by * math.sin(a)
                 if math.isfinite(bx) and math.isfinite(by) else _NAN)
        self.recorder.append(t_wall, (along, self._measured_angle_locked(bx, by), bx, by,
                                      self._sp_field, self._sp_angle))

    def _store_reads_locked(self, vx, vy, v1, v2) -> None:
        """Raw volts -> mT and C, into the brain attributes (used by start and tick)."""
        self._hall = [vx, vy]
        self._bx, self._by = self.cfg.hall.volts_to_mT(vx, vy)
        tc = self.cfg.temperature
        self._temps = [tc.t1_C_per_V * v1 + tc.t1_offset_C,
                       tc.t2_C_per_V * v2 + tc.t2_offset_C]

    def _enter_fault_locked(self, reason: str, events) -> None:
        """Latch a fault: zero the setpoint (so clearing it later can never make
        the field jump back), stop regulating, ramp down."""
        self._fault = reason
        self._state = FAULT
        self._stable = False
        self._stable_since = None
        self._sp_field = 0.0
        self._sp_bx = self._sp_by = 0.0
        # a running sweep ends here: it would otherwise walk the zeroed
        # setpoint up again (the sweep's number shows it over at once)
        swept = self._cancel_sweep_locked()
        events.append(("error", f"FAULT: {reason} -- ramping the output to 0 V"
                                + ("; sweep stopped" if swept else "")))

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
