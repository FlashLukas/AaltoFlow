"""The brain: :class:`Stage` (§5 of the guide, set-and-forget family).

A 3-axis coarse stage is *set-and-forget*: you command target positions and the
BSC203 servos the motors there on its own -- there is no control loop for us to
run (contrast the closed-loop magnet).  So this brain has no threads and no
state machine.  It:

  * holds the DESIRED state and the config,
  * clamps every request to the safety envelope (``cfg.limits``),
  * pushes accepted values to the backend,
  * owns the coordinate math (offsets + 2x2 transform),
  * owns the 20-slot position list,
  * reports a :class:`StageStatus` snapshot that must NEVER throw.

Motion is fire-and-forget: ``move_*``/``home`` return once the command is
accepted; the caller polls ``status()`` to watch it settle.

Coordinate frames
-----------------
  device (x,y,z)   raw motor positions the controller uses; authoritative.
  logical (u,v,w)  user frame.  Relationship:

      device_x = m00*u + m01*v + off_x
      device_y = m10*u + m11*v + off_y
      device_z =                 w + off_z

  i.e. logical -> device applies the 2x2 matrix M to XY then adds the offset.
  device -> logical subtracts the offset then applies M^-1.  Z is offset-only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .backends.base import StageBackend
from .config import (
    Config,
    axis_acceleration,
    axis_limits,
    axis_offset,
    axis_rel_origin,
    axis_velocity,
    matrix_is_invertible,
    matrix_tuple,
    set_axis_acceleration,
    set_axis_offset,
    set_axis_rel_origin,
    set_axis_velocity,
)
from .positions import PositionList

AXES = ("X", "Y", "Z")


@dataclass
class StageStatus:
    """A snapshot of everything the GUI / remote client needs.

    Lists are length-3, axis-indexed (X, Y, Z).  ``status()`` guarantees this
    object is always returned, even if a hardware read fails mid-way.
    """

    position: list      # device coordinates, mm
    logical: list       # logical coordinates (u, v, w), mm
    relative: list      # position measured from the relative (zeroed) origin, mm
    rel_origin: list    # the current relative origin per axis, device mm
    moving: list        # bool per axis
    homed: list         # bool per axis
    velocity: list      # mm/s per axis
    acceleration: list  # mm/s^2 per axis
    offsets: list       # mm per axis
    matrix: list        # [m00, m01, m10, m11]
    connected: bool
    # The target AS REQUESTED by the last move of each axis, in DEVICE mm --
    # the coordinates of the `position_x/y/z` controls it settles (see
    # describe.py).  None = no requested target (after `home`, or when the
    # position could not be read at start).  Deliberately None and not NaN:
    # scan-core's adopt check is abs(float(sp) - target) > tol, which is
    # False for NaN, so a NaN echo would count as "adopted".
    target_mm: list = field(default_factory=lambda: [None, None, None])
    # "" while the controller answers; the error text while its reads fail.
    # The values above are then the LAST GOOD ones and `moving` is True
    # (unknown is not "at rest").  scan-core pauses a scan on a non-empty one.
    hw_error: str = ""


class Stage:
    def __init__(self, backend: StageBackend, cfg: Config):
        self.backend = backend
        self.cfg = cfg
        self.positions = PositionList()
        self._connected = False
        # Target echo (Lukas, 2026-09-28), device mm per axis; see StageStatus.
        # Written only AFTER the hardware move was issued (see move_axis).
        self._echo: list = [None, None, None]
        # Last good hardware reads, kept for a failed read (hw_error), and the
        # "an error episode is running" text that limits error events to one
        # per episode.  _status_lock: status() is called by the publisher AND
        # by the command thread (the `status` verb); both touch these.
        self._last_good: dict | None = None
        self._hw_error = ""
        self._status_lock = threading.Lock()
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
    # coordinate transforms
    # ------------------------------------------------------------------ #
    def device_from_logical(self, u: float, v: float, w: float) -> tuple[float, float, float]:
        m00, m01, m10, m11 = matrix_tuple(self.cfg)
        ox, oy, oz = (axis_offset(self.cfg, a) for a in range(3))
        x = m00 * u + m01 * v + ox
        y = m10 * u + m11 * v + oy
        z = w + oz
        return x, y, z

    def logical_from_device(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        m00, m01, m10, m11 = matrix_tuple(self.cfg)
        ox, oy, oz = (axis_offset(self.cfg, a) for a in range(3))
        dx, dy = x - ox, y - oy
        ok, det = matrix_is_invertible(m00, m01, m10, m11)
        if not ok:
            # Backstop: a singular matrix should never reach here (set_matrix and
            # config-apply both reject/sanitise it), but if one somehow does we
            # fall back to identity on XY rather than divide by (near) zero.
            u, v = dx, dy
        else:
            u = (m11 * dx - m01 * dy) / det
            v = (-m10 * dx + m00 * dy) / det
        w = z - oz
        return u, v, w

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Open the backend and ADOPT what the controller is already doing.

        Lukas's rule (2026-09-27, every module): starting the software must not
        change the instrument.  So start() only READS the BSC203 -- positions,
        homed flags, velocity and acceleration -- and never writes a motion
        parameter.  The velocity/acceleration it finds are copied INTO the
        config (``cfg.motion``), so the Settings dialog and ``get_config`` show
        what the motors will really do; the .ini values in ``motion`` are now
        only defaults that reach the controller when YOU set them (set_velocity,
        set_acceleration, Settings > OK, set_config).

        Before 2026-09-27 this pushed cfg.motion to every axis at start, which
        silently overwrote whatever Kinesis or a previous session had set.

        ``motion.home_on_start`` (default False) is the one deliberate
        exception: homing is a state change, so it only happens when someone
        has explicitly switched it on in the .ini.
        """
        self.backend.open()
        self._connected = True
        self._ensure_matrix_ok()  # a config file could carry a singular matrix
        self._adopt_motion_params()
        self._adopt_targets()
        if self.cfg.motion.home_on_start:
            self.home_all()
        self._emit("info", f"stage started ({self.backend.idn()})")

    def _adopt_targets(self) -> None:
        """Start the target echo at where each axis IS (a read, no move).

        Without it the echo would say None until the first move, and a scan
        whose first point is the current position could not settle.  An axis
        still moving at start (a Kinesis move left running) has no target we
        know of, so it stays None.
        """
        for axis in range(3):
            try:
                if self.backend.is_moving(axis):
                    continue
                self._echo[axis] = float(self.backend.read_position(axis))
            except Exception as exc:  # noqa: BLE001 -- a read must not stop start()
                self._emit("warn", f"{AXES[axis]}: position not readable at start ({exc})")

    def _adopt_motion_params(self) -> None:
        """Copy the controller's velocity/acceleration into cfg.motion (reads only).

        If the controller holds a value above our safety ceiling we REPORT it
        (a warn event) rather than correct it: correcting would be a write at
        start.  The ceiling still applies to every value someone sets later.
        A failed read keeps the config default for that axis and says so.
        """
        lim = self.cfg.limits
        for axis in range(3):
            try:
                v = float(self.backend.read_velocity(axis))
                a = float(self.backend.read_acceleration(axis))
            except Exception as exc:  # noqa: BLE001 -- a read must not stop start()
                self._emit("warn", f"{AXES[axis]}: could not read motion parameters "
                                   f"({exc}); showing config defaults")
                continue
            set_axis_velocity(self.cfg, axis, v)
            set_axis_acceleration(self.cfg, axis, a)
            if lim.enforce and v > lim.max_velocity:
                self._emit("warn", f"{AXES[axis]} velocity on the controller ({v:.4g} mm/s) "
                                   f"is above limits.max_velocity ({lim.max_velocity:.4g}); left as is")
            if lim.enforce and a > lim.max_acceleration:
                self._emit("warn", f"{AXES[axis]} acceleration on the controller ({a:.4g} mm/s^2) "
                                   f"is above limits.max_acceleration ({lim.max_acceleration:.4g}); left as is")

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Stop all motion and close the backend.  Idempotent.

        ``keep_outputs`` (shutdown{keep_outputs: true}, a restart for a code
        update) changes nothing here: stopping motion is a SAFETY step that a
        restart keeps (no move may run on unsupervised), and nothing is moved
        back, homed or parked either way -- the next start adopts the position.
        """
        if not self._connected:
            return
        try:
            for axis in range(3):
                try:
                    self.backend.stop(axis)
                except Exception:
                    pass
        finally:
            self.backend.close()
            self._connected = False
        self._emit("info", "stage shut down")

    def status(self) -> StageStatus:
        """Snapshot of live state.  NEVER raises.

        ORDERING RULE (target echo, 2026-09-28): the echo is read FIRST, the
        hardware (`moving` above all) AFTER.  The setter does the reverse:
        hardware move first, echo after (move_axis).  Together: whenever this
        snapshot carries a new target, the move for it was already on the wire
        when `moving` was read, so the frame cannot pair the new target with a
        `moving = False` from before the move -- the stale frame that let a
        scan "arrive" at once, at the old position.  (Read the other way round,
        a move landing between the two reads would produce exactly that.)
        # VERIFY on the BSC203: that is_moving() sent right after move_to
        # already reports the move (the controller should answer the status
        # request only after it processed the move sent before it on the same
        # USB link).
        """
        with self._status_lock:
            echo = list(self._echo)            # FIRST -- see the ordering rule
            try:
                pos = [float(self.backend.read_position(a)) for a in range(3)]
                moving = [bool(self.backend.is_moving(a)) for a in range(3)]
                homed = [bool(self.backend.is_homed(a)) for a in range(3)]
                vel = [float(self.backend.read_velocity(a)) for a in range(3)]
                acc = [float(self.backend.read_acceleration(a)) for a in range(3)]
            except Exception as exc:  # noqa: BLE001 -- status must never raise
                pos, moving, homed, vel, acc = self._failed_read(exc)
            else:
                self._last_good = {"pos": pos, "homed": homed, "vel": vel, "acc": acc}
                if self._hw_error:
                    self._hw_error = ""
                    self._emit("info", "stage: hardware reads recovered")
            hw_error = self._hw_error
        logical = list(self.logical_from_device(*pos))
        rel_origin = [axis_rel_origin(self.cfg, a) for a in range(3)]
        relative = [pos[a] - rel_origin[a] for a in range(3)]
        return StageStatus(
            position=pos,
            logical=logical,
            relative=relative,
            rel_origin=rel_origin,
            moving=moving,
            homed=homed,
            velocity=vel,
            acceleration=acc,
            offsets=[axis_offset(self.cfg, a) for a in range(3)],
            matrix=list(matrix_tuple(self.cfg)),
            connected=self._connected,
            target_mm=echo,
            hw_error=hw_error,
        )

    def _failed_read(self, exc: Exception):
        """What status() reports while the controller does not answer.

        Loud, not quiet (Lukas, 2026-09-28).  This used to publish NaN
        positions, velocity 0 and `moving = False` -- a stage "at rest" that
        looked healthy, on which a scan would happily settle.  Now:
          * hw_error carries the message (scan-core pauses on it),
          * position / homed / velocity / acceleration are the LAST GOOD reads
            (NaN only if there never was one),
          * moving is True: we do not know, and False means "arrived",
          * ONE error event per episode, not one per status frame (8 Hz).
        Call with _status_lock held.
        """
        msg = f"{type(exc).__name__}: {exc}"
        if not self._hw_error:
            self._emit("error", f"stage: hardware read failed ({msg}); "
                                f"showing the last good values")
        self._hw_error = msg
        lg = self._last_good
        if lg is None:
            nan = float("nan")
            return [nan] * 3, [True] * 3, [False] * 3, [nan] * 3, [nan] * 3
        return (list(lg["pos"]), [True] * 3, list(lg["homed"]),
                list(lg["vel"]), list(lg["acc"]))

    # ------------------------------------------------------------------ #
    # clamping helpers
    # ------------------------------------------------------------------ #
    def _clamp_axis(self, axis: int, value: float) -> float:
        if not self.cfg.limits.enforce:
            return value
        lo, hi = axis_limits(self.cfg, axis)
        if value < lo:
            self._emit("warn", f"{AXES[axis]} target {value:.4g} clamped to {lo:.4g} mm")
            return lo
        if value > hi:
            self._emit("warn", f"{AXES[axis]} target {value:.4g} clamped to {hi:.4g} mm")
            return hi
        return value

    def _clamp_param(self, value: float, ceiling: float, what: str) -> float:
        value = max(0.0, float(value))
        if self.cfg.limits.enforce and value > ceiling:
            self._emit("warn", f"{what} {value:.4g} clamped to {ceiling:.4g}")
            return ceiling
        return value

    # ------------------------------------------------------------------ #
    # motion verbs (device frame)
    # ------------------------------------------------------------------ #
    def move_axis(self, axis: int, position: float) -> float:
        """Move ONE motor to an absolute DEVICE position (mm).

        Every move verb (relative, logical, go-to-slot) ends here, so this is
        the one place that stores the target echo.  ORDER MATTERS: the move
        goes to the hardware FIRST and the echo is stored AFTER (see status()).
        The echo is the target as the hardware was told: the request itself,
        unrounded, unless the travel limits clamped it.
        """
        target = self._clamp_axis(axis, float(position))
        self.backend.move_to(axis, target)
        self._echo[axis] = target              # AFTER the hardware move
        self._emit("info", f"move {AXES[axis]} -> {target:.4g} mm")
        return target

    def move_logical(self, u: float, v: float, w: float) -> list:
        """Move to a LOGICAL point -- converted through offsets + transform."""
        device = self.device_from_logical(u, v, w)
        targets = [self.move_axis(a, device[a]) for a in range(3)]
        return targets

    def home(self, axis: int) -> None:
        self.backend.home(axis)
        # Homing is not a requested position: clear the echo, so a frame from
        # before the home can never match a later request.
        self._echo[axis] = None
        self._emit("info", f"homing {AXES[axis]}")

    def home_all(self) -> None:
        for axis in range(3):
            self.backend.home(axis)
            self._echo[axis] = None
        self._emit("info", "homing all axes")

    def stop(self, axis: int) -> None:
        self.backend.stop(axis)
        self._echo_where_stopped(axis)
        self._emit("warn", f"stop {AXES[axis]}")

    def stop_all(self) -> None:
        for axis in range(3):
            self.backend.stop(axis)
            self._echo_where_stopped(axis)
        self._emit("warn", "STOP all axes")

    def _echo_where_stopped(self, axis: int) -> None:
        """After STOP the axis is NOT at the requested target.  If the echo kept
        that target, a scan waiting for it would see `moving = False` and take
        the point as arrived.  So the echo becomes where the axis stopped (a
        waiting scan then times out, loudly), or None if that is unreadable."""
        try:
            self._echo[axis] = float(self.backend.read_position(axis))
        except Exception:  # noqa: BLE001
            self._echo[axis] = None

    # ------------------------------------------------------------------ #
    # parameter verbs
    # ------------------------------------------------------------------ #
    def set_velocity(self, axis: int, velocity: float) -> float:
        v = self._clamp_param(velocity, self.cfg.limits.max_velocity, f"{AXES[axis]} velocity")
        set_axis_velocity(self.cfg, axis, v)
        self.backend.set_velocity(axis, v)
        self._emit("info", f"{AXES[axis]} velocity = {v:.4g} mm/s")
        return v

    def set_acceleration(self, axis: int, acceleration: float) -> float:
        a = self._clamp_param(acceleration, self.cfg.limits.max_acceleration, f"{AXES[axis]} accel")
        set_axis_acceleration(self.cfg, axis, a)
        self.backend.set_acceleration(axis, a)
        self._emit("info", f"{AXES[axis]} acceleration = {a:.4g} mm/s^2")
        return a

    # ------------------------------------------------------------------ #
    # coordinate-frame verbs
    # ------------------------------------------------------------------ #
    def set_offset(self, axis: int, value: float) -> None:
        set_axis_offset(self.cfg, axis, float(value))
        self._emit("info", f"{AXES[axis]} offset = {float(value):.4g} mm")

    def set_matrix(self, m00: float, m01: float, m10: float, m11: float) -> None:
        """Set the 2x2 XY transform, REJECTING a non-invertible matrix.

        A singular (or numerically unusable) matrix can't be inverted for the
        device->logical read-out, so we refuse it and keep the previous matrix
        instead of installing something that would break the coordinate math.
        Raises ValueError; over the wire this becomes an {"ok": false} reply and
        in the GUI it lands in the log as an error.
        """
        m00, m01, m10, m11 = float(m00), float(m01), float(m10), float(m11)
        ok, det = matrix_is_invertible(m00, m01, m10, m11)
        if not ok:
            self._emit("error", f"rejected singular transform matrix (det={det:.3g}); keeping previous")
            raise ValueError(f"transform matrix is not invertible (det={det:.3g}); not applied")
        t = self.cfg.transform
        t.m00, t.m01, t.m10, t.m11 = m00, m01, m10, m11
        self._emit("info", f"transform matrix set (det={det:.4g})")

    def _ensure_matrix_ok(self) -> bool:
        """Guarantee the active transform is invertible; reset to identity if not.

        Used on start() and after a config load / remote set_config, where a
        matrix could arrive from an INI file or a coordinator without going
        through set_matrix().  Returns True if the matrix was already fine.
        """
        ok, det = matrix_is_invertible(*matrix_tuple(self.cfg))
        if not ok:
            t = self.cfg.transform
            t.m00, t.m01, t.m10, t.m11 = 1.0, 0.0, 0.0, 1.0
            self._emit("error", f"transform matrix was singular (det={det:.3g}); reset to identity")
        return ok

    # ------------------------------------------------------------------ #
    # relative-frame verbs ("zero here" + relative moves)
    # ------------------------------------------------------------------ #
    def relative_position(self, axis: int, device_pos: float | None = None) -> float:
        """Current position measured from the relative (zeroed) origin."""
        if device_pos is None:
            device_pos = self.backend.read_position(axis)
        return device_pos - axis_rel_origin(self.cfg, axis)

    def set_zero(self, axis: int) -> float:
        """Define the CURRENT position of ``axis`` as its relative zero."""
        current = self.backend.read_position(axis)
        set_axis_rel_origin(self.cfg, axis, current)
        self._emit("info", f"{AXES[axis]} zeroed here (origin = {current:.4g} mm device)")
        return current

    def set_zero_all(self) -> list:
        origins = [self.set_zero(a) for a in range(3)]
        self._emit("info", "all axes zeroed at current position")
        return origins

    def clear_zero(self, axis: int) -> None:
        """Drop the relative origin back to absolute device zero."""
        set_axis_rel_origin(self.cfg, axis, 0.0)
        self._emit("info", f"{AXES[axis]} relative origin cleared (back to absolute)")

    def clear_zero_all(self) -> None:
        for axis in range(3):
            set_axis_rel_origin(self.cfg, axis, 0.0)
        self._emit("info", "relative origins cleared")

    def move_relative(self, axis: int, value: float) -> float:
        """Move to ``value`` measured from the relative zero.

        (device target = relative origin + value, then clamped to limits.)
        After ``set_zero``, ``move_relative(axis, 0)`` returns to the zero point.
        """
        device_target = axis_rel_origin(self.cfg, axis) + float(value)
        self._emit("info", f"move {AXES[axis]} -> {value:.4g} (relative)")
        return self.move_axis(axis, device_target)

    # ------------------------------------------------------------------ #
    # position list
    # ------------------------------------------------------------------ #
    def store_position(self, slot: int, name: str = "") -> dict:
        """Capture the CURRENT device position into a slot."""
        pos = [self.backend.read_position(a) for a in range(3)]
        p = self.positions.store(slot, pos[0], pos[1], pos[2], name=name)
        self._emit("info", f"stored slot {slot} '{p.name}' = ({pos[0]:.4g}, {pos[1]:.4g}, {pos[2]:.4g})")
        from dataclasses import asdict
        return asdict(p)

    def clear_position(self, slot: int) -> None:
        self.positions.clear(slot)
        self._emit("info", f"cleared slot {slot}")

    def goto_position(self, slot: int) -> list:
        """Drive all three motors to a saved slot (device coordinates)."""
        p = self.positions.get(slot)
        if not p.used:
            self._emit("warn", f"slot {slot} is empty")
            return []
        targets = [
            self.move_axis(0, p.x),
            self.move_axis(1, p.y),
            self.move_axis(2, p.z),
        ]
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
        """Re-push velocity/acceleration after the config was edited in place."""
        self._ensure_matrix_ok()  # reject a singular matrix from set_config / INI
        for axis in range(3):
            self.set_velocity(axis, axis_velocity(self.cfg, axis))
            self.set_acceleration(axis, axis_acceleration(self.cfg, axis))
        self._emit("info", "config applied")
