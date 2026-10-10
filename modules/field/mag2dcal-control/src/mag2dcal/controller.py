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
  OFF         output de-energized (enable line False). Normally AO 0 V; a
              magnet FOUND off at start keeps whatever AO it had.
  SEEK        energized and moving: the calibrated jump, then the one-way PI
              trim. The output is free to change.
  HOLD        energized, the output is FROZEN inside tolerance/2, and the
              stability dwell is counting down. This is the state that proves
              the freeze happened: the drive is not moving and the field is
              being watched.
  STABLE      the dwell completed: field_stable is True. The output stays frozen
              and only the slow long-term stabilizer may nudge it.
  RAMP_DOWN   output switched off: AO slewing to 0 V, then enable False -> OFF
  SWEEP       the setpoint MOVES at a set pace (ramp_field / ramp_angle, for
              fly scans, 2026-10-10) and the drive follows it: calibrated
              feed-forward + a two-sided PI on the measured field, and on each
              axis the drive never steps back against the direction that
              axis's setpoint is moving (the hysteresis rule of clMag's SWEEP,
              gotcha #11). The FREEZE and the STABILIZER stand aside -- a
              frozen output cannot follow a moving setpoint, and a stabilizer
              nudge against the sweep would flip the iron's branch. At the end
              each axis settles as a set_field would end, WITHOUT going back:
              freeze if within tolerance/2, a one-way trim from where the drive
              is if behind, a fresh seek only if it ran past by more.
  CALIBRATE   measuring B(V) on both axes, both legs. No setpoint is regulated
              while this runs; it ends with the coils back at 0 V.
  FAULT       an interlock tripped: setpoint zeroed, AO slewing to 0 V, then
              enable False; stays FAULT until the cause is gone AND clear_fault()

A new setpoint resets the stability timer in the same critical section that
stores it, so no status frame can show the new setpoint together with the old
point's field_stable=True.

SWEEPS (the same verbs and status keys as mag2d-control). Every sweep is
NUMBERED (ramp_id, gotcha #17); any ordinary set, output off, a calibration,
shutdown or a FAULT stops it; ramp_stop ends it where it is (and settles there
without stepping back); the target and the rate are clamped with a warning.
Every Hall reading the loop takes is recorded with its time for the fly scan
to bin by (the stream verbs). With the output OFF the setpoint walks as asked
and nothing reaches the coils.
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
from .stream import StreamRecorder

_NAN = float("nan")

OFF, SEEK, HOLD, STABLE, SWEEP = "OFF", "SEEK", "HOLD", "STABLE", "SWEEP"
RAMP_DOWN, CALIBRATE, FAULT = "RAMP_DOWN", "CALIBRATE", "FAULT"
STATES = [OFF, SEEK, HOLD, STABLE, SWEEP, RAMP_DOWN, CALIBRATE, FAULT]

#: States in which the field loop is running (the magnet is trying to hold a
#: setpoint, or to follow a moving one). Used in a dozen places, so it gets a name.
REGULATING_STATES = (SEEK, HOLD, STABLE, SWEEP)


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
    # The SWEEP (ramp_field / ramp_angle, fly scans): the same keys as mag2d.
    # ramp_id = the newest sweep started; "ramp_id >= mine and not ramping" =
    # it is over. ramp_knob = "field" or "angle"; target / rate in its units.
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
        # Set by every setpoint change; the NEXT tick starts a fresh seek from
        # it. Deferring means the approach direction is decided from a live
        # measurement on the control thread, not from whatever the caller's
        # thread happened to see.
        self._seek_pending = False

        # ---- written by the control thread ----
        self._state = OFF
        self._enable_want = False     # what the loop should put on the enable line
        self._enable_hw = False       # what the enable line actually is
        # What is actually on the AO wires (last written, or ADOPTED at start).
        # The loop writes only when its output differs from this, so a magnet
        # found off with some leftover AO voltage is left exactly as it is, and
        # a frozen output is not re-written 50 times a second.
        self._ao_hw: list | None = None
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

        # ---- the SWEEP (see the module doc); under _lock ----
        self._sweep: _Sweep | None = None
        self._ramp_id = 0
        self._ramp_knob = ""
        self._ramp_target = _NAN
        self._ramp_rate = _NAN
        # the drive's own sweep state, per axis (state SWEEP only): the last
        # setpoint (its change per tick = which way the axis moves), the leg
        # of the calibration in use, the offset of the drive from that leg at
        # the start, and the PI's integral
        self._sw_prev = [0.0, 0.0]
        self._sw_leg = [1, 1]
        self._sw_off = [0.0, 0.0]
        self._sw_int = [0.0, 0.0]
        # THE STREAM (fly scans): every Hall reading the loop takes, with its
        # time -- the field along the setpoint direction, the measured angle,
        # Bx, By and the setpoint. Recording only while a stream is started.
        self.recorder = StreamRecorder(["field", "angle", "bx", "by",
                                        "setpoint_field", "setpoint_angle"])

        self._opened = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # =================================================================== lifecycle

    def start(self, run_thread: bool = True) -> None:
        """Open the hardware, READ what the magnet is doing and adopt it, check
        the water, load a calibration, run the loop.

        ADOPT, DON'T RESET (Lukas, 2026-09-27: "all modules should read the
        instrument state on startup, not to change anything"). This used to
        write AO 0 V + enable False and then energize at 0 mT, so every start
        de-energized a magnet that was holding a field. Now start() only
        QUERIES (enable line, drive voltages, Hall probes, temperatures, water)
        and takes over from there:
          * found energized -> state HOLD, setpoint = the measured field, each
            axis FROZEN at the drive it found. Nothing on the wire changes; the
            field becomes STABLE after the usual dwell.
          * found off       -> state OFF, setpoint 0 mT. Nothing is written
            until someone switches the output on (or energize_on_start=True,
            which is off by default and is the user's explicit choice).

        The exception is the SAFETY INTERLOCK: with the water off and not
        bypassed, start() refuses (WaterInterlockError -> run_service.py exits
        with code 3) and closes the hardware, and the backend's close()
        backstop puts AO 0 V / enable False on the wires -- a magnet without
        cooling must not stay driven. That write is deliberate and kept.

        `run_thread=False` leaves the loop to the caller (tests call tick()).
        """
        self._sanitise_config()
        self._load_newest_calibration()
        self.backend.open()
        self._opened = True
        try:
            water = bool(self.backend.read_water())
            found = self._read_found_state()
        except Exception:
            self._close_backend()
            raise
        if not water and not self.cfg.interlock.water_bypass:
            # SAFETY INTERLOCK (kept on purpose): closing runs the backend's
            # backstop, which de-energizes whatever a previous run left driven.
            self._close_backend()
            raise WaterInterlockError(
                "cooling water is OFF (flow switch reads False). Start the water, "
                "or run with --bypass-water (interlock.water_bypass = True).")
        events = []
        with self._lock:
            self._water = water
            self._adopt_locked(found, events)
            if not self._enable_want and self.cfg.control.energize_on_start:
                # Opt-in only (default False): the user asked for a service that
                # switches a de-energized magnet on at 0 mT when it starts.
                self._energize_locked()
                events.append(("info", "energize_on_start: switching the output on "
                                       "at 0 mT"))
        for level, msg in events:
            self._emit(level, msg)
        if not water:
            self._emit("warn", "water interlock BYPASSED and the water is off")
        if not self.is_calibrated:
            self._emit("warn", "no calibration loaded: the jump uses the straight line "
                               f"B / {self.cfg.control.ff_mT_per_V:g} mT per volt. Run "
                               "`calibrate` (or load a saved curve) for the real magnet.")
        if run_thread:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="mag2dcal-loop",
                                            daemon=True)
            self._thread.start()

    def _read_found_state(self) -> dict:
        """Query everything start() needs to adopt. READS ONLY.

        A backend without read_output() (an old test double) is treated as
        "cannot say", which the adoption handles. A Hall read that fails here
        is recorded, not raised: the first tick reports it again, and faults if
        the magnet is energized (the existing blind-while-energized rule).
        """
        reader = getattr(self.backend, "read_output", None)
        x_V, y_V, enabled = reader() if reader is not None else (None, None, None)
        found = {"x_V": x_V, "y_V": y_V, "enabled": enabled,
                 "hall": None, "temps": None, "error": "",
                 "notes": list(getattr(self.backend, "found_notes", None) or [])}
        try:
            found["hall"] = tuple(self.backend.read_hall())
            found["temps"] = tuple(self.backend.read_temps())
        except Exception as exc:
            found["error"] = f"{type(exc).__name__}: {exc}"
        return found

    def _adopt_locked(self, found: dict, events) -> None:
        """Make the brain describe the magnet as it was FOUND. No hardware I/O."""
        c = self.cfg.control
        for note in found["notes"]:
            events.append(("warn", f"start: {note}"))
        if found["hall"] is not None:
            vx, vy = found["hall"]
            self._hall = [vx, vy]
            self._bx, self._by = self.cfg.hall.volts_to_mT(vx, vy)
        if found["temps"] is not None:
            tc = self.cfg.temperature
            v1, v2 = found["temps"]
            self._temps = [tc.t1_C_per_V * v1 + tc.t1_offset_C,
                           tc.t2_C_per_V * v2 + tc.t2_offset_C]
        if found["error"]:
            self._hw_error = found["error"]
            events.append(("error", "start: could not read the Hall probes "
                                    f"({found['error']})"))

        enabled = found["enabled"]
        if enabled is None:
            enabled = False
            events.append(("warn", "start: the enable line could not be read; "
                                   "assuming the output is OFF. Nothing was written."))
        meas = (self._bx, self._by)
        outs = [found["x_V"], found["y_V"]]
        estimated = False
        for i in range(2):
            if outs[i] is None:
                # The card could not report its AO. De-energized, the coils carry
                # no current whatever the AO says, so 0 V is the honest model.
                # Energized, the best estimate is the drive that the calibration
                # (or the straight line) says produces the field we MEASURE.
                if enabled and math.isfinite(meas[i]):
                    outs[i] = self._volts_for_field_either_leg(i, meas[i])
                    estimated = True
                else:
                    outs[i] = 0.0
        if estimated:
            events.append(("warn", "start: the drive voltages could not be read back; "
                                   f"estimated X {outs[0]:+.3f} V, Y {outs[1]:+.3f} V "
                                   "from the measured field. The first correction may "
                                   "step by that estimate's error."))
        # What is on the wires now. The loop compares against this and writes
        # only on a change, so adopting costs no write at all.
        self._ao_hw = [float(outs[0]), float(outs[1])]
        self._enable_hw = self._enable_want = bool(enabled)

        if not enabled:
            for i, s in enumerate(self._seek):
                s.reset(outs[i])
            self._state = OFF
            self._sp_field = self._sp_angle = 0.0
            self._sp_bx = self._sp_by = 0.0
            self._seek_pending = False
            events.append(("info", "magnet started: found the output OFF and left it off "
                                   f"(AO X {outs[0]:+.3f} V, Y {outs[1]:+.3f} V)"))
            return

        # Energized: the setpoint becomes what the magnet IS doing, stored as the
        # measured vector (not snapped or rounded, like any setpoint).
        bx, by = meas
        if not (math.isfinite(bx) and math.isfinite(by)):
            bx = by = 0.0                        # blind: the first tick will fault
        if math.hypot(bx, by) <= abs(c.tolerance_mT):
            # Inside the band around zero the angle of the probe noise means
            # nothing: call it 0 mT at 0 deg.
            self._sp_field = self._sp_angle = 0.0
            self._sp_bx = self._sp_by = 0.0
        else:
            self._sp_field = math.hypot(bx, by)
            self._sp_angle = math.degrees(math.atan2(by, bx))
            self._sp_bx, self._sp_by = bx, by
        sp = (self._sp_bx, self._sp_by)
        for i, s in enumerate(self._seek):
            # FROZEN at the drive we found: the trim wakes only if the field
            # leaves the full tolerance band. The hysteresis branch is not
            # known; the direction the field was most likely ramped from (up to
            # a positive field, down to a negative one) is the best guess.
            s.adopt(outs[i], approach=1 if sp[i] >= 0 else -1)
        self._state = HOLD
        self._stable = False
        self._stable_since = None
        self._seek_pending = False
        if self._sp_field > self.field_envelope_mT():
            events.append(("warn", f"start: the magnet is holding {self._sp_field:.2f} mT, "
                                   "above this module's field envelope "
                                   f"({self.field_envelope_mT():g} mT). Left as found."))
        events.append(("info", "magnet started: found the output ENERGIZED, holding "
                               f"{self._sp_field:.2f} mT at {self._sp_angle:.1f} deg "
                               f"(AO X {outs[0]:+.3f} V, Y {outs[1]:+.3f} V, frozen)"))

    def _volts_for_field_either_leg(self, axis: int, field_mT: float) -> float:
        """Drive for a field when the hysteresis branch is unknown: the mean of
        the two legs (at worst half the loop width off)."""
        return 0.5 * (self._volts_for_field(axis, field_mT, +1)
                      + self._volts_for_field(axis, field_mT, -1))

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Ramp the output to 0 V at the slew rate, disable, close. Idempotent.

        The loop thread is stopped FIRST, so from here on this (calling) thread
        is the only one touching the hardware -- and it keeps calling tick(),
        which does the ramp exactly as it would in RAMP_DOWN.

        keep_outputs=True is a RESTART for a code update (Lukas, 2026-10-06):
        the loop stops and the DAQ is closed and released, but nothing is
        written -- no ramp, no 0 V, enable line untouched. The coils keep the
        drive they have (open loop, no stabilizer, until the next start), and
        the next start ADOPTS it, as every start does.
        """
        if not self._opened:
            return
        # A CALIBRATION in progress is a half-done sweep, not a state anyone
        # chose: keeping it would leave the coils at the sweep's current voltage
        # (up to v_max). A restart then stops as safely as a plain Stop does --
        # like a stage that still stops a running move (2026-10-06).
        with self._lock:
            calibrating = self._state == CALIBRATE or self._cal_job is not None
            # no sweep step may follow: a restart keeps the drive where it is,
            # a stop ramps it down (SWEEP is a regulating state)
            self._sweep = None
        if keep_outputs and calibrating:
            keep_outputs = False
            self._emit("warn", "restart during a calibration: the calibration is "
                               "abandoned and the output ramped down, as on Stop")
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
                self._ao_hw = [0.0, 0.0]
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
            self._refuse_while_calibrating("set_angle")
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
            self._refuse_while_calibrating("set_vector")
            took = self._cancel_sweep_locked()
            bx, by, warn = self._apply_vector_locked(bx, by)
        self._took_over(took)
        self._report(warn, None, f"vector -> Bx {bx:g} mT, By {by:g} mT")

    def set_bx(self, bx_mT: float) -> None:
        """Set Bx, keep the By setpoint."""
        bx = _finite(bx_mT, "bx_mT")
        with self._lock:
            self._refuse_in_fault("set_bx")
            self._refuse_while_calibrating("set_bx")
            took = self._cancel_sweep_locked()
            bx, by, warn = self._apply_vector_locked(bx, self._sp_by)
        self._took_over(took)
        self._report(warn, None, f"Bx -> {bx:g} mT (By {by:g} mT)")

    def set_by(self, by_mT: float) -> None:
        """Set By, keep the Bx setpoint."""
        by = _finite(by_mT, "by_mT")
        with self._lock:
            self._refuse_in_fault("set_by")
            self._refuse_while_calibrating("set_by")
            took = self._cancel_sweep_locked()
            bx, by, warn = self._apply_vector_locked(self._sp_bx, by)
        self._took_over(took)
        self._report(warn, None, f"By -> {by:g} mT (Bx {bx:g} mT)")

    def zero(self) -> None:
        """Field setpoint 0 mT, angle kept. Allowed during a FAULT (it only
        lowers), and it ABORTS a calibration -- it is the panic button."""
        aborted = False
        with self._lock:
            if self._cal_job is not None:
                self._abort_calibration_locked()
                aborted = True
            took = self._cancel_sweep_locked()
            self._apply_polar_locked(0.0, self._sp_angle)
        self._took_over(took)
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
            # a calibration drives the coils over the full range: a field
            # sweep cannot go on underneath it
            self._sweep = None
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
                ang = self._measured_angle_locked(bx, by)
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
                ramping=self._sweep is not None, ramp_id=self._ramp_id,
                ramp_knob=self._ramp_knob, ramp_target=self._ramp_target,
                ramp_rate=self._ramp_rate,
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
                self._hall = [vx, vy]
                self._bx, self._by = self.cfg.hall.volts_to_mT(vx, vy)
                tc = self.cfg.temperature
                self._temps = [tc.t1_C_per_V * v1 + tc.t1_offset_C,
                               tc.t2_C_per_V * v2 + tc.t2_offset_C]
                self._record_locked(0.5 * (tw0 + tw1))
                self._water = water
                reason = self._interlock_reason_locked()
                if reason and self._state != FAULT:
                    self._enter_fault_locked(reason, events)

            # A running sweep: the setpoint to where it is NOW (a fault above
            # has already ended it). An energized magnet that regulates goes
            # over to SWEEP -- also one switched on in the middle of a sweep.
            if not read_error:
                self._advance_sweep_locked(now, events)
                if (self._sweep is not None and self._enable_hw
                        and self._state in (SEEK, HOLD, STABLE)):
                    self._enter_sweep_drive_locked()

            state = self._state
            out = [s.output for s in self._seek]
            if state == CALIBRATE and not read_error and self._enable_hw:
                out = self._tick_calibrate_locked(now, dt, events)
            elif state == SWEEP and not read_error:
                if self._enable_hw:
                    out = self._tick_sweep_locked(dt)
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
        # Order matters: the drive is written BEFORE the enable line goes on (so
        # the amplifier wakes up to the voltage we chose, not to whatever was
        # left on the AO), and brought to 0 BEFORE it goes off. The AO is
        # written only when it CHANGES: an adopted or frozen output is left
        # alone, so starting on a magnet costs no write at all.
        with self._lock:
            ao_hw = self._ao_hw
        try:
            if ao_hw is None or out[0] != ao_hw[0] or out[1] != ao_hw[1]:
                self.backend.write_ao(out[0], out[1])
                with self._lock:
                    self._ao_hw = [float(out[0]), float(out[1])]
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

    # ------------------------------------------------------------ the field SWEEP

    def ramp_field(self, field_mT: float, rate_mT_per_s: float) -> int:
        """SWEEP the (signed) field magnitude to `field_mT` at `rate_mT_per_s`,
        the angle kept. Returns the sweep's number (status `ramp_id`).

        Starts from the present SETPOINT; a sweep already running is replaced
        from wherever it got to. The target is clamped to the field envelope
        (the calibration's, when one is loaded) and the rate to the configured
        limits, each with a warning. Refused during a FAULT or a calibration.
        With the output OFF the setpoint walks but nothing reaches the coils."""
        b = _finite(field_mT, "field_mT")
        rate = self._sweep_rate(rate_mT_per_s, "rate_mT_per_s",
                                self.cfg.limits.field_rate_min_mT_per_s,
                                self.cfg.limits.field_rate_max_mT_per_s, "mT/s")
        with self._lock:
            self._refuse_in_fault("ramp_field")
            self._refuse_while_calibrating("ramp_field")
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
        rotation. Returns the sweep's number. Same rules as ramp_field. The
        angle is NOT wrapped (the setpoint model never normalises one)."""
        a = _finite(angle_deg, "angle_deg")
        rate = self._sweep_rate(rate_deg_per_s, "rate_deg_per_s",
                                self.cfg.limits.angle_rate_min_deg_per_s,
                                self.cfg.limits.angle_rate_max_deg_per_s, "deg/s")
        with self._lock:
            self._refuse_in_fault("ramp_angle")
            self._refuse_while_calibrating("ramp_angle")
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
        """End a sweep WHERE IT IS (a scan's Abort; a safety verb): the setpoint
        stays at the value it has reached now and each axis settles there
        without stepping back (see _end_sweep_drive_locked). True if a sweep
        was running."""
        events = []
        with self._lock:
            if self._sweep is None:
                return False
            knob = self._sweep.knob
            self._advance_sweep_locked(self._clock(), events)
            if self._sweep is not None:
                self._sweep = None
                if self._state == SWEEP:
                    self._end_sweep_drive_locked(events)
            here = self._sp_field if knob == "field" else self._sp_angle
        for level, msg in events:
            self._emit(level, msg)
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
        """Start a sweep of `knob` (under _lock). Returns (ramp_id, start value).
        The control thread takes it up on its next tick (state SWEEP)."""
        now = self._clock()
        if self._sweep is not None:
            # replaced: first bring the setpoint to where the old sweep is NOW
            # (the drive keeps following; the state stays SWEEP)
            self._advance_sweep_locked(now, [], ending=False)
            self._sweep = None
        frm = self._sp_field if knob == "field" else self._sp_angle
        self._ramp_id += 1
        self._sweep = _Sweep(knob, frm, to, rate, now)
        self._ramp_knob, self._ramp_target, self._ramp_rate = knob, to, rate
        self._new_setpoint_locked()          # field_stable False from this frame on
        return self._ramp_id, frm

    def _advance_sweep_locked(self, now: float, events, ending: bool = True) -> None:
        """Put the setpoint where the sweep is at `now` (under _lock). The value
        comes from the ELAPSED time, so a late tick does not slow the sweep
        down. Clamped to the LIVE limits. At the target the sweep ends and,
        when the drive was following it, the endgame starts."""
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
            if ending and self._state == SWEEP:
                self._end_sweep_drive_locked(events)

    def _enter_sweep_drive_locked(self) -> None:
        """The drive takes up a sweep (under _lock): state SWEEP.

        The calibration is the mean of nothing -- it has an UP and a DOWN leg
        -- but the iron is on one branch, and the drive on the wire is offset
        from that leg by whatever the trim and the stabilizer have added. Keep
        that offset, so the feed-forward starts exactly where the magnet is (no
        step on the wire); the PI trims the rest. The leg is the one each axis
        last approached on (the branch the iron is on now)."""
        sp = (self._sp_bx, self._sp_by)
        meas = (self._bx, self._by)
        for i, s in enumerate(self._seek):
            # where the magnet IS: the measured field (the setpoint has already
            # moved on by a tick), or the sweep's start if there is no reading
            ref = meas[i] if math.isfinite(meas[i]) else self._sw_start_component(i)
            self._sw_leg[i] = s.approach
            self._sw_off[i] = s.output - self._volts_for_field(i, ref, s.approach)
            self._sw_int[i] = 0.0
            self._sw_prev[i] = sp[i]
            s.phase = IDLE                   # not frozen: the drive must move
        self._seek_pending = False
        self._stable = False
        self._stable_since = None
        self._state = SWEEP

    def _sw_start_component(self, i: int) -> float:
        """Axis i of the setpoint at the START of the running sweep."""
        sw = self._sweep
        if sw.knob == "field":
            b, a = sw.frm, self._sp_angle
        else:
            b, a = self._sp_field, sw.frm
        rad = math.radians(a)
        return b * math.cos(rad) if i == 0 else b * math.sin(rad)

    def _tick_sweep_locked(self, dt: float) -> list:
        """One tick of the drive following a moving setpoint (state SWEEP).

        Per axis: calibrated volts for the setpoint on the leg the axis is
        moving along, plus the offset kept at the start, plus a TWO-SIDED PI
        on the measured field (a moving setpoint needs corrections both ways
        -- the one-way trim is for the final approach). Then the hysteresis
        rule: the drive never moves against the direction the axis's
        setpoint is moving; if the field runs ahead, the drive waits for the
        setpoint to catch up and the integral does not grow meanwhile (anti-
        windup). An axis whose setpoint is not moving (a field sweep at 0 deg
        leaves By at 0) is held still while it is within tolerance. No
        freeze, no stabilizer here: see the module doc."""
        c = self.cfg.control
        limit = abs(self.cfg.limits.ao_limit_V)
        tol = abs(c.tolerance_mT)
        slew = c.jump_slew_V_per_s if self.is_calibrated else c.slew_V_per_s
        step = max(0.0, slew) * max(0.0, dt)
        sp = (self._sp_bx, self._sp_by)
        meas = (self._bx, self._by)
        for i, s in enumerate(self._seek):
            d = sp[i] - self._sw_prev[i]
            self._sw_prev[i] = sp[i]
            moving = 1 if d > 1e-9 else (-1 if d < -1e-9 else 0)
            if moving:
                self._sw_leg[i] = moving
                s.approach = moving
            err = sp[i] - meas[i]
            if not math.isfinite(err):
                continue
            candidate = self._sw_int[i] + err * dt
            corr = c.kp_V_per_mT * err + c.ki_V_per_mT_s * candidate
            wanted = (self._volts_for_field(i, sp[i], self._sw_leg[i])
                      + self._sw_off[i] + corr)
            held = False
            if moving and (wanted - s.output) * moving < 0:
                wanted, held = s.output, True        # never step back (gotcha #11)
            elif not moving and abs(err) <= tol:
                wanted, held = s.output, True        # a still axis stays still
            wanted = max(-limit, min(limit, wanted))
            out = max(s.output - step, min(s.output + step, wanted))
            if not held and abs(out - wanted) <= 1e-12:
                self._sw_int[i] = candidate          # integrate only if delivered
            s.output = out
            s.phase = IDLE
        return [s.output for s in self._seek]

    def _end_sweep_drive_locked(self, events) -> None:
        """The setpoint has stopped (arrived, or ramp_stop): settle there the
        way a set_field ENDS, without going back -- the drive must not reverse
        on an axis that was moving, or the iron changes branch (gotcha #11).
        Per axis:
          * within tolerance/2: freeze the drive where it is (it came from the
            sweep's side, so the branch is the right one);
          * behind (the coil lags): a one-way trim from the present drive, its
            cap never below the drive already on the wire;
          * past the target by more than tolerance/2: an ordinary seek for that
            axis (undershoot, then the one-way trim) brings it back properly."""
        tol = abs(self.cfg.control.tolerance_mT)
        sp = (self._sp_bx, self._sp_by)
        meas = (self._bx, self._by)
        for i, s in enumerate(self._seek):
            leg = self._sw_leg[i]
            err = sp[i] - meas[i] if math.isfinite(meas[i]) else 0.0
            if abs(err) <= tol / 2.0:
                s.adopt(s.output, leg)
            elif err * leg > 0:
                est = self._volts_for_field(i, sp[i], leg)
                cap = max(est, s.output) if leg > 0 else min(est, s.output)
                s.begin(s.output, cap, leg, settle_s=0.0, output_V=s.output)
            else:
                self._begin_axis_seek_locked(i, events, note="past the sweep's end, "
                                                            "re-approaching")
        self._seek_pending = False
        self._stable = False
        self._stable_since = None
        self._state = SEEK

    def _cancel_sweep_locked(self) -> bool:
        """An ordinary set takes the knob over: end a running sweep (under
        _lock) where the last tick left the setpoint, and hand the axes back
        to the ordinary seek (the set's own setpoint starts it). True if one ran."""
        was = self._sweep is not None
        self._sweep = None
        if self._state == SWEEP:
            self._state = SEEK
            self._seek_pending = True
        return was

    def _took_over(self, took: bool) -> None:
        if took:
            self._emit("info", "sweep stopped: a set takes over")

    def _report_off(self) -> None:
        if self._state == OFF:
            self._emit("info", "output is OFF: the setpoint sweeps, but nothing reaches "
                               "the coils until the output is switched on")

    def _record_locked(self, t_wall: float) -> None:
        """One Hall reading into the stream (under _lock; a no-op unless a
        stream is started). Stamped with the wall clock at the MIDDLE of the
        read, with the setpoint the loop was driving at that moment."""
        if not self.recorder.running:
            return
        bx, by = self._bx, self._by
        a = math.radians(self._sp_angle)
        along = (bx * math.cos(a) + by * math.sin(a)
                 if math.isfinite(bx) and math.isfinite(by) else _NAN)
        self.recorder.append(t_wall, (along, self._measured_angle_locked(bx, by), bx, by,
                                      self._sp_field, self._sp_angle))

    def _measured_angle_locked(self, bx: float, by: float) -> float:
        """The measured DIRECTION, in the setpoint's own convention (under
        _lock) -- identical to mag2d's: the direction of -B for a negative
        setpoint, unwrapped to within +-180 deg of the setpoint angle (a fly
        scan over the angle bins by this number), and the setpoint angle below
        limits.angle_min_field_mT, where the direction is only probe noise."""
        if not (math.isfinite(bx) and math.isfinite(by)):
            return _NAN
        if math.hypot(bx, by) < abs(self.cfg.limits.angle_min_field_mT):
            return self._sp_angle
        s = -1.0 if self._sp_field < 0 else 1.0
        raw = math.degrees(math.atan2(s * by, s * bx))
        return self._sp_angle + ((raw - self._sp_angle + 180.0) % 360.0 - 180.0)

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
        # From OFF the amplifier carries no current whatever the AO says, so the
        # honest starting drive is 0 V (a voltage adopted at start while the
        # output was off must not be switched straight onto the coils). From
        # RAMP_DOWN the coils are still driven: continue from where they are.
        for s in self._seek:
            s.reset(s.output if self._enable_hw else 0.0)
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
        # a running sweep ends here: it would otherwise walk the zeroed
        # setpoint up again (its number shows it over at once)
        swept = self._sweep is not None
        self._sweep = None
        self._state = FAULT
        self._stable = False
        self._stable_since = None
        self._sp_field = 0.0
        self._sp_bx = self._sp_by = 0.0
        self._seek_pending = True
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
