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
from dataclasses import dataclass

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
# "settled" for a hardware/off-mode move (software moves track the ramp flag).
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
            self._target[axis] = target
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

    def shutdown(self) -> None:
        """Stop ramping and close the backend.  Idempotent.

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
        """Snapshot of live state.  NEVER raises."""
        try:
            pos = [self.backend.read_position(a) for a in range(2)]
        except Exception:
            pos = [float("nan")] * 2
        with self._ramp_lock:
            ramping = list(self._ramp_active)
            vel = list(self._ramp_vel)
        target = list(self._target)
        moving = []
        for a in range(2):
            if self.cfg.motion.ramp_mode == "software":
                moving.append(ramping[a])
            else:
                p = pos[a]
                moving.append(False if p != p else abs(p - target[a]) > SETTLE_TOL)
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
        )

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
            hz = max(1.0, float(self.cfg.motion.ramp_hz))
            period = 1.0 / hz
            for axis in range(2):
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
                except Exception as exc:
                    self._emit("error", f"{AXES[axis]} ramp write failed: {exc}")
                    with self._ramp_lock:
                        self._ramp_active[axis] = False
            self._stop.wait(period)

    def _begin_software_ramp(self, axis: int, target: float) -> None:
        try:
            current = self.backend.read_position(axis)
        except Exception:
            current = self._target[axis]
        if current != current:  # NaN guard
            current = self._target[axis]
        with self._ramp_lock:
            self._ramp_from[axis] = current
            self._ramp_to[axis] = target
            self._ramp_t0[axis] = time.monotonic()
            self._ramp_active[axis] = abs(target - current) > 1e-9
        if not self._ramp_active[axis]:
            # Already there -> write once so the backend setpoint matches.
            self.backend.set_setpoint(axis, target)

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
        if self.cfg.motion.ramp_mode == "software":
            self._begin_software_ramp(axis, target)
        else:
            # Cancel any leftover software ramp, then command directly.
            with self._ramp_lock:
                self._ramp_active[axis] = False
            self.backend.set_setpoint(axis, target)
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

    def stop(self, axis: int) -> None:
        """Freeze an axis at its current measured position."""
        try:
            current = self.backend.read_position(axis)
        except Exception:
            current = self._target[axis]
        with self._ramp_lock:
            self._ramp_active[axis] = False
        self._target[axis] = current
        try:
            self.backend.set_setpoint(axis, current)
        except Exception:
            pass
        self._emit("warn", f"stop {AXES[axis]} at {current:.4g} um")

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
