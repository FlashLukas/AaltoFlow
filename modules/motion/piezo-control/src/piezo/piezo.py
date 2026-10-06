"""The brain: :class:`Piezo` (§5 of the guide).

A 2-axis piezo stage sits between the two instrument families:

  * In CLOSED loop the d-Drive's own servo drives the strain-gauge reading to
    the setpoint -- *it* runs the control loop, not us.  So for position we are
    "set-and-forget": command a target, poll the read-out.
  * The one bit of control machinery we DO own is VELOCITY.  A piezo otherwise
    jumps to a setpoint as fast as it can; to move at a chosen speed we either
    lean on the controller's native slew-rate limiter ("hardware" ramp) or walk
    the setpoint there ourselves in small timed steps ("software" ramp).  The
    software ramp runs in one background thread and is the reason this brain
    isn't purely trivial.

Responsibilities (per the blueprint):
  * hold the DESIRED state (target, loop mode, velocity) and the config,
  * clamp every request to the safety envelope (``cfg.limits``) -- and the
    travel ceiling DEPENDS on each axis' loop mode (CL travel < OL travel),
  * push accepted values to the backend (directly, or via the ramp thread),
  * own the relative "zero here" frame and the 20-slot position list,
  * report a :class:`PiezoStatus` snapshot that must NEVER throw.

Axes: 0 = X, 1 = Y.  Units: micrometres (um), velocity um/s.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .backends.base import PiezoBackend
from .config import (
    Config,
    axis_channel,
    axis_closed_loop_default,
    axis_rel_origin,
    axis_velocity,
    set_axis_rel_origin,
    set_axis_velocity,
    travel_max,
)
from .positions import PositionList

AXES = ("X", "Y")

# How close (um) the measured position must be to the target to count as
# "settled" for a CLOSED-LOOP hardware/off-mode move (software moves track the
# ramp flag; open-loop moves use the expected slew time, see status()).
SETTLE_TOL = 0.1


@dataclass
class PiezoStatus:
    """A snapshot of everything the GUI / remote client needs.

    Lists are length-2, axis-indexed (X, Y).  ``status()`` guarantees this
    object is always returned, even if a hardware read fails mid-way.
    """

    position: list       # measured position per axis, um
    target: list         # commanded target per axis, um
    relative: list       # position measured from the relative (zeroed) origin
    rel_origin: list     # the current relative origin per axis, device um
    moving: list         # bool per axis (ramping or not yet settled)
    closed_loop: list    # bool per axis (True = closed loop / servo on)
    velocity: list       # um/s per axis
    travel_max: list     # effective upper travel limit per axis (mode-dependent)
    ramp_mode: str       # "hardware" | "software" | "off"
    connected: bool
    # TARGET ECHO (Lukas, 2026-09-28): the target AS REQUESTED by the last move
    # of each axis, um -- what the `position_x/y` settle waits for (see
    # describe.py).  Unlike `target` above it is stored only AFTER the setpoint
    # went to the controller, together with the state `moving` is computed
    # from; see the ordering rule in Piezo.status().  None = unknown (start
    # without a readable setpoint).  None and never NaN: scan-core's adopt
    # check abs(float(sp) - target) > tol is False for NaN, i.e. "adopted".
    target_um: list = field(default_factory=lambda: [None, None])
    # "" while the controller answers; the error text while position reads
    # fail.  `position` is then the LAST GOOD read-out and a closed-loop axis
    # reports moving (unknown is not "at rest").  scan-core pauses on it.
    hw_error: str = ""


class Piezo:
    def __init__(self, backend: PiezoBackend, cfg: Config):
        self.backend = backend
        self.cfg = cfg
        self.positions = PositionList()
        self._connected = False

        # Desired state we own (the backend can't always read these back).
        self._target = [0.0, 0.0]
        self._closed = [axis_closed_loop_default(cfg, a) for a in range(2)]

        # Software-ramp state, guarded by _ramp_lock and serviced by one thread.
        self._ramp_lock = threading.Lock()
        self._ramp_active = [False, False]
        self._ramp_from = [0.0, 0.0]
        self._ramp_to = [0.0, 0.0]
        self._ramp_t0 = [0.0, 0.0]
        self._ramp_vel = [axis_velocity(cfg, a) for a in range(2)]
        # The setpoint the brain last WROTE to the controller, per axis (also
        # guarded by _ramp_lock).  Why we need it (deep cleaning 2026-09-28):
        # in OPEN loop the read-out is off the drive by the piezo's hysteresis
        # (1-2 %), so "where is the drive?" can only be answered by what we
        # commanded, not by what we read.  Starting a ramp, freezing on STOP
        # and re-anchoring on a velocity change all start from here.
        self._cmd = [0.0, 0.0]
        # Open-loop "moving" (deep cleaning 2026-09-28): with no sensor to
        # compare against, an OL hardware/off move is "moving" until the time
        # the controller's slew limiter needs for the distance has passed.
        # Monotonic time per axis at which the current direct move is done.
        self._hw_eta = [0.0, 0.0]
        # Target echo per axis (guarded by _ramp_lock), see PiezoStatus.
        self._echo: list = [None, None]
        # Last good position read-out, and the running error episode (""=none).
        self._last_pos = [float("nan"), float("nan")]
        self._hw_error = ""
        self._err_lock = threading.Lock()
        self._stop = threading.Event()
        self._ramp_thread: threading.Thread | None = None

        # The service replaces this hook to forward events onto the wire; the
        # GUI replaces it to append to its log.  Levels: "info"|"warn"|"error".
        self._on_event = lambda level, msg: None

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #
    def _emit(self, level: str, msg: str) -> None:
        try:
            self._on_event(level, msg)
        except Exception:
            pass  # a broken listener must never break the instrument

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Open the backend, ADOPT the controller's state, run the ramp thread.

        Adopt rule (Lukas, 2026-09-27: "read the instrument state on startup,
        not change anything").  Starting the service must never move the stage
        or switch its loop mode, so nothing is WRITTEN here -- we only READ:

          * loop mode per axis  -> ``_closed`` (and cfg.motion.closed_loop_*,
            so a later ``set_config`` of an unrelated group does not flip it),
          * the setpoint the controller is holding -> ``_target``,
          * the native slew rate -> velocity + ramp mode (see _adopt_slew).

        A read that fails leaves the config default in place for THAT item and
        says so in a warn event -- still without writing it.  The config values
        are now defaults applied only when the user explicitly sets them
        (setters, the settings dialog, ``set_config``).  There was no
        push-on-start option to remove: the push was unconditional.
        """
        self.backend.open()
        self._connected = True
        slews = [None, None]
        for axis in range(2):
            name = AXES[axis]
            # -- loop mode ------------------------------------------------- #
            try:
                self._closed[axis] = bool(self.backend.get_closed_loop(axis))
            except Exception as exc:
                self._emit("warn", f"{name}: loop mode not readable ({exc}); "
                                   f"assuming {'CL' if self._closed[axis] else 'OL'} from config")
            setattr(self.cfg.motion, ("closed_loop_x", "closed_loop_y")[axis], self._closed[axis])
            # -- target: the SETPOINT, not the read-out --------------------- #
            # In open loop the read-out differs from the command (hysteresis),
            # so the setpoint is the honest "what am I holding".  Fall back to
            # the measured position if the controller cannot report it.
            target = None
            try:
                target = float(self.backend.read_setpoint(axis))
            except Exception:
                try:
                    target = float(self.backend.read_position(axis))
                except Exception:
                    target = None
            if target is None or target != target:
                self._emit("warn", f"{name}: position not readable at start; target shown as 0")
                target = 0.0
            else:
                # The echo starts at the adopted setpoint, so a scan whose
                # first point is "stay here" can settle.  Unknown -> None.
                self._echo[axis] = target
            self._target[axis] = target
            self._cmd[axis] = target
            # An adopted target outside this mode's travel is REPORTED, not
            # corrected: correcting it would be a move at start.
            lo = self.cfg.limits.travel_min
            hi = travel_max(self.cfg, self._closed[axis])
            if self.cfg.limits.enforce and not (lo - 1e-9 <= target <= hi + 1e-9):
                self._emit("warn", f"{name}: adopted setpoint {target:.4g} um is outside the "
                                   f"{'CL' if self._closed[axis] else 'OL'} travel [{lo:.4g}, {hi:.4g}]; "
                                   f"left as is")
            # -- native slew rate ------------------------------------------ #
            try:
                slews[axis] = max(0.0, float(self.backend.read_slew_rate(axis)))
            except Exception as exc:
                self._emit("warn", f"{name}: slew rate not readable ({exc})")
        self._adopt_slew(slews)

        self._stop.clear()
        self._ramp_thread = threading.Thread(
            target=self._ramp_worker, name="piezo-ramp", daemon=True
        )
        self._ramp_thread.start()
        st = ", ".join(f"{AXES[a]} {'CL' if self._closed[a] else 'OL'} @ {self._target[a]:.4g} um"
                       for a in range(2))
        self._emit("info", f"piezo started ({self.backend.idn()}); adopted {st}, "
                           f"ramp {self.cfg.motion.ramp_mode}")

    def _adopt_slew(self, slews: list) -> None:
        """Turn the controller's slew rates into a consistent ramp mode + velocity.

        The ramp modes need a particular hardware slew rate (see set_velocity):
        "hardware" = slew is the velocity, "software"/"off" = slew 0.  We may
        not WRITE the slew to make the config's mode true, so the mode follows
        what the controller already does:

          * any axis limits its slew (> 0)  -> "hardware", velocity = that slew
            (an axis at 0 then simply has no limit, velocity 0 = jump);
          * no axis limits                  -> the config's "software"/"off" is
            consistent and kept; a configured "hardware" is NOT (the controller
            would jump), so we adopt "software" -- the brain then limits the
            speed at the configured velocity without touching the controller.
          * slews unreadable                -> config kept as it is.
        """
        if any(v is None for v in slews):
            return
        with self._ramp_lock:
            if any(v > 0.0 for v in slews):
                if self.cfg.motion.ramp_mode != "hardware":
                    self._emit("info", "controller slew rate is set -> ramp mode 'hardware' adopted")
                self.cfg.motion.ramp_mode = "hardware"
                for axis in range(2):
                    set_axis_velocity(self.cfg, axis, slews[axis])
                    self._ramp_vel[axis] = slews[axis]
            elif self.cfg.motion.ramp_mode == "hardware":
                self._emit("warn", "controller has no slew-rate limit -> ramp mode 'software' "
                                   "adopted (the brain ramps; the controller is not written)")
                self.cfg.motion.ramp_mode = "software"

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Stop ramping and close the backend.  Idempotent.

        ``keep_outputs`` (shutdown{keep_outputs: true}, a restart for a code
        update) changes nothing here: stopping motion is a SAFETY step that a
        restart keeps (no move may run on unsupervised), and nothing is moved
        back, homed or parked either way -- the next start adopts the position.

        We deliberately leave the piezo where it is rather than forcing it to 0
        -- a surprise full-travel move could crash a sample into the optics.
        """
        if not self._connected:
            return
        self._stop.set()
        if self._ramp_thread is not None:
            self._ramp_thread.join(timeout=1.0)
            self._ramp_thread = None
        with self._ramp_lock:
            self._ramp_active = [False, False]
        try:
            self.backend.close()
        finally:
            self._connected = False
        self._emit("info", "piezo shut down")

    def status(self) -> PiezoStatus:
        """Snapshot of live state.  NEVER raises.

        ORDERING RULE (target echo, 2026-09-28).  Every move stores its echo
        AFTER writing the setpoint to the controller, inside the same
        `_ramp_lock` section that starts the ramp or records the slew time
        (move_axis / _begin_software_ramp).  Here the echo is read FIRST, in
        one locked section together with that ramp / slew state, and the
        position read-out comes AFTER.  So a frame carrying a new target always
        has (a) the ramp / slew state of THAT move and (b) a position read
        after its setpoint was written -- it can never pair the new target with
        a `moving = False` computed from before the move.

        `moving`, per axis:
          * the software ramp is still walking the setpoint -> True (until
            the LAST step is written, Lukas's rule);
          * closed loop -> the sensor is true, so "there" = |read-out -
            target| <= SETTLE_TOL (also after a software ramp: the servo may
            still be catching up with the last step); an unreadable sensor
            counts as moving;
          * open loop -> the read-out is off by the hysteresis for good, so
            the controller's expected slew time decides (_hw_eta).
        """
        with self._ramp_lock:                 # FIRST: echo + the move's state
            echo = list(self._echo)
            ramping = list(self._ramp_active)
            vel = list(self._ramp_vel)
            eta = list(self._hw_eta)
            target = list(self._target)
        pos, failed = self._read_positions()  # AFTER: see the ordering rule
        now = time.monotonic()
        moving = []
        for a in range(2):
            if ramping[a]:
                moving.append(True)
            elif self._closed[a]:
                p = pos[a]
                moving.append(failed or p != p or abs(p - target[a]) > SETTLE_TOL)
            else:
                moving.append(now < eta[a])
        rel_origin = [axis_rel_origin(self.cfg, a) for a in range(2)]
        relative = [pos[a] - rel_origin[a] for a in range(2)]
        return PiezoStatus(
            position=pos,
            target=target,
            relative=relative,
            rel_origin=rel_origin,
            moving=moving,
            closed_loop=list(self._closed),
            velocity=vel,
            travel_max=[travel_max(self.cfg, self._closed[a]) for a in range(2)],
            ramp_mode=self.cfg.motion.ramp_mode,
            connected=self._connected,
            target_um=echo,
            hw_error=self._hw_error,
        )

    def _read_positions(self):
        """Read both axes; on failure report LOUDLY (Lukas, 2026-09-28).

        Before, a failed read published NaN, and NaN made a closed-loop axis
        report `moving = False` -- a healthy-looking stage "at rest".  Now the
        last good read-out is kept, `hw_error` carries the message, and ONE
        error event goes out per failure episode (status runs at 8 Hz; one
        event per frame would bury the log).  Returns (positions, failed).
        """
        try:
            pos = [float(self.backend.read_position(a)) for a in range(2)]
        except Exception as exc:  # noqa: BLE001 -- status must never raise
            msg = f"{type(exc).__name__}: {exc}"
            with self._err_lock:
                first = not self._hw_error
                self._hw_error = msg
                pos = list(self._last_pos)
            if first:
                self._emit("error", f"piezo: position read failed ({msg}); "
                                    f"showing the last good read-out")
            return pos, True
        with self._err_lock:
            self._last_pos = list(pos)
            recovered = bool(self._hw_error)
            self._hw_error = ""
        if recovered:
            self._emit("info", "piezo: position reads recovered")
        return pos, False

    # ------------------------------------------------------------------ #
    # clamping helpers
    # ------------------------------------------------------------------ #
    def _clamp_axis(self, axis: int, value: float) -> float:
        """Clamp a target to [travel_min, travel_max(mode)] for this axis."""
        value = float(value)
        if not self.cfg.limits.enforce:
            return value
        lo = self.cfg.limits.travel_min
        hi = travel_max(self.cfg, self._closed[axis])
        if value < lo:
            self._emit("warn", f"{AXES[axis]} target {value:.4g} clamped to {lo:.4g} um")
            return lo
        if value > hi:
            mode = "CL" if self._closed[axis] else "OL"
            self._emit("warn", f"{AXES[axis]} target {value:.4g} clamped to {hi:.4g} um ({mode} travel)")
            return hi
        return value

    def _clamp_velocity(self, value: float) -> float:
        value = max(0.0, float(value))
        if self.cfg.limits.enforce and value > self.cfg.limits.max_velocity:
            self._emit("warn", f"velocity {value:.4g} clamped to {self.cfg.limits.max_velocity:.4g} um/s")
            return self.cfg.limits.max_velocity
        return value

    # ------------------------------------------------------------------ #
    # the software ramp
    # ------------------------------------------------------------------ #
    def _ramp_worker(self) -> None:
        """Walk each active axis' setpoint toward its target at its velocity.

        Runs at ``cfg.motion.ramp_hz``.  Only does anything in "software" ramp
        mode; in "hardware"/"off" mode moves write the setpoint directly and no
        axis is ever marked active, so this loop just idles.
        """
        while not self._stop.is_set():
            # ramp_hz arrives through set_config exactly as the client sent it
            # (no type cast), so a bad value must not raise here: an exception
            # would end this thread silently and every later software move
            # would report `moving` forever (deep cleaning 2026-09-28).
            try:
                hz = max(1.0, float(self.cfg.motion.ramp_hz))
            except (TypeError, ValueError):
                hz = 50.0
            period = 1.0 / hz
            for axis in range(2):
                failed = None
                # The WRITE happens inside the lock too (deep cleaning
                # 2026-09-28).  Before, the setpoint was computed under the
                # lock and written after releasing it; a stop or a direct move
                # in that gap was then overwritten by this stale ramp value,
                # leaving the stage at the old ramp point while `target` showed
                # the new one.  Holding the lock over one short serial write
                # makes "cancel the ramp + write" in the other verbs atomic.
                with self._ramp_lock:
                    if not self._ramp_active[axis]:
                        continue
                    vel = max(0.0, self._ramp_vel[axis])
                    frm = self._ramp_from[axis]
                    to = self._ramp_to[axis]
                    dt = time.monotonic() - self._ramp_t0[axis]
                    distance = to - frm
                    if vel <= 0.0:
                        travelled = abs(distance)  # vel 0 -> jump
                    else:
                        travelled = vel * dt
                    if travelled >= abs(distance):
                        setpoint = to
                        self._ramp_active[axis] = False
                    else:
                        direction = 1.0 if distance >= 0 else -1.0
                        setpoint = frm + direction * travelled
                    try:
                        self.backend.set_setpoint(axis, setpoint)
                        self._cmd[axis] = setpoint
                    except Exception as exc:
                        self._ramp_active[axis] = False
                        # The ramp died short of its target.  If the echo kept
                        # the target, a waiting scan would see moving=False and
                        # take a point the drive never reached.  Echo where the
                        # drive IS (the last step written): the scan then times
                        # out instead of recording the wrong place.
                        self._echo[axis] = self._cmd[axis]
                        failed = exc
                if failed is not None:
                    # Emitted outside the lock: a listener must never run
                    # while we hold it.
                    self._emit("error", f"{AXES[axis]} ramp write failed: {failed}")
            self._stop.wait(period)

    def _begin_software_ramp(self, axis: int, target: float, echo: float) -> None:
        # Start from the setpoint we last WROTE, not from the read-out.  In
        # software mode the controller's own slew is 0, so the drive sits
        # exactly at that setpoint (also mid-ramp: it is the last ramp step).
        # The read-out is not the drive in open loop (hysteresis): starting
        # there first stepped the drive by the OL error -- on the sim's Y axis
        # 1.35 um BACKWARDS at the start of a forward move.
        with self._ramp_lock:
            current = self._cmd[axis]
            self._ramp_from[axis] = current
            self._ramp_to[axis] = target
            self._ramp_t0[axis] = time.monotonic()
            self._ramp_active[axis] = abs(target - current) > 1e-9
            if not self._ramp_active[axis]:
                # Already there -> write once so the backend setpoint matches.
                self.backend.set_setpoint(axis, target)
                self._cmd[axis] = target
            # Echo LAST, in the same locked section that armed the ramp: a
            # frame with this target then always sees the ramp running (or
            # finished) -- never the state from before this move.
            self._echo[axis] = echo

    # ------------------------------------------------------------------ #
    # motion verbs
    # ------------------------------------------------------------------ #
    def move_axis(self, axis: int, position: float) -> float:
        """Move ONE axis to an absolute position (um).  Fire-and-forget.

        The path taken depends on ``cfg.motion.ramp_mode``:
          * "software" -> hand the target to the ramp thread (timed steps),
          * "hardware" -> write the setpoint once; the controller's slew-rate
            limiter enforces the speed,
          * "off"      -> write the setpoint once; the piezo goes as fast as it
            can.
        Returns the clamped target actually commanded.
        """
        target = self._clamp_axis(axis, position)
        self._target[axis] = target
        # The echo is the target as the controller is told: the request
        # itself, unrounded, unless the travel limits clamped it.  ORDER: the
        # setpoint write (or the ramp start) FIRST, the echo AFTER, in the same
        # locked section -- see the ordering rule in status().
        if self.cfg.motion.ramp_mode == "software":
            self._begin_software_ramp(axis, target, echo=target)
        else:
            # Cancel any leftover software ramp, then command directly -- in
            # ONE locked section, so a ramp step cannot land after our write.
            with self._ramp_lock:
                self._ramp_active[axis] = False
                self.backend.set_setpoint(axis, target)
                self._direct_write(axis, target)
                self._echo[axis] = target      # AFTER the write
        self._emit("info", f"move {AXES[axis]} -> {target:.4g} um")
        return target

    def move_xy(self, x: float, y: float) -> list:
        """Move both axes to an absolute (x, y) point, um."""
        return [self.move_axis(0, x), self.move_axis(1, y)]

    def move_relative(self, axis: int, value: float) -> float:
        """Move to ``value`` measured from the relative (zeroed) origin.

        (device target = relative origin + value, then clamped to travel.)
        After ``set_zero``, ``move_relative(axis, 0)`` returns to the zero point.
        """
        device_target = axis_rel_origin(self.cfg, axis) + float(value)
        self._emit("info", f"move {AXES[axis]} -> {value:.4g} um (relative)")
        return self.move_axis(axis, device_target)

    def _direct_write(self, axis: int, target: float) -> None:
        """Book-keeping after a direct (non-ramped) setpoint write.

        Call with _ramp_lock held, right after ``backend.set_setpoint``.
        Records the command and when an open-loop move should be done: the
        controller slews at the velocity in "hardware" mode and jumps in
        "off" mode.  If a previous move is still slewing, its remaining
        distance is added, so the estimate errs on the long side.
        """
        now = time.monotonic()
        rate = self._ramp_vel[axis] if self.cfg.motion.ramp_mode == "hardware" else 0.0
        if rate > 0.0:
            left_um = max(0.0, self._hw_eta[axis] - now) * rate
            dist = abs(target - self._cmd[axis]) + left_um
            self._hw_eta[axis] = now + dist / rate
        else:
            self._hw_eta[axis] = now
        self._cmd[axis] = target

    def stop(self, axis: int) -> None:
        """Freeze an axis where the drive is now.

        Where IS the drive?  (deep cleaning 2026-09-28 -- this used to write
        the READ-OUT back as the setpoint, which in open loop is off the drive
        by the hysteresis: STOP on a resting OL axis moved it ~1.7 um.)
          * no hardware slew (software / off mode), or at rest: the drive is
            at the last setpoint we wrote -> hold that, nothing moves;
          * mid-move in "hardware" mode: the controller's slew limiter is
            somewhere between; the read-out is the best estimate (exact in
            closed loop, off by the hysteresis in open loop).
        """
        with self._ramp_lock:
            ramping = self._ramp_active[axis]
            self._ramp_active[axis] = False
            hold = self._cmd[axis]
        if (not ramping and self.cfg.motion.ramp_mode == "hardware"
                and self.status().moving[axis]):
            try:
                p = float(self.backend.read_position(axis))
                if p == p:
                    hold = p
            except Exception:
                pass
        self._target[axis] = hold
        with self._ramp_lock:
            self._ramp_active[axis] = False
            try:
                self.backend.set_setpoint(axis, hold)
                self._cmd[axis] = hold
            except Exception:
                pass
            self._hw_eta[axis] = time.monotonic()
            # After STOP the axis is not at the requested target.  Keeping
            # that target as the echo would let a scan waiting for it "arrive"
            # as soon as moving is False; echo the held position instead (a
            # waiting scan then times out, loudly).
            self._echo[axis] = self._cmd[axis]
        self._emit("warn", f"stop {AXES[axis]} at {hold:.4g} um")

    def stop_all(self) -> None:
        for axis in range(2):
            self.stop(axis)
        self._emit("warn", "STOP all axes")

    # ------------------------------------------------------------------ #
    # loop mode
    # ------------------------------------------------------------------ #
    def set_closed_loop(self, axis: int, enabled: bool, _quiet: bool = False) -> bool:
        """Switch an axis between closed loop (servo on) and open loop.

        Switching mode changes the usable travel (CL is smaller), so we
        re-clamp the current target to the new ceiling and, if it moved, command
        the corrected position.
        """
        enabled = bool(enabled)
        self.backend.set_closed_loop(axis, enabled)
        self._closed[axis] = enabled
        # Keep the config in step with the live mode, so a set_config of some
        # other group (which ends in apply_config) re-applies THIS mode instead
        # of flipping the axis back to a stale default.
        setattr(self.cfg.motion, ("closed_loop_x", "closed_loop_y")[axis], enabled)
        if not _quiet:
            self._emit("info", f"{AXES[axis]} -> {'CLOSED' if enabled else 'OPEN'} loop")
        # Re-clamp the standing target to the (possibly smaller) travel.
        clamped = self._clamp_axis(axis, self._target[axis])
        if abs(clamped - self._target[axis]) > 1e-9:
            self.move_axis(axis, clamped)
        return enabled

    # ------------------------------------------------------------------ #
    # velocity
    # ------------------------------------------------------------------ #
    def set_velocity(self, axis: int, velocity: float, _quiet: bool = False) -> float:
        """Set the motion velocity (um/s) for an axis.

        Stored in config, tracked for the software ramp, AND pushed to the
        controller's native slew-rate limiter so both ramp modes honour it.
        """
        v = self._clamp_velocity(velocity)
        set_axis_velocity(self.cfg, axis, v)
        with self._ramp_lock:
            now = time.monotonic()
            if self._ramp_active[axis]:
                # Re-anchor a running ramp at the point it has reached (deep
                # cleaning 2026-09-28).  The ramp computes from + v*(now - t0);
                # changing v without moving the anchor made the setpoint JUMP
                # to where the new speed "would have been" (10 -> 20 um/s
                # after 1 s: a 10 um step in one tick).
                self._ramp_from[axis] = self._cmd[axis]
                self._ramp_t0[axis] = now
            # A direct move still slewing: keep its expected end honest.
            old = self._ramp_vel[axis]
            if old > 0.0 and self._hw_eta[axis] > now:
                left_um = (self._hw_eta[axis] - now) * old
                self._hw_eta[axis] = now + (left_um / v if v > 0.0 else 0.0)
            self._ramp_vel[axis] = v
        # Only "hardware" mode uses the controller's native slew-rate limiter.
        # In "software" mode WE step the setpoint, so the hardware must follow
        # instantly (rate 0) or it would double-limit and lag our ramp; "off"
        # means no limiting at all.  Hence hardware-rate = velocity only here.
        hw_rate = v if self.cfg.motion.ramp_mode == "hardware" else 0.0
        try:
            self.backend.set_slew_rate(axis, hw_rate)
        except Exception as exc:
            self._emit("warn", f"{AXES[axis]} slew-rate set failed: {exc}")
        if not _quiet:
            self._emit("info", f"{AXES[axis]} velocity = {v:.4g} um/s")
        return v

    def set_ramp_mode(self, mode: str) -> str:
        """Choose how velocity is applied: 'hardware' | 'software' | 'off'."""
        from .config import RAMP_MODES
        if mode not in RAMP_MODES:
            raise ValueError(f"bad ramp mode {mode!r}; use one of {RAMP_MODES}")
        self.cfg.motion.ramp_mode = mode
        # Re-push velocities so the hardware slew rate matches the new mode.
        for axis in range(2):
            self.set_velocity(axis, axis_velocity(self.cfg, axis), _quiet=True)
        self._emit("info", f"ramp mode = {mode}")
        return mode

    # ------------------------------------------------------------------ #
    # relative-frame verbs ("zero here" + relative moves)
    # ------------------------------------------------------------------ #
    def set_zero(self, axis: int) -> float:
        """Define the CURRENT position of ``axis`` as its relative zero."""
        current = self.backend.read_position(axis)
        set_axis_rel_origin(self.cfg, axis, current)
        self._emit("info", f"{AXES[axis]} zeroed here (origin = {current:.4g} um)")
        return current

    def set_zero_all(self) -> list:
        origins = [self.set_zero(a) for a in range(2)]
        self._emit("info", "all axes zeroed at current position")
        return origins

    def clear_zero(self, axis: int) -> None:
        set_axis_rel_origin(self.cfg, axis, 0.0)
        self._emit("info", f"{AXES[axis]} relative origin cleared (back to absolute)")

    def clear_zero_all(self) -> None:
        for axis in range(2):
            set_axis_rel_origin(self.cfg, axis, 0.0)
        self._emit("info", "relative origins cleared")

    # ------------------------------------------------------------------ #
    # position list
    # ------------------------------------------------------------------ #
    def store_position(self, slot: int, name: str = "") -> dict:
        """Capture the CURRENT measured position into a slot."""
        pos = [self.backend.read_position(a) for a in range(2)]
        p = self.positions.store(slot, pos[0], pos[1], name=name)
        self._emit("info", f"stored slot {slot} '{p.name}' = ({pos[0]:.4g}, {pos[1]:.4g})")
        from dataclasses import asdict
        return asdict(p)

    def clear_position(self, slot: int) -> None:
        self.positions.clear(slot)
        self._emit("info", f"cleared slot {slot}")

    def goto_position(self, slot: int) -> list:
        """Drive both axes to a saved slot (device coordinates)."""
        p = self.positions.get(slot)
        if not p.used:
            self._emit("warn", f"slot {slot} is empty")
            return []
        targets = [self.move_axis(0, p.x), self.move_axis(1, p.y)]
        self._emit("info", f"go to slot {slot} '{p.name}'")
        return targets

    def save_positions(self, path: str) -> None:
        self.positions.save(path)
        self._emit("info", f"saved positions -> {path}")

    def load_positions(self, path: str) -> None:
        self.positions.load(path)
        self._emit("info", f"loaded positions <- {path}")

    def get_positions(self) -> list:
        return self.positions.to_list()

    # ------------------------------------------------------------------ #
    # config
    # ------------------------------------------------------------------ #
    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-push loop mode / velocity / channels after the config was edited.

        Called after a local settings-dialog edit or a remote ``set_config``.
        """
        # Re-resolve channels (swap_xy may have flipped).
        try:
            self.backend._channels = [axis_channel(self.cfg, 0), axis_channel(self.cfg, 1)]
        except Exception:
            pass
        if self.cfg.motion.ramp_mode not in ("hardware", "software", "off"):
            self.cfg.motion.ramp_mode = "software"
        for axis in range(2):
            self.set_closed_loop(axis, axis_closed_loop_default(self.cfg, axis), _quiet=True)
            self.set_velocity(axis, axis_velocity(self.cfg, axis), _quiet=True)
        self._emit("info", "config applied")
