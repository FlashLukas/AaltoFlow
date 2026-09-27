"""The brain: :class:`Positioner` -- one closed-loop linear axis (section 5).

The SCU closes the position loop itself: we send a target, it steps the piezo
until its encoder says "there". So this is a *set-and-forget* instrument with
one important twist -- the encoder has to be REFERENCED before its numbers mean
anything absolute. The brain:

  * holds the desired state (target, speed, hold time) and the config,
  * clamps every request to the safety envelope (``cfg.limits``) and says so,
  * refuses absolute moves until the axis is referenced (configurable),
  * runs ONE poll thread that owns every hardware read and rebuilds the
    :class:`SmaractStatus` snapshot (gotcha #1: setters change brain
    attributes, never the snapshot; ``status()`` never touches hardware),
  * records the position into a stream for fly scans (stream.py).

Threading rule, and why it matters for scans
--------------------------------------------
Setters and the poll thread share ONE lock. A setter sends the command and
updates ``_target`` inside the lock; the poll thread reads the hardware AND
copies ``_target`` into the snapshot inside the lock. So a snapshot can never
pair the NEW target with a "not moving" read from BEFORE the command -- which
is exactly the combination that would make a scan think it had arrived before
the carriage even started (scan-core's adopt_then_flag, gotcha #2).

A second guard covers the hardware side: right after a move command the
controller may still report "stopped" for a moment. For ``MOVE_GRACE_S`` after
each command the axis counts as moving unless it is already on target.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass

from .backends.base import BUSY_STATES, SmaractBackend
from .config import Config
from .positions import PositionList
from .stream import StreamRecorder

#: After a move/reference command, how long a "stopped" read-back is not
#: believed (the controller may not have started yet). # VERIFY on the SCU.
MOVE_GRACE_S = 0.3

#: Highest hold time accepted, ms.
MAX_HOLD_MS = 60000

#: Fly-scan stream channel(s): the measured position in mm.
STREAM_CHANNELS = ("position",)


@dataclass
class SmaractStatus:
    """A snapshot of everything the GUI / a remote client needs."""

    position_mm: float = float("nan")    # measured (encoder), mm
    target_mm: float = float("nan")      # the target last commanded, mm
    relative_mm: float = float("nan")    # position - rel_origin_mm
    rel_origin_mm: float = 0.0           # the "zero here" origin, mm
    moving: bool = False                 # carriage in motion (or about to be)
    on_target: bool = False              # stopped within tolerance of target
    channel_state: str = "stopped"       # the SCU's own state name
    referenced: bool = False             # physical (absolute) position known
    referencing: bool = False            # a reference search is running
    ref_id: int = 0                      # number of the last reference search
    move_id: int = 0                     # number of the last move command
    velocity_mm_s: float = 0.0           # commanded speed
    max_frequency_hz: int = 0            # the closed-loop step frequency limit
    hold_time_ms: int = 0
    speed_mm_s: float = 0.0              # MEASURED speed (from the encoder)
    min_mm: float = 0.0                  # live soft limits, for the GUI
    max_mm: float = 0.0
    connected: bool = False
    hw_error: str = ""                   # last hardware read failure, "" if fine
    #: Manifest revision; only the SERVICE fills it in (see net/service.py).
    describe_rev: int | None = None


class Positioner:
    def __init__(self, backend: SmaractBackend, cfg: Config):
        self.backend = backend
        self.cfg = cfg
        self.positions = PositionList()
        self.stream = StreamRecorder(STREAM_CHANNELS)

        self._lock = threading.RLock()
        self._connected = False
        self._target = float("nan")
        self._move_id = 0
        self._ref_id = 0
        self._referencing = False
        self._known_before_ref = False   # was the scale absolute when the search began?
        self._grace_until = 0.0
        self._freq_hz = 0
        self._was_moving = False
        self._last_pos = float("nan")
        self._last_t = 0.0
        self._speed = 0.0
        self._pos = float("nan")         # last good position read (for setters)
        self._known = False
        self._hw_error = ""
        self._status = self._snapshot_empty()

        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        # The service replaces this hook to forward events onto the wire; the
        # GUI replaces it to append to its log. Levels: "info"|"warn"|"error".
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
        """Open the controller, ADOPT its state, start the poll thread.

        Lukas's rule (2026-09-27): starting the service READS the instrument
        and changes nothing. So the speed is read back from the controller
        (its closed-loop max frequency) and becomes motion.velocity_mm_s --
        the .ini value is only a default for an explicit set_velocity /
        set_config. The position, the channel state (a move another program
        started keeps running and shows as "moving") and "referenced" are
        read too. The target is "where it is": the SCU library has no call
        that returns a target, so a move already under way shows as moving
        towards an unknown target until it ends.

        Nothing MOVES here unless motion.reference_on_start is set (default
        off, and it stays an explicit opt-in: it is the one startup action
        that changes the instrument).
        """
        with self._lock:
            self.backend.open()
            self._connected = True
            self._adopt_velocity()
            # hold time is NOT an instrument setting on the SCU: it travels
            # with every move command, so clamping the config value writes
            # nothing to the controller.
            self.cfg.motion.hold_time_ms = self._clamp_hold(self.cfg.motion.hold_time_ms)
            self._pos = self.backend.read_position_mm()
            self._known = self.backend.physical_position_known()
            self._target = self._pos       # "where it is" is the first target
            self._poll_once()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="smaract-poll",
                                        daemon=True)
        self._thread.start()
        self._emit("info", f"positioner started ({self.backend.idn()}); "
                   + ("referenced" if self._known else "NOT referenced -- run Find reference"))
        if self.cfg.motion.reference_on_start and not self._known:
            self.find_reference()

    def shutdown(self) -> None:
        """Stop the carriage, stop polling, close the controller. Idempotent.

        Stopping first matters: a closed-loop move left running while the
        library is released would keep the piezo stepping unobserved.
        """
        if not self._connected:
            return
        self.stream.stop()
        self._stop_evt.set()
        th, self._thread = self._thread, None
        if th is not None:
            th.join(timeout=2.0)
        with self._lock:
            try:
                self.backend.stop()
            except Exception:
                pass
            try:
                self.backend.close()
            finally:
                self._connected = False
                self._status = self._build_status(self._status.channel_state,
                                                  self._status.moving, self._status.on_target)
        self._emit("info", "positioner shut down")

    def status(self) -> SmaractStatus:
        """The latest snapshot. Never touches hardware, never raises."""
        return self._status

    # ------------------------------------------------------------------ #
    # poll thread -- the ONLY place that reads the hardware periodically
    # ------------------------------------------------------------------ #
    def _poll_loop(self) -> None:
        period = 1.0 / max(1.0, float(self.cfg.hardware.poll_hz))
        while not self._stop_evt.is_set():
            t0 = time.monotonic()
            with self._lock:
                self._poll_once()
            # time.sleep, not Event.wait: a timed wait rounds up to the 15.6 ms
            # Windows tick (gotcha #34) and the stream would run slow.
            rest = period - (time.monotonic() - t0)
            if rest > 0:
                time.sleep(rest)

    def _poll_once(self) -> None:
        """Read the controller and rebuild the snapshot. Caller holds the lock."""
        now = time.monotonic()
        try:
            pos = self.backend.read_position_mm()
            state = self.backend.channel_state()
            known = self.backend.physical_position_known()
            self._hw_error = ""
        except Exception as exc:
            # Keep the last good values; say what went wrong.
            self._hw_error = f"{type(exc).__name__}: {exc}"
            self._status = self._build_status(self._status.channel_state,
                                              self._status.moving, self._status.on_target)
            return
        self.stream.append(time.time(), (pos,))

        # measured speed, lightly smoothed (the encoder reading jitters by a
        # count while the piezo slips)
        if math.isfinite(self._last_pos) and now > self._last_t:
            v = (pos - self._last_pos) / (now - self._last_t)
            self._speed = 0.6 * self._speed + 0.4 * v
        self._last_pos, self._last_t = pos, now
        self._pos = pos

        tol = self.cfg.motion.on_target_tol_um * 1e-3
        near = math.isfinite(self._target) and abs(pos - self._target) <= tol
        in_grace = now < self._grace_until
        busy = state in BUSY_STATES

        # Reference search bookkeeping.
        if self._referencing:
            if busy or in_grace:
                pass  # still searching (or not started yet)
            else:
                self._referencing = False
                self._target = pos       # the scale may have jumped: re-anchor
                near, self._was_moving = True, False
                if known:
                    self._emit("info", f"referenced: absolute position {pos:.4f} mm")
                    # A "zero here" taken on the power-on counter scale means
                    # nothing on the absolute scale, so it has to go. But if the
                    # axis was ALREADY referenced, the scale did not change (the
                    # carriage merely moved a few mm to the marks) and the
                    # user's zero is still valid -- keep it.
                    if not self._known_before_ref and self.cfg.relative.rel_origin_mm != 0.0:
                        self.cfg.relative.rel_origin_mm = 0.0
                        self._emit("warn", "zero cleared: the scale changed when referencing")
                else:
                    self._emit("error", "reference search ended WITHOUT finding the "
                                        "reference marks (end stop? stopped?)")
        if known and not self._known:
            self._known = True
        elif not known and self._known:
            self._known = False
            self._emit("warn", "controller reports the position is NO LONGER known "
                               "(power cycle?) -- reference again")

        moving = busy or self._referencing or (in_grace and not near)
        on_target = (not moving) and near
        if self._was_moving and not moving and not self._referencing and not near \
                and math.isfinite(self._target):
            self._emit("warn", f"stopped {abs(pos - self._target) * 1e3:.1f} um away "
                               f"from the target {self._target:.4f} mm (end stop, or stopped)")
        self._was_moving = moving
        if not moving:
            self._speed = 0.0 if abs(self._speed) < 1e-4 else self._speed * 0.5
        self._status = self._build_status(state, moving, on_target)

    def _snapshot_empty(self) -> SmaractStatus:
        lo, hi = self.cfg.limits.min_mm, self.cfg.limits.max_mm
        return SmaractStatus(rel_origin_mm=self.cfg.relative.rel_origin_mm,
                             velocity_mm_s=self.cfg.motion.velocity_mm_s,
                             hold_time_ms=int(self.cfg.motion.hold_time_ms),
                             min_mm=lo, max_mm=hi)

    def _build_status(self, state: str, moving: bool, on_target: bool) -> SmaractStatus:
        origin = self.cfg.relative.rel_origin_mm
        return SmaractStatus(
            position_mm=self._pos,
            target_mm=self._target,
            relative_mm=self._pos - origin,
            rel_origin_mm=origin,
            moving=bool(moving),
            on_target=bool(on_target),
            channel_state=str(state),
            referenced=bool(self._known),
            referencing=bool(self._referencing),
            ref_id=self._ref_id,
            move_id=self._move_id,
            velocity_mm_s=float(self.cfg.motion.velocity_mm_s),
            max_frequency_hz=int(self._freq_hz),
            hold_time_ms=int(self.cfg.motion.hold_time_ms),
            speed_mm_s=float(self._speed),
            min_mm=float(self.cfg.limits.min_mm),
            max_mm=float(self.cfg.limits.max_mm),
            connected=self._connected,
            hw_error=self._hw_error,
        )

    # ------------------------------------------------------------------ #
    # guards and clamps
    # ------------------------------------------------------------------ #
    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("positioner is not started")

    def _require_reference(self, what: str) -> None:
        if self._referencing:
            raise RuntimeError(f"{what} refused: a reference search is running")
        if self.cfg.motion.require_reference and not self._known:
            raise RuntimeError(
                f"{what} refused: the axis is NOT referenced, so absolute positions "
                "are not trusted yet. Run find_reference first (or use a relative step).")

    def _clamp_position(self, value: float) -> float:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"position {value!r} is not a number")
        if not self.cfg.limits.enforce:
            return value
        lo, hi = self.cfg.limits.min_mm, self.cfg.limits.max_mm
        if value < lo:
            self._emit("warn", f"target {value:.4f} mm clamped to {lo:.4f} mm")
            return lo
        if value > hi:
            self._emit("warn", f"target {value:.4f} mm clamped to {hi:.4f} mm")
            return hi
        return value

    def velocity_range(self) -> tuple[float, float]:
        """The speeds that can actually be set, mm/s: the config limits
        intersected with what the controller's frequency range allows."""
        hw, lim = self.cfg.hardware, self.cfg.limits
        step_mm = max(hw.um_per_step, 1e-9) * 1e-3
        lo = max(lim.min_velocity_mm_s, hw.min_frequency_hz * step_mm)
        hi = min(lim.max_velocity_mm_s, hw.max_frequency_hz * step_mm)
        return lo, max(lo, hi)

    def _clamp_hold(self, ms) -> int:
        ms = int(round(float(ms)))
        if ms < 0 or ms > MAX_HOLD_MS:
            c = min(MAX_HOLD_MS, max(0, ms))
            self._emit("warn", f"hold time {ms} ms clamped to {c} ms")
            return c
        return ms

    # ------------------------------------------------------------------ #
    # motion verbs
    # ------------------------------------------------------------------ #
    def _command_move(self, target: float) -> float:
        """Send an absolute move (lock held by caller) and record it."""
        self.backend.move_absolute(target, int(self.cfg.motion.hold_time_ms))
        self._target = float(target)
        self._move_id += 1
        self._grace_until = time.monotonic() + MOVE_GRACE_S
        self._was_moving = True
        return self._target

    def move_to(self, position: float) -> float:
        """Move to an ABSOLUTE position (mm). Needs a referenced axis."""
        self._require_connected()
        with self._lock:
            self._require_reference("absolute move")
            target = self._clamp_position(position)
            self._command_move(target)
        self._emit("info", f"move -> {target:.4f} mm")
        return target

    def move_by(self, delta: float) -> float:
        """Step by ``delta`` mm from where the carriage is going (or is).

        Relative to the TARGET while a move is under way, so several quick jog
        clicks add up instead of each one starting from a moving position.
        Allowed before referencing, but then limited to
        motion.max_unreferenced_step_mm (the soft limits cannot help yet).
        """
        self._require_connected()
        delta = float(delta)
        if not math.isfinite(delta):
            raise ValueError(f"step {delta!r} is not a number")
        with self._lock:
            if self._referencing:
                raise RuntimeError("step refused: a reference search is running")
            moving = self._status.moving
            base = self._target if (moving and math.isfinite(self._target)) else self._pos
            if not math.isfinite(base):
                base = self.backend.read_position_mm()
            if self._known or not self.cfg.motion.require_reference:
                target = self._clamp_position(base + delta)
            else:
                cap = abs(self.cfg.motion.max_unreferenced_step_mm)
                if abs(delta) > cap:
                    self._emit("warn", f"unreferenced step {delta:.4f} mm clamped to "
                                       f"{math.copysign(cap, delta):.4f} mm")
                    delta = math.copysign(cap, delta)
                target = base + delta
            self._command_move(target)
        self._emit("info", f"step {delta:+.4f} mm -> {target:.4f} mm")
        return target

    def move_from_zero(self, value: float) -> float:
        """Move to ``value`` mm measured from the "zero here" origin."""
        return self.move_to(self.cfg.relative.rel_origin_mm + float(value))

    def find_reference(self) -> int:
        """Search the distance-coded reference marks (the carriage MOVES a few
        mm). Returns the search's number; status `ref_id` carries it and
        `referencing` goes false when it is over."""
        self._require_connected()
        with self._lock:
            self.backend.find_reference(int(self.cfg.motion.hold_time_ms))
            self._known_before_ref = self._known
            self._ref_id += 1
            self._referencing = True
            self._grace_until = time.monotonic() + MOVE_GRACE_S
            rid = self._ref_id
            # Re-publish now, so a status read right after this reply already
            # says "referencing #rid" rather than "idle, previous id".
            self._status = self._build_status("moving_to_reference", True, False)
        self._emit("info", f"reference search #{rid} started")
        return rid

    def stop(self) -> None:
        """Halt the carriage where it is. Always allowed."""
        self._require_connected()
        with self._lock:
            self.backend.stop()
            self._referencing = False
            self._grace_until = 0.0
            try:
                self._pos = self.backend.read_position_mm()
            except Exception:
                pass
            # The target becomes "here": nothing is pending any more, and a
            # scan waiting on this axis sees it stopped where it stopped.
            self._target = self._pos
            self._was_moving = False
        self._emit("warn", "STOP")

    # ------------------------------------------------------------------ #
    # parameters
    # ------------------------------------------------------------------ #
    def _adopt_velocity(self) -> float:
        """Read the controller's closed-loop max frequency and turn it into
        the speed the brain reports (mm/s = Hz x step length). A read, never
        a write: whatever speed the last user left in the SCU stays. A value
        outside our configured range is reported, not corrected -- the next
        explicit set_velocity will clamp it. Caller holds the lock."""
        hw = self.cfg.hardware
        hz = int(self.backend.get_max_frequency())
        self._freq_hz = hz
        v = hz * max(hw.um_per_step, 1e-9) * 1e-3
        self.cfg.motion.velocity_mm_s = v
        lo, hi = self.velocity_range()
        if hz <= 0:
            # What 0 means on the SCU is not confirmed (unlimited? disabled?).
            # VERIFY on the controller.
            self._emit("warn", "controller reports a closed-loop max frequency of "
                               f"{hz} Hz -- speed unknown; set one with set_velocity")
        elif v < lo - 1e-12 or v > hi + 1e-12:
            self._emit("warn", f"adopted speed {v:.4g} mm/s ({hz} Hz) is outside the "
                               f"configured range {lo:.4g}..{hi:.4g} mm/s; left as it is")
        return v

    def _push_velocity(self, value: float, quiet: bool = False) -> float:
        lo, hi = self.velocity_range()
        v = float(value)
        if not math.isfinite(v):
            raise ValueError(f"velocity {value!r} is not a number")
        if self.cfg.limits.enforce and (v < lo or v > hi):
            c = min(hi, max(lo, v))
            if not quiet or v != c:
                self._emit("warn", f"velocity {v:.4g} mm/s clamped to {c:.4g} mm/s")
            v = c
        hw = self.cfg.hardware
        hz = int(round(v / (max(hw.um_per_step, 1e-9) * 1e-3)))
        hz = min(hw.max_frequency_hz, max(hw.min_frequency_hz, hz))
        self.backend.set_max_frequency(hz)
        self._freq_hz = hz
        self.cfg.motion.velocity_mm_s = v
        return v

    def set_velocity(self, value: float) -> float:
        """Set the travel speed (mm/s) = the closed-loop max step frequency."""
        self._require_connected()
        with self._lock:
            v = self._push_velocity(value)
            self._status = self._build_status(self._status.channel_state,
                                              self._status.moving, self._status.on_target)
        self._emit("info", f"velocity = {v:.4g} mm/s ({self._freq_hz} Hz)")
        return v

    def set_hold_time(self, ms) -> int:
        """How long the controller holds a reached target, ms (0 = let go)."""
        with self._lock:
            ms = self._clamp_hold(ms)
            self.cfg.motion.hold_time_ms = ms
        self._emit("info", f"hold time = {ms} ms (applies from the next move)")
        return ms

    # ------------------------------------------------------------------ #
    # "zero here"
    # ------------------------------------------------------------------ #
    def set_zero(self) -> float:
        """Make the CURRENT position the relative zero."""
        self._require_connected()
        with self._lock:
            origin = self._pos
            if not math.isfinite(origin):
                raise RuntimeError("no position reading yet")
            self.cfg.relative.rel_origin_mm = origin
        self._emit("info", f"zero here (origin = {origin:.4f} mm)")
        return origin

    def clear_zero(self) -> None:
        with self._lock:
            self.cfg.relative.rel_origin_mm = 0.0
        self._emit("info", "zero cleared (relative = absolute)")

    # ------------------------------------------------------------------ #
    # stored positions (absolute scale -> need a referenced axis)
    # ------------------------------------------------------------------ #
    def store_position(self, slot: int, name: str = "") -> dict:
        self._require_connected()
        self._require_reference("store position")
        p = self.positions.store(int(slot), self._pos, name=name)
        self._emit("info", f"stored slot {slot} '{p.name}' = {p.position_mm:.4f} mm")
        return asdict(p)

    def clear_position(self, slot: int) -> None:
        self.positions.clear(int(slot))
        self._emit("info", f"cleared slot {slot}")

    def goto_position(self, slot: int) -> float:
        p = self.positions.get(int(slot))
        if not p.used:
            raise ValueError(f"slot {slot} is empty")
        target = self.move_to(p.position_mm)
        self._emit("info", f"go to slot {slot} '{p.name}'")
        return target

    def get_positions(self) -> list:
        return self.positions.to_list()

    def save_positions(self, path: str) -> None:
        self.positions.save(path)
        self._emit("info", f"saved positions -> {path}")

    def load_positions(self, path: str) -> None:
        self.positions.load(path)
        self._emit("info", f"loaded positions <- {path}")

    # ------------------------------------------------------------------ #
    # fly-scan stream
    # ------------------------------------------------------------------ #
    def stream_start(self) -> int:
        """Start recording every position the poll thread reads."""
        return self.stream.start()

    def stream_stop(self) -> dict:
        return self.stream.stop()

    # ------------------------------------------------------------------ #
    # config
    # ------------------------------------------------------------------ #
    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Apply the config after it was edited in place (set_config).

        The speed is written to the controller only when the config value
        actually DIFFERS from what the controller runs at now: a set_config
        that touches another group (theme, limits, ...) must not rewrite -- or
        silently clamp -- a speed that was adopted from the instrument.
        """
        with self._lock:
            hw = self.cfg.hardware
            current = self._freq_hz * max(hw.um_per_step, 1e-9) * 1e-3
            wanted = float(self.cfg.motion.velocity_mm_s)
            if self._connected and abs(wanted - current) > 1e-9 * max(1.0, abs(current)):
                self._push_velocity(wanted)
            self.cfg.motion.hold_time_ms = self._clamp_hold(self.cfg.motion.hold_time_ms)
            if self._connected:
                self._status = self._build_status(self._status.channel_state,
                                                  self._status.moving, self._status.on_target)
        self._emit("info", "config applied")
