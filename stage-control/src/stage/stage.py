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

from dataclasses import dataclass

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


class Stage:
    def __init__(self, backend: StageBackend, cfg: Config):
        self.backend = backend
        self.cfg = cfg
        self.positions = PositionList()
        self._connected = False
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
        """Open the backend and push the start-up motion parameters."""
        self.backend.open()
        self._connected = True
        self._ensure_matrix_ok()  # a config file could carry a singular matrix
        for axis in range(3):
            self.backend.set_velocity(axis, axis_velocity(self.cfg, axis))
            self.backend.set_acceleration(axis, axis_acceleration(self.cfg, axis))
        if self.cfg.motion.home_on_start:
            self.home_all()
        self._emit("info", f"stage started ({self.backend.idn()})")

    def shutdown(self) -> None:
        """Stop all motion and close the backend.  Idempotent."""
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
        """Snapshot of live state.  NEVER raises."""
        try:
            pos = [self.backend.read_position(a) for a in range(3)]
            moving = [self.backend.is_moving(a) for a in range(3)]
            homed = [self.backend.is_homed(a) for a in range(3)]
            vel = [self.backend.read_velocity(a) for a in range(3)]
            acc = [self.backend.read_acceleration(a) for a in range(3)]
        except Exception:
            pos = [float("nan")] * 3
            moving = [False] * 3
            homed = [False] * 3
            vel = [0.0] * 3
            acc = [0.0] * 3
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
        )

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
        """Move ONE motor to an absolute DEVICE position (mm)."""
        target = self._clamp_axis(axis, float(position))
        self.backend.move_to(axis, target)
        self._emit("info", f"move {AXES[axis]} -> {target:.4g} mm")
        return target

    def move_logical(self, u: float, v: float, w: float) -> list:
        """Move to a LOGICAL point -- converted through offsets + transform."""
        device = self.device_from_logical(u, v, w)
        targets = [self.move_axis(a, device[a]) for a in range(3)]
        return targets

    def home(self, axis: int) -> None:
        self.backend.home(axis)
        self._emit("info", f"homing {AXES[axis]}")

    def home_all(self) -> None:
        for axis in range(3):
            self.backend.home(axis)
        self._emit("info", "homing all axes")

    def stop(self, axis: int) -> None:
        self.backend.stop(axis)
        self._emit("warn", f"stop {AXES[axis]}")

    def stop_all(self) -> None:
        for axis in range(3):
            self.backend.stop(axis)
        self._emit("warn", "STOP all axes")

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
