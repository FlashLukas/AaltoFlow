"""The brain: :class:`Rotator` -- one continuous rotary axis, in degrees.

Set-and-forget family: you command an angle and the K-Cube's servo drives the
stage there on its own; there is no control loop for us to run. What the brain
DOES own:

  * the wrap policy -- how "go to 10 deg" becomes a controller position on a
    stage that can turn forever (angles.py, config.py docstring),
  * the safety envelope (clamps + a warn event),
  * the homing rule: absolute moves are refused until the stage is homed,
  * the display zero and a small list of stored orientations,
  * an honest "moving" flag and the target a scan waits on (below),
  * the fly-scan position stream (stream.py).

Threads (gotcha #1)
-------------------
ONE poll thread owns every hardware READ: position, moving, homed, at
``hardware.poll_hz``. ``status()`` never touches hardware; it composes the
latest readings with the brain's own attributes. Commands (move, home, stop,
velocity) run on the caller's thread. Every backend call, from either thread,
is made under ``self._hw`` (an RLock), so the driver never sees two at once.
Setters change BRAIN ATTRIBUTES; nothing ever writes into a published snapshot.

The "moving" flag and the stale-status hole (gotchas #2, #17, #28)
------------------------------------------------------------------
Right after a move command the last poll may still say "not moving" -- the
reading was taken BEFORE the command, or the servo has not started yet. A
scan that trusted it would measure at the old angle. So a move command sets
``_move_pending`` in the SAME critical section that publishes the new
``target_deg``; the poll thread clears it only on a reading that STARTED after
the command and says "not moving" and either "at the target" or "the start
grace has expired" (a stop, or a target the servo parks a hair away from).
``moving`` in status = hardware moving OR pending OR homing.

A latch is also released as soon as a post-command reading says "not moving"
after an earlier post-command reading said "moving" (the move ran and ended,
wherever it ended) -- the grace is only the fallback for a controller that
never reports the motion at all.

Why every move is also NUMBERED (``move_id``): the angle's settle compares the
echoed ``target_deg`` with the commanded number. In a modulo-360 mode a
relative move of +360 lands on the SAME target number it started from, so a
status frame from before the command ("target 10, not moving") would pass for
an arrival while the stage still has a whole turn to go. The move_by /
goto_angle actions therefore wait on the ``move_id`` the command replied with,
exactly as hf2 waits on its acquisition number (gotcha #17).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass

from .angles import AngleList, display_angle, raw_target, wrap360
from .backends.base import RotatorBackend
from .config import Config, wrap_policy
from .stream import StreamRecorder

#: Fly-scan stream channel(s): the displayed angle, deg.
STREAM_CHANNELS = ("angle",)
#: Highest poll rate a stream may ask for, Hz. Each poll is three USB
#: round trips on the real K-Cube (VERIFY the cost; 100 Hz may be too much).
STREAM_HZ_MAX = 100.0
#: Slowest velocity accepted, deg/s (a zero velocity would never arrive).
MIN_VELOCITY = 0.1
MIN_ACCELERATION = 1.0


@dataclass
class RotatorStatus:
    """A snapshot of everything the GUI / a remote client needs."""

    angle_deg: float          # displayed angle (literal: continuous; else [0,360))
    raw_deg: float            # controller position, continuous over turns
    target_deg: float | None  # last commanded angle, exactly as commanded; None after stop
    moving: bool              # hardware moving OR a move/home not yet confirmed
    homed: bool
    homing: bool
    home_id: int              # number of the last home command (wait target)
    move_id: int              # number of the last move command (wait target)
    velocity: float           # deg/s, read back from the controller
    acceleration: float       # deg/s^2, read back
    zero_deg: float           # display zero, controller deg
    wrap: str                 # active wrap policy
    streaming: bool
    connected: bool
    hw_error: str             # "" when the last poll succeeded


class Rotator:
    def __init__(self, backend: RotatorBackend, cfg: Config):
        self.backend = backend
        self.cfg = cfg
        self.angles = AngleList()
        self.stream = StreamRecorder(STREAM_CHANNELS)
        self._on_event = lambda level, msg: None

        self._hw = threading.RLock()        # every backend call
        self._st = threading.Lock()         # the attributes status() composes

        # -- latest hardware readings (written by the poll thread only) ----- #
        self._raw = float("nan")
        self._hw_moving = False
        self._hw_homed = False
        self._hw_error = ""
        # -- command state (written by commands; poll thread clears latches) - #
        self._target_angle: float | None = None
        self._target_raw: float | None = None
        self._move_pending = False
        self._move_t = 0.0
        self._move_id = 0
        self._move_seen_busy = False
        self._home_seen_busy = False
        self._homing = False
        self._home_t = 0.0
        self._home_id = 0
        self._velocity = float("nan")
        self._acceleration = float("nan")
        self._stream_hz = 0.0

        self._connected = False
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

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
        """Open the controller, ADOPT its state, start polling.

        Lukas's rule (2026-09-27, every module): starting the software must
        not change the instrument. So start() only READS: the controller's
        stored velocity/acceleration are adopted into the brain AND into
        ``cfg.motion`` (so the GUI's spin boxes, get_config and describe show
        what the K-Cube will really do), and the first poll adopts position,
        moving and homed. Nothing is written -- no profile push, no homing.
        The config's velocity/acceleration are applied only when the user
        sets them (set_velocity / set_acceleration / set_config).
        """
        with self._hw:
            # If open() fails (HardwareBusy: another service holds this
            # K-Cube; or no driver), _connected stays False, so shutdown()
            # sends NOTHING -- no "safe stop" to a controller we do not own.
            self.backend.open()
            try:
                v, a = self.backend.read_velocity_params()
            except BaseException:
                # Opened but could not even read it: close again (which also
                # releases the hardware lock) instead of leaving it half-open.
                try:
                    self.backend.close()
                except Exception:
                    pass
                raise
            self._connected = True
        v, a = float(v), float(a)
        with self._st:
            self._velocity, self._acceleration = v, a
        self.cfg.motion.velocity, self.cfg.motion.acceleration = v, a
        self._poll_once()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="ddr25-poll", daemon=True)
        self._thread.start()
        self._emit("info", f"rotation stage started ({self.idn()})")
        st = self.status()
        self._emit("info", f"adopted controller state: {st.raw_deg:.4f} deg, "
                           f"{'homed' if st.homed else 'NOT homed'}, "
                           f"{v:.4g} deg/s, {a:.4g} deg/s^2")
        # Out-of-envelope values are REPORTED, not corrected: correcting them
        # would be a write at start. The next set_velocity clamps as usual.
        if self.cfg.limits.enforce and v > self.cfg.limits.max_velocity:
            self._emit("warn", f"controller velocity {v:.4g} deg/s is above the "
                               f"limit {self.cfg.limits.max_velocity:.4g} (left as is)")
        if self.cfg.limits.enforce and a > self.cfg.limits.max_acceleration:
            self._emit("warn", f"controller acceleration {a:.4g} deg/s^2 is above the "
                               f"limit {self.cfg.limits.max_acceleration:.4g} (left as is)")

    def shutdown(self) -> None:
        """Stop motion (profiled), stop polling, close. Idempotent."""
        if not self._connected:
            return
        try:
            self.stream_stop()
        except Exception:
            pass
        try:
            with self._hw:
                self.backend.stop(False)
        except Exception:
            pass
        self._stop_evt.set()
        th, self._thread = self._thread, None
        if th is not None and th is not threading.current_thread():
            th.join(timeout=2.0)
        try:
            with self._hw:
                self.backend.close()
        finally:
            self._connected = False
        self._emit("info", "rotation stage shut down")

    def idn(self) -> str:
        try:
            with self._hw:
                return self.backend.idn()
        except Exception as exc:
            return f"? ({exc})"

    # ------------------------------------------------------------------ #
    # the poll thread
    # ------------------------------------------------------------------ #
    def _poll_period(self) -> float:
        hz = max(float(self.cfg.hardware.poll_hz), 1.0)
        if self.stream.running:
            hz = max(hz, self._stream_hz)
        return 1.0 / hz

    def _poll_loop(self) -> None:
        next_t = time.monotonic()
        while not self._stop_evt.is_set():
            self._poll_once()
            next_t += self._poll_period()
            wait = next_t - time.monotonic()
            if wait > 0:
                time.sleep(wait)       # high resolution on Windows (gotcha #34)
            else:
                next_t = time.monotonic()

    def _poll_once(self) -> None:
        t_start = time.monotonic()
        try:
            with self._hw:
                raw = float(self.backend.read_position())
                moving = bool(self.backend.is_moving())
                homed = bool(self.backend.is_homed())
            err = ""
        except Exception as exc:
            raw, moving, homed, err = float("nan"), False, False, f"{type(exc).__name__}: {exc}"
        t_read = time.time()
        grace = max(float(self.cfg.hardware.start_grace_s), 0.0)
        tol = max(float(self.cfg.hardware.in_position_tol_deg), 1e-6)
        events = []
        with self._st:
            self._raw, self._hw_moving, self._hw_homed = raw, moving, homed
            if err and err != self._hw_error:
                events.append(("error", f"hardware read failed: {err}"))
            self._hw_error = err
            # A latch is released only by a reading that began AFTER its command.
            if self._move_pending and t_start > self._move_t and not err and moving:
                self._move_seen_busy = True
            if self._homing and t_start > self._home_t and not err and moving:
                self._home_seen_busy = True
            if self._move_pending and t_start > self._move_t and not err and not moving:
                at_target = (self._target_raw is not None
                             and abs(raw - self._target_raw) <= tol)
                if at_target or self._move_seen_busy or t_start - self._move_t >= grace:
                    self._move_pending = False
                    if not at_target and self._target_raw is not None:
                        events.append(("warn", f"stopped {raw - self._target_raw:+.4f} deg "
                                               f"from the target"))
            if self._homing and t_start > self._home_t and not err and not moving:
                # "homed" alone is not enough: a re-home may start with the
                # previous home's bit still set (VERIFY on the KBD101), so it
                # counts only once the home was SEEN running, or after the grace.
                if homed and (self._home_seen_busy or t_start - self._home_t >= grace):
                    self._homing = False
                    events.append(("info", f"homed (#{self._home_id})"))
                elif not homed and (self._home_seen_busy
                                    or t_start - self._home_t >= grace):
                    self._homing = False
                    events.append(("warn", "homing ended without a reference"))
            angle = display_angle(raw, self.cfg.frame.zero_deg, wrap_policy(self.cfg))
        if self.stream.running and not err:
            self.stream.append(t_read, [angle])
        for level, msg in events:
            self._emit(level, msg)

    # ------------------------------------------------------------------ #
    # status (never touches hardware, never raises)
    # ------------------------------------------------------------------ #
    def status(self) -> RotatorStatus:
        try:
            with self._st:
                policy = wrap_policy(self.cfg)
                zero = float(self.cfg.frame.zero_deg)
                return RotatorStatus(
                    angle_deg=display_angle(self._raw, zero, policy),
                    raw_deg=self._raw,
                    target_deg=self._target_angle,
                    moving=bool(self._hw_moving or self._move_pending or self._homing),
                    homed=self._hw_homed,
                    homing=self._homing,
                    home_id=self._home_id,
                    move_id=self._move_id,
                    velocity=self._velocity,
                    acceleration=self._acceleration,
                    zero_deg=zero,
                    wrap=policy,
                    streaming=self.stream.running,
                    connected=self._connected,
                    hw_error=self._hw_error,
                )
        except Exception as exc:  # pragma: no cover - belt and braces
            return RotatorStatus(float("nan"), float("nan"), None, False, False, False,
                                 0, 0, float("nan"), float("nan"), 0.0, "literal", False,
                                 self._connected, f"status failed: {exc}")

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _clamp(self, value: float, lo: float, hi: float, what: str, unit: str) -> float:
        if not self.cfg.limits.enforce:
            return value
        if value < lo:
            self._emit("warn", f"{what} {value:.6g} clamped to {lo:.6g} {unit}")
            return lo
        if value > hi:
            self._emit("warn", f"{what} {value:.6g} clamped to {hi:.6g} {unit}")
            return hi
        return value

    def _base_raw(self) -> float:
        """Where a new move starts from: the target if a move is still under
        way (so quick jogs add up) or if the stage is parked on it (so 33 + 20
        is 53, not 52.9999 read through the servo's dither); else the last
        measured position."""
        if self._target_raw is not None:
            tol = max(float(self.cfg.hardware.in_position_tol_deg), 1e-6)
            if (self._move_pending or self._hw_moving
                    or abs(self._raw - self._target_raw) <= tol):
                return self._target_raw
        return self._raw

    def _command_move(self, angle: float, raw: float, what: str) -> None:
        """Issue the move and publish target + pending in ONE critical section
        (gotcha #28): no status frame can show the new target with a stale
        "not moving"."""
        with self._hw:
            self.backend.move_to(raw)
            with self._st:
                self._target_angle = float(angle)
                self._target_raw = float(raw)
                self._move_pending = True
                self._move_seen_busy = False
                self._move_t = time.monotonic()
                self._move_id += 1
        self._emit("info", f"{what} -> {angle:.4f} deg (controller {raw:.4f})")

    @property
    def move_id(self) -> int:
        """Number of the last move command (the service returns it in the
        move replies; commands arrive one at a time on its command thread)."""
        return self._move_id

    def _refuse_if_busy_homing(self) -> None:
        if self._homing:
            raise RuntimeError("homing in progress: wait for it, or stop first")

    # ------------------------------------------------------------------ #
    # motion verbs
    # ------------------------------------------------------------------ #
    def move_to(self, angle: float) -> float:
        """Absolute move to ``angle`` (deg), by the active wrap policy.

        Returns the angle as adopted (clamped in literal mode). Refused while
        not homed (``motion.require_home``) or while homing.
        """
        angle = float(angle)
        if not math.isfinite(angle):
            raise ValueError("angle must be a finite number")
        self._refuse_if_busy_homing()
        if self.cfg.motion.require_home and not self._hw_homed:
            raise RuntimeError("not homed: home the stage before an absolute move "
                               "(the encoder zero is arbitrary until then)")
        policy = wrap_policy(self.cfg)
        if policy == "literal":
            angle = self._clamp(angle, self.cfg.limits.min_deg, self.cfg.limits.max_deg,
                                "angle", "deg")
        if not math.isfinite(self._raw):
            raise RuntimeError(f"position unknown ({self._hw_error or 'no reading yet'})")
        raw = raw_target(policy, self._base_raw(), self.cfg.frame.zero_deg, angle)
        self._command_move(angle, raw, f"move ({policy})")
        return angle

    def move_by(self, delta: float) -> float:
        """Relative move by ``delta`` deg. Allowed before homing (a relative
        move needs no reference). Returns the new target angle."""
        delta = float(delta)
        if not math.isfinite(delta):
            raise ValueError("delta must be a finite number")
        self._refuse_if_busy_homing()
        if not math.isfinite(self._raw):
            raise RuntimeError(f"position unknown ({self._hw_error or 'no reading yet'})")
        policy = wrap_policy(self.cfg)
        zero = float(self.cfg.frame.zero_deg)
        base = self._base_raw()
        new_angle = (base - zero) + delta
        if policy == "literal":
            new_angle = self._clamp(new_angle, self.cfg.limits.min_deg,
                                    self.cfg.limits.max_deg, "angle", "deg")
            raw = zero + new_angle
        else:
            # Relative means relative: +720 turns twice, whatever the policy.
            raw = base + delta
            new_angle = wrap360(new_angle)
        self._command_move(new_angle, raw, f"move by {delta:+.4f}")
        return new_angle

    def home(self) -> int:
        """Start homing (turns to the encoder index). Returns the home number,
        which status echoes as ``home_id`` -- the wait target for a scan."""
        with self._hw:
            self.backend.home()
            with self._st:
                self._home_id += 1
                self._homing = True
                self._home_seen_busy = False
                self._home_t = time.monotonic()
                self._move_pending = False
                self._target_angle = None
                self._target_raw = None
                hid = self._home_id
        self._emit("info", f"homing (#{hid})")
        return hid

    def stop(self, immediate: bool = False) -> None:
        """Stop now (profiled deceleration, or at once with ``immediate``).

        The target is forgotten (``target_deg`` -> None), so a scan waiting on
        this move does not mistake the stopped stage for an arrived one.
        """
        with self._hw:
            self.backend.stop(bool(immediate))
            with self._st:
                self._move_pending = False
                self._homing = False
                self._target_angle = None
                self._target_raw = None
        self._emit("warn", "STOP" + (" (immediate)" if immediate else ""))

    # ------------------------------------------------------------------ #
    # profile verbs
    # ------------------------------------------------------------------ #
    def set_velocity(self, value: float) -> float:
        v = self._clamp(float(value), MIN_VELOCITY, self.cfg.limits.max_velocity,
                        "velocity", "deg/s")
        v = max(v, MIN_VELOCITY)          # zero never arrives, even unclamped
        with self._hw:
            self.backend.set_velocity(v)
            rb = self.backend.read_velocity_params()[0]
        with self._st:
            self._velocity = float(rb)
        self.cfg.motion.velocity = v
        self._emit("info", f"velocity = {rb:.4g} deg/s")
        return float(rb)

    def set_acceleration(self, value: float) -> float:
        a = self._clamp(float(value), MIN_ACCELERATION, self.cfg.limits.max_acceleration,
                        "acceleration", "deg/s^2")
        a = max(a, MIN_ACCELERATION)
        with self._hw:
            self.backend.set_acceleration(a)
            rb = self.backend.read_velocity_params()[1]
        with self._st:
            self._acceleration = float(rb)
        self.cfg.motion.acceleration = a
        self._emit("info", f"acceleration = {rb:.4g} deg/s^2")
        return float(rb)

    def set_wrap(self, policy: str) -> str:
        p = str(policy).strip().lower()
        from .config import WRAP_POLICIES
        if p not in WRAP_POLICIES:
            raise ValueError(f"wrap must be one of {', '.join(WRAP_POLICIES)}")
        with self._st:
            self.cfg.motion.wrap = p
        self._emit("info", f"wrap policy = {p}")
        return p

    # ------------------------------------------------------------------ #
    # display zero
    # ------------------------------------------------------------------ #
    def set_zero(self) -> float:
        """Make the CURRENT orientation read 0 deg. Returns the zero (controller deg)."""
        if not math.isfinite(self._raw):
            raise RuntimeError("position unknown")
        with self._st:
            self.cfg.frame.zero_deg = float(self._raw)
            self._target_angle = None       # the old target meant another frame
        self._emit("info", f"zeroed here (controller {self._raw:.4f} deg)")
        return float(self._raw)

    def clear_zero(self) -> None:
        with self._st:
            self.cfg.frame.zero_deg = 0.0
            self._target_angle = None
        self._emit("info", "display zero cleared (angle = controller position)")

    # ------------------------------------------------------------------ #
    # stored angles
    # ------------------------------------------------------------------ #
    def store_angle(self, slot: int, name: str = "") -> dict:
        if not math.isfinite(self._raw):
            raise RuntimeError("position unknown")
        raw = self._raw
        moving = self._move_pending or self._hw_moving or self._homing
        if not moving and self._target_raw is not None and \
                abs(raw - self._target_raw) <= self.cfg.hardware.in_position_tol_deg:
            raw = self._target_raw      # parked on a target: store it, not the dither
        s = self.angles.store(int(slot), raw, name)
        self._emit("info", f"stored slot {slot} '{s.name}' (controller {s.raw:.4f} deg)")
        return asdict(s)

    def clear_angle(self, slot: int) -> None:
        self.angles.clear(int(slot))
        self._emit("info", f"cleared slot {slot}")

    def goto_angle(self, slot: int) -> float:
        """Return to a stored orientation, by the active wrap policy (so in a
        modulo mode it takes the short way, not back through every turn)."""
        s = self.angles.get(int(slot))
        if not s.used:
            raise ValueError(f"slot {slot} is empty")
        angle = s.raw - float(self.cfg.frame.zero_deg)
        if wrap_policy(self.cfg) != "literal":
            angle = wrap360(angle)
        return self.move_to(angle)

    def get_angles(self) -> list:
        return self.angles.to_list()

    def save_angles(self, path: str) -> None:
        self.angles.save(path)
        self._emit("info", f"saved stored angles -> {path}")

    def load_angles(self, path: str) -> None:
        self.angles.load(path)
        self._emit("info", f"loaded stored angles <- {path}")

    # ------------------------------------------------------------------ #
    # fly-scan stream
    # ------------------------------------------------------------------ #
    def stream_start(self, rate_hz: float | None = None) -> int:
        """Record the angle at every poll from now on, time-stamped.

        The poll thread already reads the encoder; while a stream runs it
        polls at max(poll_hz, rate_hz), capped at STREAM_HZ_MAX. The angle is
        the ENCODER reading, a true measurement (unlike an open-loop counter).
        In the modulo-360 wrap modes the angle jumps 359.99 -> 0 at the seam:
        fly across it only in literal mode.
        """
        self._stream_hz = min(max(float(rate_hz or self.cfg.hardware.poll_hz), 1.0),
                              STREAM_HZ_MAX)
        return self.stream.start()

    def stream_stop(self) -> dict:
        return self.stream.stop()

    # ------------------------------------------------------------------ #
    # config
    # ------------------------------------------------------------------ #
    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Apply the config after it was edited in place (set_config, Settings).

        Only what DIFFERS from the controller's read-back is written, so a
        set_config of, say, the ``frame`` group does not re-send an unchanged
        velocity. The tolerances are the describe echo tolerances (the
        acceleration read-back is quantised, ~0.36 deg/s^2 per unit).
        """
        if wrap_policy(self.cfg) != str(self.cfg.motion.wrap).strip().lower():
            self._emit("warn", f"unknown wrap {self.cfg.motion.wrap!r}; using literal")
            self.cfg.motion.wrap = "literal"
        with self._st:
            v_now, a_now = self._velocity, self._acceleration
        v_cfg, a_cfg = float(self.cfg.motion.velocity), float(self.cfg.motion.acceleration)
        if not (math.isfinite(v_now) and abs(v_cfg - v_now) <= 1e-3):
            self.set_velocity(v_cfg)
        if not (math.isfinite(a_now) and abs(a_cfg - a_now) <= 0.5):
            self.set_acceleration(a_cfg)
        self._emit("info", "config applied")
