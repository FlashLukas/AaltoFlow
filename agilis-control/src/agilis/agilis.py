"""The brain: :class:`AgilisStage` (set-and-forget family, with a poll thread).

A Newport Agilis stage is *set-and-forget*: you ask the AG-UC2 for N steps and
it makes them on its own -- there is no control loop for us to run. The brain:

  * owns the ONLY conversation with the backend (one lock around every call),
  * runs a POLL THREAD that reads the step counters, the axis states and the
    limit switches and rebuilds the status snapshot (gotcha #1: setters change
    brain attributes, never the snapshot; ``status()`` never touches hardware),
  * clamps every target to the safety envelope (limits or leash) and stops a
    continuous jog that leaves it -- a jog has no end point of its own,
  * keeps the position ESTIMATE in micrometres (see below),
  * owns the relative "zero here" read-out origin and the 20-slot position list,
  * records the position for scan-core's fly scan (stream verbs).

Motion is fire-and-forget: ``move_*`` returns once the controller has accepted
the command; the caller watches ``status().moving``.

------------------------------------------------------------------------------
Steps are counted, micrometres are estimated
------------------------------------------------------------------------------
The controller's step counter (TP) is forward steps minus backward steps. On a
slip-stick actuator the two directions step DIFFERENT distances, so the net
count alone cannot give a position in um: 1000 steps out and 1000 back is
counter 0, but the stage has drifted by 1000 x (forward - backward step size).
So the brain watches every change of the counter and keeps the forward and the
backward steps SEPARATELY since the datum:

    position_um = forward_steps x um_per_step_fwd - backward_steps x um_per_step_bwd

With equal step sizes this is just counter x um_per_step. A relative um move
uses the step size of the direction it goes in; an absolute um move goes from
the current ESTIMATE to the target.

Each step size is only valid at the AMPLITUDE it was measured at (the manual:
no linear relation between amplitude and step size). The calibration records
that amplitude, and ``cal_valid`` in status says whether it still matches.

Steps made at ANOTHER amplitude are booked with the wrong step size, and that
error stays in the estimate even after the amplitude is set back. Two ways it
happens: the amplitude was changed, or a jog ran at speed 2 or 3 -- the manual
says those two speeds always use the MAXIMUM amplitude (50), whatever SU says.
The brain counts such steps (``uncal_steps``) and reports ``estimate_ok`` =
False until the next datum, instead of pretending the um are still good.

Two distinct "zeros":
  * ``zero_counter``  -- HARDWARE datum (ZP): step counter := 0 here, and the
                         forward/backward tallies with it.
  * ``set_zero``      -- SOFTWARE display origin for the relative read-out only.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass

from .backends.base import JOGGING, MOVING_TO_LIMIT, READY, STEPPING, AgilisBackend
from .config import (
    AMPLITUDE_MAX,
    AMPLITUDE_MIN,
    AXES,
    Config,
    axis_amplitude,
    axis_effective_limits,
    axis_rel_origin,
    axis_um_per_step,
    calibration_amplitude,
    hardware_axis,
    set_axis_amplitude,
    set_axis_rel_origin,
    set_axis_um_per_step,
    set_calibration_amplitude,
)
from .positions import PositionList
from .stream import StreamRecorder

#: Fly-scan stream channels: the position estimate of each axis in um, the
#: same number `position_um` in status (and the `position_x` control) reads.
STREAM_CHANNELS = ("x", "y")
#: Highest poll/stream rate, Hz. Each poll is 5 serial round trips (2x TP,
#: 2x TS, PH); at 921600 baud the USB latency (~1 ms each) dominates. # VERIFY
POLL_HZ_MAX = 100.0

#: For this long after a PR is accepted the axis counts as moving even if TS
#: already says READY, unless the counter has reached the target. Guards the
#: moment between "command accepted" and "TS reports stepping" (gotcha #2),
#: which the manual does not specify. # VERIFY on the controller.
PENDING_S = 0.3

STATE_NAMES = {READY: "ready", STEPPING: "stepping", JOGGING: "jogging",
               MOVING_TO_LIMIT: "to limit"}


@dataclass
class AgilisStatus:
    """A snapshot of everything the GUI / remote client needs.

    Lists are length-2, axis-indexed (X, Y). Built by the poll thread only.
    """

    position_steps: list   # step counter per axis (TP)
    position_um: list      # position ESTIMATE, um (forward/backward tallies)
    rel_steps: list        # steps from the software zero origin
    rel_um: list           # um from the software zero origin
    rel_origin: list       # the software zero origin, steps
    target_steps: list     # last commanded step target
    target_um: list        # last commanded target in um (as commanded)
    moving: list           # bool per axis (stepping, jogging, or just commanded)
    axis_state: list       # TS code per axis: 0 ready 1 stepping 2 jogging 3 to limit
    jogging: list          # bool per axis
    direction: list        # +1 / -1 / 0: which way the counter last moved
    amplitude_fwd: list    # step amplitude (SU) forward, 1..50
    amplitude_bwd: list    # step amplitude backward
    um_per_step: list      # mean step size (both directions), um
    um_per_step_fwd: list
    um_per_step_bwd: list
    cal_amp_fwd: list      # amplitude the forward step size was measured at
    cal_amp_bwd: list
    cal_valid: list        # does the step size still apply (amplitudes match)?
    uncal_steps: list      # steps since the datum made at a non-calibrated amplitude
    estimate_ok: list      # cal_valid AND no such steps: position_um can be trusted
    limit_lo: list         # effective lower step bound per axis (what we clamp to)
    limit_hi: list
    limit_switch: list     # PH: limit switch active (only stages that have one)
    leash: bool
    leash_steps: int
    step_large: bool       # amplitude preset: True = all at large_amplitude
    connected: bool
    hw_error: str = ""     # last failed hardware read ("" = fine)
    poll_hz: float = 0.0


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


class AgilisStage:
    def __init__(self, backend: AgilisBackend, cfg: Config):
        self.backend = backend
        self.cfg = cfg
        self.positions = PositionList()
        self._connected = False
        self._on_event = lambda level, msg: None
        # ONE lock around every backend call AND the bookkeeping that goes with
        # it, so a poll never reads the counter half-way through a move command
        # and a snapshot never pairs a new target with an old axis state.
        self._io = threading.RLock()
        # -- live control state (setters write these; the poll copies them) --
        self._count_seen = [0, 0]      # last counter value accounted for
        self._fwd = [0, 0]             # forward steps since the datum
        self._bwd = [0, 0]             # backward steps since the datum
        self._dir = [0, 0]
        self._target_steps = [0, 0]
        self._target_um = [0.0, 0.0]
        self._pending_until = [0.0, 0.0]
        self._jog_mode = [0, 0]
        self._jog_deadline = [0.0, 0.0]
        # steps booked since the datum at an amplitude the step size was NOT
        # measured at (see the module docstring): the estimate is then approximate
        self._uncal = [0, 0]
        self._hw_error = ""
        # -- poll thread + fly-scan stream --
        self.stream = StreamRecorder(STREAM_CHANNELS)
        self._stream_hz = 0.0
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._snap = self._build_snapshot([0, 0], [READY, READY], 0)

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #
    def _emit(self, level: str, msg: str) -> None:
        try:
            self._on_event(level, msg)
        except Exception:
            pass  # a broken listener must never break the instrument

    # ------------------------------------------------------------------ #
    # the calibration bridge (steps <-> micrometres)
    # ------------------------------------------------------------------ #
    def um_per_step(self, axis: int, direction: int = 0) -> float:
        """Measured step size (um): +1 forward, -1 backward, 0 mean. Always > 0."""
        v = axis_um_per_step(self.cfg, axis, direction)
        return v if v > 0 else 1e-9          # never divide by zero downstream

    def cal_valid(self, axis: int) -> bool:
        """True when both directions' step sizes were measured at the amplitude
        now set. An amplitude of 0 in the calibration means "not recorded",
        which is reported as not valid -- the brain cannot vouch for it."""
        return all(
            calibration_amplitude(self.cfg, axis, d) == axis_amplitude(self.cfg, axis, d)
            for d in (+1, -1))

    def _estimate_um(self, axis: int) -> float:
        return (self._fwd[axis] * self.um_per_step(axis, +1)
                - self._bwd[axis] * self.um_per_step(axis, -1))

    def _amp_in_force(self, axis: int, direction: int) -> int:
        """The amplitude the controller is stepping with right now.

        Jog speeds 2 and 3 use the maximum amplitude whatever SU says (manual,
        JA and SU commands); everything else uses the SU value.
        """
        if abs(self._jog_mode[axis]) in (2, 3):
            return AMPLITUDE_MAX
        return axis_amplitude(self.cfg, axis, direction)

    def _account(self, axis: int, count: int) -> None:
        """Book a counter change as forward or backward steps.

        Called with every fresh counter reading. Between two readings an axis
        moves one way only (a PR or a jog), so the sign of the change says
        which step size applies. Readings are taken before every command too,
        so a reversal is never hidden inside one interval.
        """
        delta = int(count) - self._count_seen[axis]
        if delta > 0:
            self._fwd[axis] += delta
        elif delta < 0:
            self._bwd[axis] -= delta
        if delta:
            d = _sign(delta)
            self._dir[axis] = d
            if self._amp_in_force(axis, d) != calibration_amplitude(self.cfg, axis, d):
                self._uncal[axis] += abs(delta)
        self._count_seen[axis] = int(count)

    def _hw(self, axis: int) -> int:
        return hardware_axis(self.cfg, axis)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Open the controller, make it safe, push the amplitudes, start polling."""
        with self._io:
            self.backend.open()
            self._connected = True
            try:
                self._start_axes()
            except Exception:
                # never leave a half-started controller behind: stop, close
                # (the port is freed, the buttons go back to the user), re-raise
                try:
                    for axis in range(2):
                        try:
                            self.backend.stop(self._hw(axis))
                        except Exception:
                            pass
                    self.backend.close()
                finally:
                    self._connected = False
                raise
        self._poll_once()
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, name="agilis-poll",
                                             daemon=True)
        self._poll_thread.start()
        self._emit("info", f"agilis started ({self.backend.idn()})")

    def _start_axes(self) -> None:
        """Safe state + bookkeeping + amplitudes for both axes (under the lock)."""
        for axis in range(2):
            hw = self._hw(axis)
            # SAFE START: a jog left running by a crashed session would
            # otherwise keep driving the stage into its end stop.
            self.backend.stop(hw)
            count = int(self.backend.read_position(hw))
            # The counter may not be 0 (it counts since power-up): book what
            # it holds so the estimate starts consistent with it.
            self._count_seen[axis] = 0
            self._fwd[axis] = self._bwd[axis] = 0
            self._account(axis, count)
            # Only the NET count survives a restart: how many of those steps
            # went which way, and at what amplitude, is lost. So a non-zero
            # counter at start is an estimate we cannot vouch for until the
            # next datum.
            self._uncal[axis] = abs(count)
            self._target_steps[axis] = count
            self._target_um[axis] = self._estimate_um(axis)
            for d in (+1, -1):
                # the controller forgets SU at power-down: push ours
                self.backend.set_amplitude(hw, d, axis_amplitude(self.cfg, axis, d))

    def shutdown(self) -> None:
        """Stop all motion, stop polling and close the backend. Idempotent."""
        if not self._connected:
            return
        self.stream.stop()
        self._poll_stop.set()
        th, self._poll_thread = self._poll_thread, None
        if th is not None and th is not threading.current_thread():
            th.join(timeout=2.0)
        with self._io:
            try:
                for axis in range(2):
                    try:
                        self.backend.stop(self._hw(axis))
                    except Exception:
                        pass
            finally:
                try:
                    self.backend.close()
                finally:
                    self._connected = False
        self._snap = self._build_snapshot(list(self._count_seen), [READY, READY], 0)
        self._emit("info", "agilis shut down")

    # ------------------------------------------------------------------ #
    # the poll thread: the only place that builds a status snapshot
    # ------------------------------------------------------------------ #
    def _poll_period(self) -> float:
        hz = float(self.cfg.hardware.poll_hz or 10.0)
        if self.stream.running:
            hz = max(hz, self._stream_hz)
        return 1.0 / min(max(hz, 1.0), POLL_HZ_MAX)

    def _poll_loop(self) -> None:
        next_t = time.monotonic()
        while not self._poll_stop.is_set():
            self._poll_once()
            next_t += self._poll_period()
            wait = next_t - time.monotonic()
            if wait > 0:
                self._poll_stop.wait(wait)
            else:
                next_t = time.monotonic()

    def _poll_once(self) -> None:
        if not self._connected:
            return
        try:
            with self._io:
                counts = [int(self.backend.read_position(self._hw(a))) for a in range(2)]
                states = [int(self.backend.axis_state(self._hw(a))) for a in range(2)]
                limits = int(self.backend.limit_status())
                for a in range(2):
                    self._account(a, counts[a])
                    self._guard_jog(a, counts[a], states[a])
                snap = self._build_snapshot(counts, states, limits)
                self._hw_error = ""
        except Exception as exc:
            # A failed read is shown, not fatal: keep the last good snapshot
            # but say what went wrong (and keep trying).
            self._hw_error = f"{type(exc).__name__}: {exc}"
            old = self._snap
            old.hw_error = self._hw_error
            return
        self._snap = snap
        if self.stream.running:
            self.stream.append(time.time(), snap.position_um)

    def _guard_jog(self, axis: int, count: int, state: int) -> None:
        """Stop a continuous jog that timed out or left the travel envelope.

        A jog (JA) has no end point, so the clamp that protects a PR move
        cannot protect it; this check, run on every poll, is what does. Called
        under the lock.
        """
        if state != JOGGING or self._jog_mode[axis] == 0:
            if state != JOGGING:
                self._jog_mode[axis] = 0
            return
        why = ""
        if time.monotonic() > self._jog_deadline[axis]:
            why = "jog released (dead-man timeout)"
        elif self.cfg.limits.enforce:
            lo, hi = axis_effective_limits(self.cfg, axis)
            if (self._jog_mode[axis] > 0 and count >= hi) or (self._jog_mode[axis] < 0 and count <= lo):
                why = "jog reached the " + ("leash" if self.cfg.limits.leash_enabled else "limit")
        if why:
            self.backend.stop(self._hw(axis))
            # the steps made since this poll's reading belong to the jog: book
            # them at its amplitude before the mode is forgotten
            count = self._fresh_count(axis)
            self._jog_mode[axis] = 0
            self._target_steps[axis] = count
            self._target_um[axis] = self._estimate_um(axis)
            self._emit("warn" if "reached" in why else "info", f"{AXES[axis]} {why}, stopped")

    def _build_snapshot(self, counts, states, limits) -> AgilisStatus:
        now = time.monotonic()
        rel_origin = [axis_rel_origin(self.cfg, a) for a in range(2)]
        mean = [self.um_per_step(a) for a in range(2)]
        moving = []
        for a in range(2):
            pending = (now < self._pending_until[a] and counts[a] != self._target_steps[a])
            moving.append(states[a] != READY or pending)
        bounds = [axis_effective_limits(self.cfg, a) for a in range(2)]
        amps_f = [axis_amplitude(self.cfg, a, +1) for a in range(2)]
        amps_b = [axis_amplitude(self.cfg, a, -1) for a in range(2)]
        large = int(self.cfg.motion.large_amplitude)
        hw_bit = [self._hw(a) - 1 for a in range(2)]
        return AgilisStatus(
            position_steps=list(counts),
            position_um=[self._estimate_um(a) for a in range(2)],
            rel_steps=[counts[a] - rel_origin[a] for a in range(2)],
            rel_um=[(counts[a] - rel_origin[a]) * mean[a] for a in range(2)],
            rel_origin=rel_origin,
            target_steps=list(self._target_steps),
            target_um=list(self._target_um),
            moving=moving,
            axis_state=list(states),
            jogging=[s == JOGGING for s in states],
            direction=list(self._dir),
            amplitude_fwd=amps_f,
            amplitude_bwd=amps_b,
            um_per_step=mean,
            um_per_step_fwd=[self.um_per_step(a, +1) for a in range(2)],
            um_per_step_bwd=[self.um_per_step(a, -1) for a in range(2)],
            cal_amp_fwd=[calibration_amplitude(self.cfg, a, +1) for a in range(2)],
            cal_amp_bwd=[calibration_amplitude(self.cfg, a, -1) for a in range(2)],
            cal_valid=[self.cal_valid(a) for a in range(2)],
            uncal_steps=list(self._uncal),
            estimate_ok=[self.cal_valid(a) and self._uncal[a] == 0 for a in range(2)],
            limit_lo=[b[0] for b in bounds],
            limit_hi=[b[1] for b in bounds],
            limit_switch=[bool(limits >> hw_bit[a] & 1) for a in range(2)],
            leash=bool(self.cfg.limits.leash_enabled),
            leash_steps=abs(int(self.cfg.limits.leash_steps)),
            step_large=all(v == large for v in amps_f + amps_b),
            connected=self._connected,
            hw_error=self._hw_error,
            poll_hz=1.0 / self._poll_period(),
        )

    def status(self) -> AgilisStatus:
        """The latest snapshot built by the poll thread. Never touches hardware."""
        return self._snap

    # ------------------------------------------------------------------ #
    # clamping helpers
    # ------------------------------------------------------------------ #
    def _clamp_steps(self, axis: int, value: int) -> int:
        value = int(round(value))
        if not self.cfg.limits.enforce:
            return value
        lo, hi = axis_effective_limits(self.cfg, axis)
        why = "leash" if self.cfg.limits.leash_enabled else "limit"
        if value < lo:
            self._emit("warn", f"{AXES[axis]} target {value} clamped to {lo} steps ({why})")
            return lo
        if value > hi:
            self._emit("warn", f"{AXES[axis]} target {value} clamped to {hi} steps ({why})")
            return hi
        return value

    def _need_connection(self) -> None:
        if not self._connected:
            raise RuntimeError("controller not connected (start the brain first)")

    # ------------------------------------------------------------------ #
    # motion -- the one place a PR is issued
    # ------------------------------------------------------------------ #
    def _fresh_count(self, axis: int) -> int:
        """Read the counter now and book it (call under the lock)."""
        count = int(self.backend.read_position(self._hw(axis)))
        self._account(axis, count)
        return count

    def _issue(self, axis: int, target: int, target_um: float | None, what: str) -> int:
        """Move ``axis`` to step ``target`` with one PR (call under the lock).

        A new move REPLACES one in progress (stop first: the controller refuses
        PR while an axis is not ready).
        """
        hw = self._hw(axis)
        state = int(self.backend.axis_state(hw))
        if state == MOVING_TO_LIMIT:
            raise RuntimeError(f"{AXES[axis]} is running a limit move; STOP it first")
        if state != READY:
            self.backend.stop(hw)
        # book the steps of a running jog while its mode (and so its amplitude)
        # is still known, THEN forget the jog
        count = self._fresh_count(axis)
        self._jog_mode[axis] = 0
        delta = int(target) - count
        if delta:
            self.backend.move_by(hw, delta)
        k = self.um_per_step(axis, _sign(delta))
        self._target_steps[axis] = int(target)
        # the estimate BEFORE the move plus the distance the steps will cover
        self._target_um[axis] = (float(target_um) if target_um is not None
                                 else self._estimate_um(axis) + delta * k)
        self._pending_until[axis] = time.monotonic() + PENDING_S if delta else 0.0
        self._emit("info", f"move {AXES[axis]} {what} ({delta:+d} steps -> {target})")
        return int(target)

    # -- STEP language ----------------------------------------------------- #
    def move_to_step(self, axis: int, position_steps: int) -> int:
        """Move ONE axis to an absolute step-counter value (clamped)."""
        self._need_connection()
        with self._io:
            target = self._clamp_steps(axis, int(round(position_steps)))
            return self._issue(axis, target, None, f"to {target} steps")

    def move_steps(self, axis: int, delta_steps: int) -> int:
        """Move ONE axis BY a number of steps from its current count."""
        self._need_connection()
        with self._io:
            count = self._fresh_count(axis)
            target = self._clamp_steps(axis, count + int(round(delta_steps)))
            return self._issue(axis, target, None, f"by {int(round(delta_steps))} steps")

    # -- MICROMETRE language ------------------------------------------------- #
    def move_to_um(self, axis: int, position_um: float) -> int:
        """Move ONE axis to an absolute position ESTIMATE in um.

        The distance from the current estimate is converted with the step size
        of the direction the axis will go. If the target is not clamped, the
        commanded um is kept verbatim as ``target_um`` so a scan can see the
        service adopted exactly its number (settle policy adopt_then_flag).
        """
        self._need_connection()
        want = float(position_um)
        with self._io:
            count = self._fresh_count(axis)
            d_um = want - self._estimate_um(axis)
            steps = int(round(d_um / self.um_per_step(axis, _sign(d_um))))
            target = self._clamp_steps(axis, count + steps)
            keep = want if target == count + steps else None
            return self._issue(axis, target, keep, f"to {want:.4g} um")

    def move_relative_um(self, axis: int, delta_um: float) -> int:
        """Move ONE axis BY a distance in um, with that direction's step size."""
        self._need_connection()
        d = float(delta_um)
        with self._io:
            count = self._fresh_count(axis)
            steps = int(round(d / self.um_per_step(axis, _sign(d))))
            target = self._clamp_steps(axis, count + steps)
            keep = self._estimate_um(axis) + d if target == count + steps else None
            return self._issue(axis, target, keep, f"by {d:+.4g} um")

    # -- continuous jog -------------------------------------------------------- #
    def jog(self, axis: int, speed: int) -> int:
        """Start (or keep alive) a continuous jog. ``speed`` -4..4, sign = way.

        The controller's four speeds: 1 = 5, 2 = 100, 3 = 1700, 4 = 666
        steps/s (2 and 3 at maximum amplitude). A jog ends by itself
        ``motion.jog_timeout_s`` after the last jog call (dead-man), at the
        travel envelope, or on STOP / ``jog(axis, 0)``. Returns the mode running.
        """
        self._need_connection()
        mode = int(speed)
        if abs(mode) > 4:
            mode = 4 if mode > 0 else -4
            self._emit("warn", f"{AXES[axis]} jog speed clamped to {mode}")
        hw = self._hw(axis)
        with self._io:
            if mode == 0:
                self.backend.stop(hw)
                self._fresh_count(axis)          # book the jog's steps first
                self._jog_mode[axis] = 0
                return 0
            count = self._fresh_count(axis)
            lo, hi = axis_effective_limits(self.cfg, axis)
            if self.cfg.limits.enforce and ((mode > 0 and count >= hi) or (mode < 0 and count <= lo)):
                self._emit("warn", f"{AXES[axis]} is at its {'leash' if self.cfg.limits.leash_enabled else 'limit'}: "
                                   f"jog {'+' if mode > 0 else '-'} refused")
                return 0
            state = int(self.backend.axis_state(hw))
            self._jog_deadline[axis] = time.monotonic() + float(self.cfg.motion.jog_timeout_s)
            if state == JOGGING and self._jog_mode[axis] == mode:
                return mode                         # keep-alive only
            if state == STEPPING:
                self.backend.stop(hw)
            if abs(mode) in (2, 3) and any(
                    calibration_amplitude(self.cfg, axis, d) != AMPLITUDE_MAX for d in (+1, -1)):
                self._emit("warn", f"{AXES[axis]} jog speed {abs(mode)} steps at the MAXIMUM "
                                   f"amplitude ({AMPLITUDE_MAX}), not the calibrated one: "
                                   "um are approximate until the next datum")
            self.backend.jog(hw, mode)
            self._jog_mode[axis] = mode
            self._pending_until[axis] = 0.0
            self._emit("info", f"jog {AXES[axis]} speed {mode:+d}")
            return mode

    def stop(self, axis: int) -> None:
        if not self._connected:
            return                               # nothing can be moving
        with self._io:
            self.backend.stop(self._hw(axis))
            self._pending_until[axis] = 0.0
            count = self._fresh_count(axis)      # book a jog's steps before
            self._jog_mode[axis] = 0             # forgetting its amplitude
            self._target_steps[axis] = count
            self._target_um[axis] = self._estimate_um(axis)
        self._emit("warn", f"stop {AXES[axis]}")

    def stop_all(self) -> None:
        for axis in range(2):
            self.stop(axis)

    # ------------------------------------------------------------------ #
    # datum + relative read-out origin (the two "zeros")
    # ------------------------------------------------------------------ #
    def zero_counter(self, axis: int) -> None:
        """HARDWARE datum: reset the step counter (ZP) to 0 here."""
        self._need_connection()
        with self._io:
            hw = self._hw(axis)
            if int(self.backend.axis_state(hw)) != READY:
                raise RuntimeError(f"{AXES[axis]} is moving: stop it before setting the datum")
            self.backend.zero_counter(hw)
            self._count_seen[axis] = 0
            self._fwd[axis] = self._bwd[axis] = 0
            self._uncal[axis] = 0                  # a fresh start for the estimate
            self._target_steps[axis] = 0
            self._target_um[axis] = 0.0
            set_axis_rel_origin(self.cfg, axis, 0)     # the display origin follows
        self._emit("info", f"{AXES[axis]} step counter zeroed (datum set here)")

    def zero_counter_all(self) -> None:
        for axis in range(2):
            self.zero_counter(axis)

    def set_zero(self, axis: int) -> int:
        """SOFTWARE display origin: mark the current step count as relative 0."""
        self._need_connection()
        with self._io:
            current = self._fresh_count(axis)
        set_axis_rel_origin(self.cfg, axis, current)
        self._emit("info", f"{AXES[axis]} display zeroed here (origin = {current} steps)")
        return current

    def set_zero_all(self) -> list:
        return [self.set_zero(a) for a in range(2)]

    def clear_zero(self, axis: int) -> None:
        set_axis_rel_origin(self.cfg, axis, 0)
        self._emit("info", f"{AXES[axis]} display origin cleared (back to absolute)")

    def clear_zero_all(self) -> None:
        for axis in range(2):
            self.clear_zero(axis)

    # ------------------------------------------------------------------ #
    # amplitude (step size knob) and the step-size calibration
    # ------------------------------------------------------------------ #
    def set_amplitude(self, axis: int, value: int, direction: int = 0) -> int:
        """Set the step amplitude (SU, 1..50). ``direction`` +1, -1 or 0 = both.

        Refused while the axis moves (the controller only accepts SU when the
        axis is ready). Changing it away from the amplitude the step size was
        measured at makes the um readings approximate -- the brain warns and
        ``cal_valid`` goes False.
        """
        self._need_connection()
        v = int(round(float(value)))
        if self.cfg.limits.enforce and not AMPLITUDE_MIN <= v <= AMPLITUDE_MAX:
            c = min(max(v, AMPLITUDE_MIN), AMPLITUDE_MAX)
            self._emit("warn", f"{AXES[axis]} amplitude {v} clamped to {c}")
            v = c
        dirs = (+1, -1) if direction == 0 else ((+1,) if direction > 0 else (-1,))
        with self._io:
            hw = self._hw(axis)
            if int(self.backend.axis_state(hw)) != READY:
                raise RuntimeError(f"{AXES[axis]} is moving: the amplitude can only change at rest")
            for d in dirs:
                self.backend.set_amplitude(hw, d, v)
                set_axis_amplitude(self.cfg, axis, d, v)
        way = {1: " forward", -1: " backward"}.get(direction, "")
        self._emit("info", f"{AXES[axis]}{way} step amplitude = {v}")
        if not self.cal_valid(axis):
            self._emit("warn", f"{AXES[axis]} step size was measured at amplitude "
                               f"{calibration_amplitude(self.cfg, axis, +1)}/"
                               f"{calibration_amplitude(self.cfg, axis, -1)} (fwd/bwd): um "
                               f"readings are approximate until you re-measure it")
        return v

    def set_step_size(self, large: bool) -> dict:
        """Amplitude preset: every axis and direction to the large or small value."""
        m = self.cfg.motion
        v = int(m.large_amplitude if large else m.small_amplitude)
        for a in range(2):
            self.set_amplitude(a, v, 0)
        self._emit("info", f"{'LARGE' if large else 'small'} steps (amplitude {v})")
        return {"large": bool(large), "amplitude": v}

    def set_calibration(self, axis: int, um_per_step: float, direction: int = 0) -> float:
        """Store a MEASURED step size (um per step) for an axis.

        ``direction`` 0 = both ways, +1 forward only, -1 backward only. The
        amplitude in force right now is recorded with it, because that is what
        the number was measured at.
        """
        v = float(um_per_step)
        if not v > 0 or not math.isfinite(v):
            self._emit("error", f"{AXES[axis]} step size must be > 0; ignored")
            raise ValueError("um_per_step must be > 0")
        set_axis_um_per_step(self.cfg, axis, v, direction)
        for d in ((+1, -1) if direction == 0 else (direction,)):
            set_calibration_amplitude(self.cfg, axis, d, axis_amplitude(self.cfg, axis, d))
        way = {1: " forward", -1: " backward"}.get(direction, "")
        self._emit("info", f"{AXES[axis]}{way} step size = {v * 1000:.4g} nm/step "
                           f"(at amplitude {axis_amplitude(self.cfg, axis, direction or 1)})")
        return v

    def set_leash(self, enabled=None, leash_steps=None) -> dict:
        """Configure the travel LEASH -- a symmetric box around the datum.

        ``None`` keeps a value, so the leash can be toggled without touching
        the range and resized without toggling.
        """
        lim = self.cfg.limits
        if enabled is not None:
            lim.leash_enabled = bool(enabled)
        if leash_steps is not None:
            lim.leash_steps = max(0, int(leash_steps))
        self._emit("info", f"leash {'ON' if lim.leash_enabled else 'off'}: "
                           f"+/-{lim.leash_steps} steps from datum")
        return {"enabled": lim.leash_enabled, "leash_steps": lim.leash_steps}

    # ------------------------------------------------------------------ #
    # fly-scan stream (the poll thread records the position)
    # ------------------------------------------------------------------ #
    def stream_start(self, rate_hz: float | None = None) -> int:
        """Record both axes' position estimate, time stamped, from now on.

        The poll thread does the sampling, sped up to ``rate_hz`` (max
        POLL_HZ_MAX) while the stream runs. The "measured" position here is
        the step counter booked with the calibration -- the best this
        open-loop stage has, not a sensor.
        """
        self._stream_hz = min(max(float(rate_hz or 50.0), 1.0), POLL_HZ_MAX)
        return self.stream.start()

    def stream_stop(self) -> dict:
        return self.stream.stop()

    # ------------------------------------------------------------------ #
    # position list (stored in STEPS)
    # ------------------------------------------------------------------ #
    def store_position(self, slot: int, name: str = "") -> dict:
        self._need_connection()
        with self._io:
            pos = [self._fresh_count(a) for a in range(2)]
        p = self.positions.store(slot, pos[0], pos[1], name=name)
        self._emit("info", f"stored slot {slot} '{p.name}' = ({pos[0]}, {pos[1]}) steps")
        return asdict(p)

    def clear_position(self, slot: int) -> None:
        self.positions.clear(slot)
        self._emit("info", f"cleared slot {slot}")

    def goto_position(self, slot: int) -> list:
        p = self.positions.get(slot)
        if not p.used:
            self._emit("warn", f"slot {slot} is empty")
            return []
        targets = [self.move_to_step(0, int(p.x)), self.move_to_step(1, int(p.y))]
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
        """Re-push the amplitudes after the config was edited in place.

        Skipped for an axis that is moving (SU is refused then); the config
        still holds the new value and the next apply/start pushes it.
        """
        if not self._connected:
            return
        with self._io:
            for axis in range(2):
                hw = self._hw(axis)
                if int(self.backend.axis_state(hw)) != READY:
                    self._emit("warn", f"{AXES[axis]} moving: amplitude not pushed")
                    continue
                for d in (+1, -1):
                    raw = int(axis_amplitude(self.cfg, axis, d))
                    v = min(max(raw, AMPLITUDE_MIN), AMPLITUDE_MAX)
                    if v != raw:
                        # write the clamped value back, or status would report
                        # an amplitude the controller is not using
                        set_axis_amplitude(self.cfg, axis, d, v)
                        self._emit("warn", f"{AXES[axis]} amplitude {raw} clamped to {v}")
                    self.backend.set_amplitude(hw, d, v)
                if not self.cal_valid(axis):
                    self._emit("warn", f"{AXES[axis]} amplitude differs from the one the step "
                                       "size was measured at: um are approximate")
        self._emit("info", "config applied")
