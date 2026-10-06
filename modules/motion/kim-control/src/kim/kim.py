"""The brain: :class:`Kim` (§5 of the guide, set-and-forget family).

A 3-axis piezo-inertia stage is *set-and-forget*: you command a target and the
KIM101 steps the actuator there on its own -- there is no control loop for us to
run (contrast the closed-loop magnet).  So this brain has no threads and no
state machine.  It:

  * holds the config and owns the STEP <-> MICROMETRE conversion,
  * clamps every request to the safety envelope (``cfg.limits``),
  * pushes accepted values to the backend (which speaks STEPS only),
  * owns the relative "zero here" read-out origin,
  * owns the 20-slot position list (stored in STEPS),
  * reports a :class:`KimStatus` snapshot that must NEVER throw.

Motion is fire-and-forget: ``move_*`` returns once the command is accepted; the
caller polls ``status()`` to watch it settle.

------------------------------------------------------------------------------
Two languages, one bridge (the reason this module exists)
------------------------------------------------------------------------------
The hardware counts STEPS.  You think in MICROMETRES.  ``um_per_step`` (per
axis, in :class:`~kim.config.Calibration`) bridges them:

    steps        = round(um / um_per_step)
    um           = steps * um_per_step
    step_rate    = round(um_per_s / um_per_step)   [steps/s, clamped to 2000]

Every motion verb comes in BOTH languages so you can drive the stage however you
are thinking at the moment:

    STEP language      | MICROMETRE language
    -------------------+----------------------------------------------
    move_to_step       | move_to_um          (absolute)
    move_steps         | move_relative_um    (incremental, from HERE)
    set_step_rate      | set_velocity_um
    set_acceleration   | (steps/s^2 only -- accel stays native)

Two distinct "zeros":
  * ``zero_counter``  -- HARDWARE datum: reset the controller's step counter to 0
                         at the current physical position (moves the absolute
                         origin).  This is the closest thing to "home".
  * ``set_zero``      -- SOFTWARE display origin: a bench-DRO reference for the
                         relative read-out only; commands nothing on the motor.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config as _config
from . import pxcal
from .backends.base import KimBackend
from .config import (
    Config,
    axis_acceleration,
    axis_effective_limits,
    axis_leash_half,
    axis_rate,
    axis_rel_origin,
    axis_um_per_step,
    axis_voltage,
    set_axis_acceleration,
    set_axis_rate,
    set_axis_rel_origin,
    set_axis_um_per_step,
    set_axis_voltage,
)
from .positions import PositionList
from .stream import StreamRecorder

AXES = ("X", "Y", "Z")

#: Fly-scan stream channels: the absolute position of each axis in um, the
#: same number `position_um` in status (and the `position_x` control) reads.
STREAM_CHANNELS = ("x", "y", "z")

#: Default and highest position sampling rate of the stream, Hz. # VERIFY on
#: the KIM101: each sample is three USB round trips under the backend's lock,
#: shared with the status publisher and the moves; if the link cannot keep up
#: the loop simply runs slower (every sample is time stamped, so a slower
#: stream is coarser, never wrong).
STREAM_HZ = 50.0
STREAM_HZ_MAX = 500.0


@dataclass
class KimStatus:
    """A snapshot of everything the GUI / remote client needs.

    Lists are length-3, axis-indexed (X, Y, Z).  ``status()`` guarantees this
    object is always returned, even if a hardware read fails mid-way.
    """

    position_steps: list  # absolute step counter per axis
    position_um: list     # position in micrometres (steps * um_per_step)
    rel_steps: list       # steps measured from the software zero origin
    rel_um: list          # micrometres measured from the software zero origin
    rel_origin: list      # the software zero origin per axis, in steps
    moving: list          # bool per axis
    step_rate: list       # steps/s per axis
    velocity_um: list     # um/s per axis (step_rate * um_per_step)
    acceleration: list    # steps/s^2 per axis
    voltage: list         # V per axis (sets physical step size)
    um_per_step: list     # calibration per axis (mean of both directions)
    um_per_step_fwd: list # forward / backward step size per axis: they DIFFER on a
    um_per_step_bwd: list # slip-stick actuator (Y- was 1.7x Y+ at 85 V on this rig)
    um_per_step_src: list # "camera" (measured px/step x pixel size) or "config"
    limit_lo: list        # effective lower step bound per axis (what we clamp to)
    limit_hi: list        # effective upper step bound per axis
    leash: bool           # is the leash (symmetric box around datum) active?
    leash_half: list      # +/- leash half-range per axis, steps (for display)
    speed_fast: bool      # movement preset: True = fast, False = slow
    step_large: bool      # step-size preset: True = large (max V), False = small (min V)
    connected: bool
    # The TARGET ECHO, um per axis (2026-09-28): where the last move command
    # sent each axis, as the caller asked for it -- an absolute move echoes the
    # requested number unrounded, a relative move / jog / goto the resulting
    # target. A scan waits until this equals what it asked for AND `moving` is
    # False (scan-core's adopt_then_flag); without it, the status frame from
    # BEFORE the move ("not moving") looked like "arrived" (gotcha #2).
    target_um: list = None
    # "" while the controller answers; the error text while the last read
    # failed. A scan pauses on a non-empty one, and the GUI shows it in red.
    hw_error: str = ""
    px_calibrated: bool = False   # is a camera px/step table loaded?
    calib_running: bool = False   # is the camera calibration running right now?
    calib_progress: str = ""      # its latest progress / result / error line


class Kim:
    def __init__(self, backend: KimBackend, cfg: Config):
        self.backend = backend
        self.cfg = cfg
        self.positions = PositionList()
        self._connected = False
        # Front-panel preset state (inferred from config so the toggle buttons
        # start in the mode that matches the loaded values).
        m = cfg.motion
        self._speed_fast = abs(m.rate_x - m.fast_rate) <= abs(m.rate_x - m.slow_rate)
        v = cfg.motion.voltage_x
        self._step_large = abs(v - cfg.limits.max_voltage) <= abs(v - cfg.limits.min_voltage)
        # The service replaces this hook to forward events onto the wire; the
        # GUI replaces it to append to its log.  Levels: "info"|"warn"|"error".
        self._on_event = lambda level, msg: None
        # Camera-frame calibration (px/step table) + the thread measuring it.
        self._pxcal: pxcal.PxCalibration | None = None
        self._calibrator = None
        self._calib_thread: threading.Thread | None = None
        self._calib_progress = ""
        # The fly-scan position record (stream.py). This brain has no poll
        # thread of its own (status is read on demand), so streaming starts a
        # small sampler thread, and only while a scan wants it.
        self.stream = StreamRecorder(STREAM_CHANNELS)
        self._stream_thread: threading.Thread | None = None
        self._stream_stop = threading.Event()
        # Target echo per axis (um), see KimStatus.target_um. None until
        # start() adopts the current position. Written ONLY by the move paths,
        # always AFTER the hardware accepted the move (see _set_target).
        self._target_um: list = [None, None, None]
        # Hardware-read health: the last good raw readings (so a failed read
        # repeats them instead of publishing zeros) and the current error
        # text. The lock makes "first failure -> one error event" and
        # "recovered -> one info event" happen once even when two threads
        # (status publisher, describe, the GUI) read status at the same time.
        self._hw_error = ""
        self._hw_lock = threading.Lock()
        self._last_good: dict | None = None
        # Where save_calibration / save_config write. None = kim.ini in the
        # project folder; run_service.py sets it to the file it loaded (the
        # --config file, if one was given), so a save lands where the next
        # start will read it.
        self.config_path: Path | None = None
        try:
            self._pxcal = pxcal.load(self.px_file())
        except Exception as exc:
            self._calib_progress = f"could not read {self.px_file()}: {exc}"

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
        """Calibration for ``axis`` (um moved per step).  Always > 0.

        PREFERS the camera measurement when there is one (`pxcal.um_per_step`:
        the px/step table times the pixel size it was measured at), because the
        configured number is a datasheet figure -- 20 nm for every axis and both
        directions, which is why a +-2 um jog used to send the same 100 steps
        everywhere. `calibration.use_px_calibration = false` forces the
        configured value back.

        `direction` +1/-1 picks the measured forward/backward step size; 0 asks
        for the mean, which is what a POSITION readout or a speed must use --
        there is no direction yet, and an absolute counter on an asymmetric
        open-loop axis is nominal anyway. Z always uses the config (no camera
        calibration for it).
        """
        if self.cfg.calibration.use_px_calibration and self._pxcal is not None:
            try:
                v = pxcal.um_per_step(self._pxcal, axis, direction,
                                      self.cfg.motion.voltage_x, self.cfg.motion.voltage_y)
            except Exception:
                v = None
            if v:
                return v
        # No camera table (or it is switched off): the CONFIGURED numbers, which
        # can themselves be per direction -- measure a stage without a camera and
        # you can still tell the module it steps 21 nm out and 15 nm back.
        v = axis_um_per_step(self.cfg, axis, direction)
        return v if v > 0 else 1e-9  # never divide by zero downstream

    def um_per_step_source(self, axis: int) -> str:
        """Where this axis's step size comes from: "camera" or "config"."""
        if self.cfg.calibration.use_px_calibration and self._pxcal is not None:
            try:
                if pxcal.um_per_step(self._pxcal, axis, 0,
                                     self.cfg.motion.voltage_x, self.cfg.motion.voltage_y):
                    return "camera"
            except Exception:
                pass
        return "config"

    def um_to_steps(self, axis: int, um: float, directional: bool = False) -> int:
        """Convert a micrometre distance/position to an integer step count.

        `directional=True` for a RELATIVE move: the sign of `um` says which way
        the axis will travel, so the step size of THAT direction applies.
        """
        value = float(um)
        direction = (1 if value > 0 else -1 if value < 0 else 0) if directional else 0
        return int(round(value / self.um_per_step(axis, direction)))

    def steps_to_um(self, axis: int, steps: float) -> float:
        """Convert a step count to micrometres (mean step size: see um_per_step)."""
        return float(steps) * self.um_per_step(axis)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Open the backend and ADOPT the controller's drive parameters.

        Rule (Lukas, 2026-09-27, every module): starting the software must not
        change the instrument. The KIM101 remembers its step rate, acceleration
        and drive voltage per channel; whoever set them (Kinesis, a previous
        session, a colleague) had a reason. So we READ them and copy them into
        ``cfg.motion`` -- the brain's live drive state -- instead of pushing the
        .ini values over them. The .ini numbers are now only defaults that the
        user applies explicitly (Settings, set_config, a preset, a setter).

        Why the copy into cfg matters: the voltage picks the row of the camera
        px/step table (``um_per_step``), and the presets / Settings read cfg.
        Adopting only into the status would leave the um conversion computed at
        the .ini voltage while the stage steps at the real one.
        """
        self.backend.open()
        self._connected = True
        self._adopt_drive_state()
        self._adopt_targets()
        self.status()      # a first good reading, so a link lost later freezes on it, not on 0
        self._emit("info", f"kim started ({self.backend.idn()})")

    def _adopt_targets(self) -> None:
        """Target echo at start = where each axis IS (a query, no move).

        Nothing has been commanded yet, so "the target" is the current
        position; a scan asking for exactly this position then settles at
        once, which is right: the stage is already there.
        """
        for axis in range(3):
            try:
                self._target_um[axis] = self.steps_to_um(
                    axis, int(self.backend.read_position(axis)))
            except Exception:
                self._target_um[axis] = None      # unknown; the next move sets it

    def _set_target(self, axis: int, target_um: float) -> None:
        """Store the target echo. CALL ONLY AFTER backend.move_to() RETURNED.

        THE ORDERING RULE that makes the echo trustworthy (two threads: the
        commander runs the move verbs, the publisher builds status):
          1. the setter sends the move to the hardware FIRST, and stores the
             new echo target only AFTERWARDS (here);
          2. status() reads the echo target BEFORE it asks the hardware
             whether the axis is moving.
        So a status frame that shows the NEW target read `moving` after the
        move command had been accepted -- never the stale "not moving" from
        before it. (Reverse either order and a frame can pair the new target
        with the old "not moving": the scan would take the point before the
        stage left.)

        # VERIFY on the KIM101: this relies on the controller reporting
        # "moving" as soon as it has ACCEPTED a move (pylablib's move_to
        # returns once the command is sent). If the first is_moving() after a
        # move can still say False, a scan could settle one frame early; then
        # add a hold-off here (e.g. poll is_moving until it turns True, with a
        # short timeout for zero-length moves).
        """
        self._target_um[axis] = float(target_um)

    def _adopt_drive_state(self) -> None:
        """Read rate / acceleration / voltage per axis into cfg.motion. Queries only."""
        lim = self.cfg.limits
        found = []
        for axis in range(3):
            try:
                rate = float(self.backend.read_step_rate(axis))
                acc = float(self.backend.read_acceleration(axis))
                volt = float(self.backend.read_voltage(axis))
            except Exception as exc:
                # Keep the config's numbers for DISPLAY, but say so: they were
                # not written, so they may not be what the controller does.
                self._emit("warn", f"{AXES[axis]}: could not read the drive parameters "
                                   f"({exc}); showing the config values, nothing was written")
                continue
            set_axis_rate(self.cfg, axis, rate)
            set_axis_acceleration(self.cfg, axis, acc)
            set_axis_voltage(self.cfg, axis, volt)
            found.append(f"{AXES[axis]} {volt:g} V / {rate:g} steps/s / {acc:g} steps/s^2")
            # Outside our safety window? Report it, do not "fix" it: correcting
            # it would be exactly the start-up write this rule forbids. The next
            # explicit set_* call clamps as usual.
            if not lim.min_voltage <= volt <= lim.max_voltage:
                self._emit("warn", f"{AXES[axis]} drive voltage {volt:g} V is outside "
                                   f"{lim.min_voltage:g}..{lim.max_voltage:g} V (left as found)")
            if not 1.0 <= rate <= lim.max_step_rate or not 1.0 <= acc <= lim.max_acceleration:
                self._emit("warn", f"{AXES[axis]} rate/acceleration outside the configured "
                                   f"limits (left as found)")
        # The front-panel toggles show whichever preset the ADOPTED values are
        # closer to (they only mirror; pressing one is what writes).
        m = self.cfg.motion
        self._speed_fast = abs(m.rate_x - m.fast_rate) <= abs(m.rate_x - m.slow_rate)
        self._step_large = (abs(m.voltage_x - lim.max_voltage)
                            <= abs(m.voltage_x - lim.min_voltage))
        if found:
            self._emit("info", "adopted from the controller: " + "; ".join(found))

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Stop all motion and close the backend.  Idempotent.

        ``keep_outputs`` (shutdown{keep_outputs: true}, a restart for a code
        update) changes nothing here: stopping motion is a SAFETY step that a
        restart keeps (no move may run on unsupervised), and nothing is moved
        back, homed or parked either way -- the next start adopts the position.
        (A running px calibration is aborted either way; its own finally puts
        back the drive voltages it found -- the user's setting, not a new one.)
        """
        if not self._connected:
            return
        self.stream_stop()
        self.abort_px_calibration()
        if self._calib_thread is not None:
            self._calib_thread.join(timeout=5.0)
        try:
            for axis in range(3):
                try:
                    self.backend.stop(axis)
                except Exception:
                    pass
        finally:
            self.backend.close()
            self._connected = False
        self._emit("info", "kim shut down")

    # ------------------------------------------------------------------ #
    # fly-scan stream (position recorded continuously)
    # ------------------------------------------------------------------ #
    def stream_start(self, rate_hz: float | None = None) -> int:
        """Record the position of all three axes from now on, time stamped.

        For scan-core's fly scan, which bins a detector by where the stage
        MEASURABLY was rather than where it was sent. On this open-loop
        stage "measured" means the controller's step counter -- the best
        readback it has, but not a sensor: slip-stick steps vary, so the
        counter drifts from the true position over a long scan (seen on the
        rig: ~26 um over a 525-point raster). A fly image is then regular
        in COUNTS, which is exactly as true as a stepped image here.
        """
        self.stream_stop()
        rate = min(max(float(rate_hz or STREAM_HZ), 1.0), STREAM_HZ_MAX)
        sid = self.stream.start()
        self._stream_stop.clear()
        self._stream_thread = threading.Thread(
            target=self._stream_loop, args=(1.0 / rate,), name="kim-stream", daemon=True)
        self._stream_thread.start()
        return sid

    def stream_stop(self) -> dict:
        """Stop the sampler and return what it recorded since the last read."""
        self._stream_stop.set()
        th, self._stream_thread = self._stream_thread, None
        if th is not None and th is not threading.current_thread():
            th.join(timeout=2.0)
        return self.stream.stop()

    def _stream_loop(self, period: float) -> None:
        next_t = time.monotonic()
        while not self._stream_stop.is_set():
            try:
                # The three reads are one USB round trip each, one after the
                # other. The row gets ONE time stamp, so use the MIDDLE of the
                # read window: stamping after the last read (as before
                # 2026-09-28) filed X -- read first -- 2.5 round trips late,
                # i.e. a moving stage a little ahead of where it was read.
                t_first = time.time()
                steps = [self.backend.read_position(a) for a in range(3)]
                t = 0.5 * (t_first + time.time())
                self.stream.append(t, [self.steps_to_um(a, steps[a]) for a in range(3)])
            except Exception:
                pass                      # a failed read is a gap, not a crash
            next_t += period
            wait = next_t - time.monotonic()
            if wait > 0:
                time.sleep(wait)          # high-resolution on Windows, unlike Event.wait
            else:
                next_t = time.monotonic()

    def status(self) -> KimStatus:
        """Snapshot of live state.  NEVER raises.

        A failed hardware read is LOUD (Lukas, 2026-09-28): `hw_error` carries
        the message, `connected` goes False, and the positions / drive values
        are the LAST GOOD ones -- not zeros, which looked like a live stage at
        the origin. `moving` is reported True on every axis while the link is
        down: "at rest" cannot be confirmed, and a scan that trusted a stale
        False would settle on a dead link. One error event per failure
        episode (not one per status frame), one info event when it recovers.
        """
        # ORDERING RULE (see _set_target): the echo target is read BEFORE the
        # hardware is asked whether the axis moves.
        targets = list(self._target_um)
        try:
            pos = [int(self.backend.read_position(a)) for a in range(3)]
            moving = [bool(self.backend.is_moving(a)) for a in range(3)]
            rate = [float(self.backend.read_step_rate(a)) for a in range(3)]
            acc = [float(self.backend.read_acceleration(a)) for a in range(3)]
            volt = [float(self.backend.read_voltage(a)) for a in range(3)]
            self._last_good = {"pos": pos, "rate": rate, "acc": acc, "volt": volt}
            self._hw_ok()
        except Exception as exc:
            self._hw_failed(exc)
            good = self._last_good or {"pos": [0, 0, 0], "rate": [0.0] * 3,
                                       "acc": [0.0] * 3, "volt": [0.0] * 3}
            pos, rate = list(good["pos"]), list(good["rate"])
            acc, volt = list(good["acc"]), list(good["volt"])
            moving = [True] * 3
        hw_error = self._hw_error
        cal = [self.um_per_step(a) for a in range(3)]
        rel_origin = [axis_rel_origin(self.cfg, a) for a in range(3)]
        rel_steps = [pos[a] - rel_origin[a] for a in range(3)]
        bounds = [axis_effective_limits(self.cfg, a) for a in range(3)]
        return KimStatus(
            position_steps=pos,
            position_um=[self.steps_to_um(a, pos[a]) for a in range(3)],
            rel_steps=rel_steps,
            rel_um=[self.steps_to_um(a, rel_steps[a]) for a in range(3)],
            rel_origin=rel_origin,
            moving=moving,
            step_rate=rate,
            velocity_um=[rate[a] * cal[a] for a in range(3)],
            acceleration=acc,
            voltage=volt,
            um_per_step=cal,
            um_per_step_fwd=[self.um_per_step(a, +1) for a in range(3)],
            um_per_step_bwd=[self.um_per_step(a, -1) for a in range(3)],
            um_per_step_src=[self.um_per_step_source(a) for a in range(3)],
            limit_lo=[b[0] for b in bounds],
            limit_hi=[b[1] for b in bounds],
            leash=bool(self.cfg.limits.leash_enabled),
            leash_half=[axis_leash_half(self.cfg, a) for a in range(3)],
            speed_fast=bool(self._speed_fast),
            step_large=bool(self._step_large),
            connected=bool(self._connected and not hw_error),
            target_um=[None if t is None else float(t) for t in targets],
            hw_error=hw_error,
            px_calibrated=self._pxcal is not None,
            calib_running=self.calibration_running(),
            calib_progress=self._calib_progress,
        )

    def _hw_failed(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        with self._hw_lock:
            first = not self._hw_error
            self._hw_error = msg
        if first:                         # once per episode, not once per frame
            self._emit("error", f"hardware read failed: {msg} -- positions frozen at the "
                                f"last good reading; is the KIM101 still connected?")

    def _hw_ok(self) -> None:
        with self._hw_lock:
            was = self._hw_error
            self._hw_error = ""
        if was:
            self._emit("info", "hardware link recovered")

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

    def _clamp_steps_quiet(self, axis: int, value: int) -> int:
        """Where `_clamp_steps` would send `value`, without the warn event."""
        value = int(round(value))
        if not self.cfg.limits.enforce:
            return value
        lo, hi = axis_effective_limits(self.cfg, axis)
        return min(max(value, lo), hi)

    def _clamp_range(self, value: float, lo: float, hi: float, what: str) -> float:
        value = float(value)
        if not self.cfg.limits.enforce:
            return value
        if value < lo:
            self._emit("warn", f"{what} {value:.4g} clamped to {lo:.4g}")
            return lo
        if value > hi:
            self._emit("warn", f"{what} {value:.4g} clamped to {hi:.4g}")
            return hi
        return value

    # ------------------------------------------------------------------ #
    # motion verbs -- STEP language
    # ------------------------------------------------------------------ #
    def move_to_step(self, axis: int, position_steps: int) -> int:
        """Move ONE axis to an ABSOLUTE step position (clamped to limits)."""
        return self._move_to_step(axis, position_steps, None)

    def _move_to_step(self, axis: int, position_steps, requested_um) -> int:
        """The one absolute move: clamp, send, THEN store the echo target.

        `requested_um` = the micrometre value the caller asked for (move_to_um),
        echoed unrounded so a scan's "is my setpoint adopted?" check matches
        exactly. If the clamp changed the target, the echo is where the stage
        is really going: a scan asking for an unreachable point then never sees
        its setpoint adopted and times out -- loud, instead of settling
        somewhere else.
        """
        self._guard_calibration("move")
        wanted = int(round(position_steps))
        target = self._clamp_steps(axis, wanted)
        self.backend.move_to(axis, target)                # 1) hardware first ...
        if requested_um is not None and target == wanted:
            self._set_target(axis, float(requested_um))   # 2) ... then the echo
        else:
            self._set_target(axis, self.steps_to_um(axis, target))
        self._emit("info", f"move {AXES[axis]} -> {target} steps")
        return target

    def move_steps(self, axis: int, delta_steps: int) -> int:
        """Move ONE axis BY a number of steps from its current position.

        This is the natural piezo-inertia operation: "step N times from here".
        The resulting absolute target is clamped to the travel limits.
        """
        self._guard_calibration("move")
        current = int(self.backend.read_position(axis))
        target = self._clamp_steps(axis, current + int(round(delta_steps)))
        self.backend.move_to(axis, target)                       # hardware first,
        self._set_target(axis, self.steps_to_um(axis, target))   # then the echo
        self._emit("info", f"move {AXES[axis]} by {int(round(delta_steps))} steps -> {target}")
        return target

    # ------------------------------------------------------------------ #
    # motion verbs -- MICROMETRE language (bridged through the calibration)
    # ------------------------------------------------------------------ #
    def move_to_um(self, axis: int, position_um: float) -> int:
        """Move ONE axis to an ABSOLUTE micrometre position.

        The micrometre value is converted to an absolute step target through the
        axis calibration, then clamped.  Returns the (clamped) step target.
        """
        steps = self.um_to_steps(axis, position_um)
        self._emit("info", f"move {AXES[axis]} -> {float(position_um):.4g} um")
        return self._move_to_step(axis, steps, float(position_um))

    def move_relative_um(self, axis: int, delta_um: float) -> int:
        """Move ONE axis BY a micrometre distance from its current position.

        This is the headline "relative movement in micrometres": the distance is
        converted to a step count via the datasheet calibration and issued as an
        incremental step move.  Returns the (clamped) absolute step target.
        """
        # directional: a +2 um jog and a -2 um jog are NOT the same step count
        # on a slip-stick axis, and the camera measured both.
        delta_steps = self.um_to_steps(axis, delta_um, directional=True)
        self._emit("info", f"move {AXES[axis]} by {float(delta_um):.4g} um ({delta_steps} steps, "
                           f"{self.um_per_step_source(axis)})")
        return self.move_steps(axis, delta_steps)

    def stop(self, axis: int) -> None:
        self.abort_px_calibration()      # STOP always wins over a running calibration
        self.backend.stop(axis)
        self._target_after_stop(axis)
        self._emit("warn", f"stop {AXES[axis]}")

    def stop_all(self) -> None:
        self.abort_px_calibration()
        for axis in range(3):
            self.backend.stop(axis)
            self._target_after_stop(axis)
        self._emit("warn", "STOP all axes")

    def _target_after_stop(self, axis: int) -> None:
        """After a STOP the target is where the axis stopped, not where it was
        going: a scan still waiting for the old target must NOT see it echoed
        together with "not moving" and take the point short of it -- it keeps
        waiting (and times out, loudly). Same order as a move: stop first."""
        try:
            self._set_target(axis, self.steps_to_um(axis, int(self.backend.read_position(axis))))
        except Exception:
            self._target_um[axis] = None

    # ------------------------------------------------------------------ #
    # datum + relative read-out origin (the two "zeros")
    # ------------------------------------------------------------------ #
    def zero_counter(self, axis: int) -> None:
        """HARDWARE datum: reset the controller step counter to 0 here."""
        self._guard_calibration("reset the counter")
        self.backend.zero_counter(axis)
        set_axis_rel_origin(self.cfg, axis, 0)  # the display origin follows
        # the coordinates moved under the old target: the target is "here" = 0
        self._set_target(axis, 0.0)
        self._emit("info", f"{AXES[axis]} step counter zeroed (datum set here)")

    def zero_counter_all(self) -> None:
        for axis in range(3):
            self.zero_counter(axis)
        self._emit("info", "all step counters zeroed")

    def set_zero(self, axis: int) -> int:
        """SOFTWARE display origin: mark the current step count as relative 0."""
        current = int(self.backend.read_position(axis))
        set_axis_rel_origin(self.cfg, axis, current)
        self._emit("info", f"{AXES[axis]} display zeroed here (origin = {current} steps)")
        return current

    def set_zero_all(self) -> list:
        origins = [self.set_zero(a) for a in range(3)]
        self._emit("info", "all axes display-zeroed at current position")
        return origins

    def clear_zero(self, axis: int) -> None:
        """Drop the display origin back to the absolute step counter."""
        set_axis_rel_origin(self.cfg, axis, 0)
        self._emit("info", f"{AXES[axis]} display origin cleared (back to absolute)")

    def clear_zero_all(self) -> None:
        for axis in range(3):
            set_axis_rel_origin(self.cfg, axis, 0)
        self._emit("info", "display origins cleared")

    # ------------------------------------------------------------------ #
    # parameter verbs
    # ------------------------------------------------------------------ #
    def set_step_rate(self, axis: int, steps_per_sec: float) -> float:
        """Set the step rate (steps/s), clamped to 1..max_step_rate."""
        v = self._clamp_range(steps_per_sec, 1.0, self.cfg.limits.max_step_rate,
                              f"{AXES[axis]} step rate")
        set_axis_rate(self.cfg, axis, v)
        self.backend.set_step_rate(axis, v)
        self._emit("info", f"{AXES[axis]} step rate = {v:.4g} steps/s")
        return v

    def set_velocity_um(self, axis: int, um_per_sec: float) -> float:
        """Set velocity in um/s -- converted to a step rate via the calibration.

        Returns the ACTUAL um/s achieved after the step rate was clamped to the
        controller's 1..max_step_rate window (so the caller sees the truth, not
        just what it asked for).
        """
        want_rate = float(um_per_sec) / self.um_per_step(axis)
        rate = self._clamp_range(want_rate, 1.0, self.cfg.limits.max_step_rate,
                                f"{AXES[axis]} step rate")
        set_axis_rate(self.cfg, axis, rate)
        self.backend.set_step_rate(axis, rate)
        actual_um = rate * self.um_per_step(axis)
        self._emit("info", f"{AXES[axis]} velocity = {actual_um:.4g} um/s ({rate:.4g} steps/s)")
        return actual_um

    def set_acceleration(self, axis: int, steps_per_sec2: float) -> float:
        """Set the step acceleration (steps/s^2), clamped to 1..max."""
        a = self._clamp_range(steps_per_sec2, 1.0, self.cfg.limits.max_acceleration,
                             f"{AXES[axis]} acceleration")
        set_axis_acceleration(self.cfg, axis, a)
        self.backend.set_acceleration(axis, a)
        self._emit("info", f"{AXES[axis]} acceleration = {a:.4g} steps/s^2")
        return a

    def set_voltage(self, axis: int, volts: float) -> float:
        """Set the piezo drive voltage (V) -- this sets the physical step SIZE.

        Clamped to the KIM101's min_voltage..max_voltage window.  Changing the
        voltage changes um-per-step, so re-measure the calibration afterwards.
        """
        self._guard_calibration("change the drive voltage")
        v = self._clamp_range(volts, self.cfg.limits.min_voltage, self.cfg.limits.max_voltage,
                             f"{AXES[axis]} voltage")
        self.backend.set_voltage(axis, v)
        # Keep what the controller ACCEPTED, not what we asked for: the KIM101
        # takes whole volts, and cfg's voltage picks the row of the camera
        # px/step table -- um must be converted at the voltage the stage runs at.
        try:
            v = float(self.backend.read_voltage(axis))
        except Exception:
            pass                          # no read-back: the requested value
        set_axis_voltage(self.cfg, axis, v)
        self._emit("info", f"{AXES[axis]} drive voltage = {v:.4g} V (re-check calibration)")
        return v

    def set_calibration(self, axis: int, um_per_step: float, direction: int = 0) -> float:
        """Set the um-per-step calibration for an axis (must be > 0).

        `direction` 0 = both ways (one number, the datasheet case), +1 forward
        only, -1 backward only -- so a stage measured by hand, with no camera,
        can still be told that it steps further one way than the other.
        """
        v = float(um_per_step)
        if v <= 0:
            self._emit("error", f"{AXES[axis]} calibration must be > 0; ignored")
            raise ValueError("um_per_step must be > 0")
        set_axis_um_per_step(self.cfg, axis, v, direction)
        way = {1: " forward", -1: " backward"}.get(direction, "")
        self._emit("info", f"{AXES[axis]}{way} calibration = {v:.5g} um/step")
        if self.um_per_step_source(axis) == "camera":
            # Saying nothing here would be the worst outcome: the number is
            # stored, the readout does not move, and nobody knows why.
            self._emit("warn", f"{AXES[axis]} is using the CAMERA calibration "
                               f"({self.um_per_step(axis) * 1000:.1f} nm/step); this value "
                               f"applies only with calibration.use_px_calibration = false")
        return v

    def set_leash(self, enabled=None, leash_xy=None, leash_z=None) -> dict:
        """Configure the travel LEASH -- a symmetric box around the datum.

        Any argument left as ``None`` keeps its current value, so you can toggle
        the leash on/off without touching the ranges, or resize it without
        toggling.  Ranges are +/- steps FROM THE DATUM (the counter's 0), X and Y
        sharing ``leash_xy`` and Z using ``leash_z``.  When on, the leash is what
        the brain clamps to (it replaces the absolute step limits).  Returns the
        resulting {enabled, leash_xy, leash_z}.
        """
        lim = self.cfg.limits
        if enabled is not None:
            lim.leash_enabled = bool(enabled)
        if leash_xy is not None:
            lim.leash_xy = max(0, int(leash_xy))
        if leash_z is not None:
            lim.leash_z = max(0, int(leash_z))
        state = "ON" if lim.leash_enabled else "off"
        self._emit(
            "info",
            f"leash {state}: XY +/-{lim.leash_xy}, Z +/-{lim.leash_z} steps from datum",
        )
        return {"enabled": lim.leash_enabled, "leash_xy": lim.leash_xy, "leash_z": lim.leash_z}

    # ------------------------------------------------------------------ #
    # front-panel presets (one toggle applies to all three axes)
    # ------------------------------------------------------------------ #
    def set_speed(self, fast: bool) -> dict:
        """Movement preset: push the fast (or slow) rate + acceleration to all
        axes.  The fast/slow values live in ``cfg.motion`` (editable in
        Settings).  Values are clamped by the per-axis setters."""
        self._speed_fast = bool(fast)
        m = self.cfg.motion
        rate = m.fast_rate if fast else m.slow_rate
        acc = m.fast_accel if fast else m.slow_accel
        for a in range(3):
            self.set_step_rate(a, rate)
            self.set_acceleration(a, acc)
        self._emit("info", f"movement {'FAST' if fast else 'slow'} "
                           f"(rate {rate:.4g} steps/s, accel {acc:.4g} steps/s^2)")
        return {"fast": self._speed_fast, "rate": rate, "accel": acc}

    def set_step_size(self, large: bool) -> dict:
        """Step-size preset: set ALL axes to the highest (large) or lowest
        (small) drive voltage.  A bigger drive voltage makes each inertia step
        physically larger.  Uses the voltage window in ``cfg.limits``."""
        self._step_large = bool(large)
        v = self.cfg.limits.max_voltage if large else self.cfg.limits.min_voltage
        for a in range(3):
            self.set_voltage(a, v)
        self._emit("info", f"{'LARGE' if large else 'small'} steps "
                           f"(drive voltage {v:.4g} V -- re-check calibration)")
        return {"large": self._step_large, "voltage": v}

    # ------------------------------------------------------------------ #
    # camera-frame calibration: image px per step (see pxcal.py, calibration.py)
    # ------------------------------------------------------------------ #
    def px_file(self) -> Path:
        p = Path(self.cfg.calibration.px_file)
        # relative -> the kim-control project folder (src/kim/kim.py -> parents[2])
        return p if p.is_absolute() else Path(__file__).resolve().parents[2] / p

    def calibration_running(self) -> bool:
        return self._calib_thread is not None and self._calib_thread.is_alive()

    def _guard_calibration(self, what: str) -> None:
        """Refuse outside motion while the calibration owns the stage: a move, a
        voltage change or a counter reset in the middle would corrupt the fit.
        The calibration's own thread passes; STOP aborts it instead."""
        if self.calibration_running() and threading.current_thread() is not self._calib_thread:
            raise RuntimeError(f"camera calibration is running: cannot {what} (STOP aborts it)")

    def _calibration_move(self, axis: int, target_steps: int) -> int:
        """The calibrator's move: same clamps and leash, no per-move log line."""
        target = self._clamp_steps(axis, int(target_steps))
        self.backend.move_to(axis, target)
        self._set_target(axis, self.steps_to_um(axis, target))   # after the move
        return target

    def _calibration_voltage(self, axis: int, volts: float) -> float:
        return self.set_voltage(axis, volts)

    def start_px_calibration(self, camera_host: str = "127.0.0.1", camera_port: int = 5563,
                             voltages=(85, 95, 105, 115, 125), repeats: int = 2,
                             **options) -> str:
        """Start measuring the px/step table from camera feedback (background).

        Progress and the final result appear in ``status().calib_progress`` and
        as events; the table is saved to ``px_file()`` and used at once.
        """
        from .calibration import CalibrationAborted, CameraLink, PxCalibrator

        if self.calibration_running():
            raise RuntimeError("camera calibration is already running")
        if not voltages:
            raise ValueError("give at least one voltage")
        lim = self.cfg.limits
        bad = [v for v in voltages if not lim.min_voltage <= float(v) <= lim.max_voltage]
        if bad:
            raise ValueError(f"voltages {bad} outside {lim.min_voltage:g}..{lim.max_voltage:g} V")

        def progress(msg: str) -> None:
            self._calib_progress = msg
            self._emit("info", f"calibration: {msg}")

        camera = CameraLink(camera_host, int(camera_port))
        self._calibrator = PxCalibrator(self, camera, voltages=voltages, repeats=repeats,
                                        progress=progress, **options)

        def work() -> None:
            try:
                cal = self._calibrator.run()
                pxcal.save(cal, self.px_file())
                self._pxcal = cal
                worst = max((c["error_px"] for c in cal.validation), default=0.0)
                self._calib_progress = (f"done: {len(cal.table)} voltages, closed-loop check "
                                        f"worst {worst:.1f} px; saved {self.px_file().name}")
                self._emit("info", f"calibration: {self._calib_progress}")
            except CalibrationAborted:
                self._calib_progress = "aborted"
                self._emit("warn", "calibration aborted (table unchanged)")
            except Exception as exc:
                self._calib_progress = f"failed: {type(exc).__name__}: {exc}"
                self._emit("error", f"calibration {self._calib_progress}")
            finally:
                camera.close()

        self._calib_progress = "starting"
        self._calib_thread = threading.Thread(target=work, name="kim-pxcal", daemon=True)
        self._calib_thread.start()
        self._emit("info", f"camera calibration started: {list(voltages)} V, {repeats} repeats, "
                           f"camera {camera_host}:{camera_port}")
        return "started"

    def abort_px_calibration(self) -> None:
        if self.calibration_running() and self._calibrator is not None:
            self._calibrator.abort()

    def get_px_calibration(self) -> dict | None:
        """The table plus a readable summary at the CURRENT voltages, or None.

        The table itself stays in image PIXELS per step -- that is what was
        measured and what the stabiliser closes its loop on -- but every step
        size is also reported in MICROMETRES (`um_table`, and `um_per_step_*` in
        the geometry), because px/step tells a person nothing about how far the
        stage moves. The conversion is the camera pixel size stored in the file.
        """
        if self._pxcal is None:
            return None
        px_um = float(self._pxcal.pixel_size_um or 0.0)
        d = self._pxcal.to_dict()
        d["um_table"] = {
            volts: {direction: (math.hypot(float(col[0]), float(col[1])) * px_um
                                if px_um else None)
                    for direction, col in cols.items()}
            for volts, cols in self._pxcal.table.items()
        }
        cols = pxcal.columns_at(self._pxcal, self.cfg.motion.voltage_x, self.cfg.motion.voltage_y)
        d["now"] = {"voltage": [self.cfg.motion.voltage_x, self.cfg.motion.voltage_y],
                    "columns": {k: v.tolist() for k, v in cols.items()},
                    "geometry": pxcal.axis_geometry(cols, px_um),
                    # what the um <-> steps bridge is actually using right now
                    "um_per_step": {"X": [self.um_per_step(0, +1), self.um_per_step(0, -1)],
                                    "Y": [self.um_per_step(1, +1), self.um_per_step(1, -1)],
                                    "Z": [self.um_per_step(2), self.um_per_step(2)]},
                    "source": [self.um_per_step_source(a) for a in range(3)]}
        d["file"] = str(self.px_file())
        return d

    def move_image_px(self, dx: float, dy: float, context: dict | None = None) -> list:
        """Move X/Y so the CAMERA IMAGE shifts by (dx, dy) pixels.

        Uses the px/step table at each axis's current voltage and the direction
        each axis has to go. ``context`` is the caller's image geometry
        (objective, rotation, symmetry, clip, frame size); it must match what
        the table was measured under, or the move is refused. Returns the signed
        [x, y] steps issued.
        """
        if self._pxcal is None:
            raise RuntimeError("no camera px/step calibration: run the camera calibration first")
        if context:
            stored = self._pxcal.context
            diff = {k: (stored.get(k), v) for k, v in context.items()
                    if k in stored and stored.get(k) != v}
            if diff:
                raise RuntimeError(f"image geometry differs from the calibration "
                                   f"(stored, given): {diff} -- recalibrate")
        cols = pxcal.columns_at(self._pxcal, self.cfg.motion.voltage_x, self.cfg.motion.voltage_y)
        sx, sy = pxcal.solve_steps((float(dx), float(dy)), cols)
        steps = [int(round(sx)), int(round(sy))]
        for axis, s in enumerate(steps):
            if s:
                self.move_steps(axis, s)
        return steps

    # ------------------------------------------------------------------ #
    # position list (stored in STEPS)
    # ------------------------------------------------------------------ #
    def store_position(self, slot: int, name: str = "") -> dict:
        """Capture the CURRENT step position into a slot."""
        pos = [int(self.backend.read_position(a)) for a in range(3)]
        p = self.positions.store(slot, pos[0], pos[1], pos[2], name=name)
        self._emit("info", f"stored slot {slot} '{p.name}' = ({pos[0]}, {pos[1]}, {pos[2]}) steps")
        from dataclasses import asdict
        return asdict(p)

    def clear_position(self, slot: int) -> None:
        self.positions.clear(slot)
        self._emit("info", f"cleared slot {slot}")

    def goto_position(self, slot: int) -> list:
        """Drive all three axes to a saved slot (absolute step coordinates)."""
        p = self.positions.get(slot)
        if not p.used:
            self._emit("warn", f"slot {slot} is empty")
            return []
        self._guard_calibration("move")
        # An axis already AT its stored count and at rest is left alone. Not
        # just tidiness: the KIM101 drives one channel PAIR at a time, (1,2) =
        # X,Y or (3,4) = Z (backends/kinesis_kim.py), so a zero-length Z move
        # sent right after the X and Y moves enabled Z's pair and STOPPED X and
        # Y a few steps into their moves. (If Z really has to move as well, it
        # still cuts X/Y off -- see "deep cleaning 2026-09-28" in the notes.)
        targets = []
        for axis, value in enumerate((p.x, p.y, p.z)):
            want = int(value)
            try:
                here = (int(self.backend.read_position(axis)) == self._clamp_steps_quiet(axis, want)
                        and not self.backend.is_moving(axis))
            except Exception:
                here = False              # cannot tell: send the move as before
            if here:
                # no move sent; the echo still says where the slot put this axis
                targets.append(self._clamp_steps_quiet(axis, want))
                self._set_target(axis, self.steps_to_um(axis, targets[-1]))
            else:
                targets.append(self.move_to_step(axis, want))
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

    def config_file(self) -> Path:
        """The .ini the save verbs write (and the next start reads)."""
        # _config.default_config_path is looked up at CALL time (not imported
        # by name) so the tests can redirect it away from the project folder.
        return Path(self.config_path) if self.config_path else _config.default_config_path()

    def save_calibration(self, path=None) -> str:
        """Persist the CALIBRATION group (every per-axis, per-direction step
        size, use_px_calibration, px_file) into the .ini, keeping the file's
        other sections as they are.

        WHY a separate verb from save_config: the camera's "Calibrate Z steps"
        writes the measured up/down step sizes with set_calibration and then
        calls this. It must not also freeze whatever leash, limits or drive
        numbers happen to be live at that moment into the file -- a
        calibration save should save the calibration and nothing else.
        """
        p = _config.save_sections(self.cfg, path or self.config_file(), ["Calibration"])
        self._emit("info", f"calibration saved to {p}")
        return str(p)

    def save_config(self, path=None) -> str:
        """Persist the WHOLE config into the .ini (merging: a section this
        version does not know is kept). Written with what is live now, so the
        Motion group holds the ADOPTED drive values -- harmless, because the
        next start adopts from the controller again and never writes them."""
        p = _config.save_sections(self.cfg, path or self.config_file())
        self._emit("info", f"config saved to {p}")
        return str(p)

    def apply_config(self) -> None:
        """Push the drive parameters after the config was edited in place.

        Only a value that DIFFERS from what the controller reports is written
        (2026-09-28). Settings OK and set_config both end here, and most of the
        time nothing in the drive changed (the theme, a leash, a step size):
        re-sending the same numbers is not harmless -- set_voltage clamps, so
        an adopted value outside the window would be "corrected" by a click on
        OK, which is exactly the silent change the adopt-on-start rule forbids.
        A controller that cannot be read is written, as before.
        """
        def differs(read, want) -> bool:
            try:
                return abs(float(read()) - float(want)) > 1e-9
            except Exception:
                return True

        b = self.backend
        for axis in range(3):
            if differs(lambda: b.read_step_rate(axis), axis_rate(self.cfg, axis)):
                self.set_step_rate(axis, axis_rate(self.cfg, axis))
            if differs(lambda: b.read_acceleration(axis), axis_acceleration(self.cfg, axis)):
                self.set_acceleration(axis, axis_acceleration(self.cfg, axis))
            if differs(lambda: b.read_voltage(axis), axis_voltage(self.cfg, axis)):
                self.set_voltage(axis, axis_voltage(self.cfg, axis))
        self._emit("info", "config applied")
