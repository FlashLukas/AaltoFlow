"""The brain: :class:`RotationMount` (set-and-forget family, one worker thread).

An ELL14 is set-and-forget: you command an angle and the mount's own
controller drives its piezo motor there against its encoder.  There is no
control loop for us to run.  What this brain DOES own:

  * the safety envelope -- every request is clamped to ``cfg.limits`` (and a
    warn event says so);
  * the user frame -- user angle = device angle - offset, wrapped to [0, 360);
  * ONE worker thread that is the only code touching the backend.  Setters
    never talk to the hardware: they validate, record the new target in brain
    attributes and put the backend call on a queue.  The worker executes the
    queue, polls every mount, and rebuilds the status snapshot.

Why a worker thread?  On the real bus a move is answered only when it has
FINISHED, and every read is a serial round trip.  If a command waited for that,
a 360 deg turn would block the service's command socket for seconds and the
client's 2 s timeout would fire.  So a command reply means "accepted", and the
caller watches ``status`` (the suite's fire-and-forget contract).

Why sequence numbers?  (gotcha #2, the stale-status trap.)  Right after a move
is queued, the mount has not started yet -- the backend still says "not
moving".  If ``moving`` were read from the backend alone, a scan would see the
new target, "not moving", and believe it had arrived.  So every motion command
gets a number (``move_id``); an axis counts as moving until the worker has
EXECUTED the latest numbered command AND the backend reports the motion over.

The angle SWEEP (fly scans, 2026-10-10; INSTRUMENT_MODULE_GUIDE.md 6b
"Ramps").  ``ramp_angle`` turns one mount to an angle AT A RATE: a HARDWARE
ramp -- the mount itself moves at a set speed ("move to at velocity").  The
rate (deg/s, scan-core's unit) becomes the ELL14's velocity percent
(hardware.max_speed_deg_s = 100 %); the user's own velocity is put back when
the sweep ends, however it ends.  The readback is the encoder angle the
worker polls, streamed (a fly scan bins by the MEASURED angle).  One sweep at
a time (one ramp_id): a new one, on any mount, ends the running one.  The
slowest speed is ~30 % of ~430 deg/s, so a fly row over an angle is fast.

Threads (gotcha #1): the snapshot is built only by the worker, from brain
attributes read under one lock; setters change those attributes, never the
snapshot.  ``status()`` hands out the last snapshot and never touches hardware.
"""

from __future__ import annotations

import collections
import math
import threading
import time
from dataclasses import asdict, dataclass, field

from .backends.base import ElliptecBackend, status_text
from .config import Config, axis_names, get_offsets, parse_addresses, set_offsets
from .stream import StreamRecorder


def wrap360(deg: float) -> float:
    """Any angle -> [0, 360)."""
    return float(deg) % 360.0


@dataclass
class MountStatus:
    """A snapshot of everything the GUI / a remote client needs.

    Lists have one entry per axis (per configured bus address).  ``None`` in a
    number list means "not known yet" (JSON has no NaN).
    """

    addresses: list = field(default_factory=list)
    names: list = field(default_factory=list)
    angle_deg: list = field(default_factory=list)     # USER angle, [0, 360)
    device_deg: list = field(default_factory=list)    # encoder angle, [0, 360)
    target_deg: list = field(default_factory=list)    # user target AS COMMANDED
    moving: list = field(default_factory=list)
    homed: list = field(default_factory=list)         # homed during this session
    velocity_pct: list = field(default_factory=list)
    offset_deg: list = field(default_factory=list)
    error_code: list = field(default_factory=list)    # Elliptec status code, 0 = OK
    error: list = field(default_factory=list)         # the same in words, "" if OK
    move_id: list = field(default_factory=list)       # number of the last motion command
    connected: bool = False
    n_axes: int = 0
    # The angle SWEEP (ramp_angle, fly scans).  ONE sweep at a time, so plain
    # values, not per-axis lists: ramp_id = the newest sweep started; "ramp_id
    # >= mine and not ramping" = my sweep is over.  ramp_axis = its axis
    # index (-1 = none yet); the rate is what the mount really runs at (the
    # asked rate rounded to a whole velocity percent).
    ramping: bool = False
    ramp_id: int = 0
    ramp_axis: int = -1
    ramp_target_deg: float | None = None
    ramp_rate_deg_per_s: float | None = None


class RotationMount:
    def __init__(self, backend: ElliptecBackend, cfg: Config):
        self.backend = backend
        self.cfg = cfg
        # The axis list is fixed for the life of the process: the backend
        # opened exactly these mounts.  A changed address list needs a restart.
        self.addresses = parse_addresses(cfg.axes.addresses)
        self.names = axis_names(cfg)
        self.n = len(self.addresses)

        self._lock = threading.RLock()
        self._queue: collections.deque = collections.deque()
        self._stop_evt = threading.Event()
        self._worker: threading.Thread | None = None
        self._connected = False
        self._infos: list[dict] = [{} for _ in range(self.n)]

        n = self.n
        # -- control state (written by setters, read by the worker) ------- #
        self._target = [None] * n            # user deg, as commanded
        self._velocity = [None] * n          # percent, READ from the mount at start
        self._cfg_velocity = int(cfg.motion.velocity_pct)
        self._offsets = get_offsets(cfg, n)
        self._seq = [0] * n                  # last motion command number
        self._exec_seq = [0] * n             # last one the worker has executed
        self._home_seq = [0] * n             # number of a home still in progress
        # -- measured state (written by the worker only) ------------------ #
        self._device = [None] * n            # device deg, wrapped
        self._hw_moving = [False] * n
        self._homed = [False] * n
        self._err = [0] * n
        self._err_text = [""] * n
        # -- the angle SWEEP (under _lock) -------------------------------- #
        self._ramp_id = 0
        self._ramping = False
        self._ramp_axis = -1
        self._ramp_seq = 0                   # move_id of the sweep's move
        self._ramp_target = None
        self._ramp_rate = None
        # -- THE STREAM (fly scans): every polled angle, all axes per row --- #
        self.recorder = StreamRecorder([f"angle_{a.lower()}" for a in self.addresses])
        self._rec_prev = [None] * n          # last recorded value (unwrapping)
        self._snapshot = MountStatus()

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

    def _label(self, axis: int) -> str:
        return f"{self.names[axis]} (addr {self.addresses[axis]})"

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Open the bus, READ every mount once, adopt what it is doing, start
        the worker.

        Adopt, never impose (Lukas, 2026-09-27: "all modules should read the
        instrument state on startup, not to change anything"): the angle and
        the SPEED each mount already has become the brain's state.  Nothing is
        written to a mount here -- no speed push, no homing, no move.  The
        config's ``motion.velocity_pct`` is only a default the user can apply
        on purpose (Settings / set_config, or the per-axis velocity control).
        """
        self.backend.open(list(self.addresses))
        self._connected = True
        for i, a in enumerate(self.addresses):
            try:
                self._infos[i] = self.backend.device_info(a)
            except Exception:
                self._infos[i] = {"address": a}
            try:
                v = self.backend.read_velocity(a)      # a READ: what the mount runs at
            except Exception:
                v = None
            self._velocity[i] = None if v is None else int(v)
            if v is None:
                self._emit("warn", f"{self._label(i)}: speed could not be read; "
                                   "set it to see it here")
            elif self._clamp_velocity(v, quiet=True) != int(v):
                # Shown as it is, not "corrected": correcting it would be a write.
                self._emit("warn", f"{self._label(i)} runs at {int(v)} %, outside the "
                                   "configured velocity window (left unchanged)")
        # Remember the config speed as it was at start, so a later set_config
        # that does not CHANGE it (e.g. only an offset edited) does not push it
        # over the speed the mounts were found at (see apply_config).
        self._cfg_velocity = int(self.cfg.motion.velocity_pct)
        self._poll_all()
        with self._lock:
            # The mount does not move at start: its target is where it is.
            for i in range(self.n):
                if self._device[i] is not None:
                    self._target[i] = self._user(i, self._device[i])
            self._rebuild_snapshot()
        self._stop_evt.clear()
        self._worker = threading.Thread(target=self._run, name="elliptec-worker", daemon=True)
        self._worker.start()
        if self.cfg.motion.home_on_start:
            # Explicit opt-in only (default False): homing TURNS the optic, so
            # under the adopt-on-start rule it never happens unless asked for.
            self._emit("warn", "motion.home_on_start is set: homing every mount")
            self.home_all()
        self._emit("info", f"elliptec started: {self.backend.idn()}")

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Stop every mount, stop the worker, close the bus.  Idempotent.

        ``keep_outputs`` (shutdown{keep_outputs: true}, a restart for a code
        update) changes nothing here: stopping motion is a SAFETY step that a
        restart keeps (no move may run on unsupervised), and nothing is moved
        back, homed or parked either way -- the next start adopts the position.

        A rotation mount holds its angle unpowered, so "safe" here just means
        "not turning": each axis gets a stop before the port is closed.
        """
        if not self._connected:
            return
        self._stop_evt.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        try:
            for a in self.addresses:
                try:
                    self.backend.stop(a)
                except Exception:
                    pass
            # A sweep runs the mount at ITS speed: put the user's back (the
            # worker is gone, so the backend is ours to call here).
            with self._lock:
                i, was = self._ramp_axis, self._ramping
                self._ramping = False
            if was and 0 <= i < self.n:
                try:
                    self._set_tracking(self.addresses[i], False)
                    if self._velocity[i] is not None:
                        self.backend.set_velocity(self.addresses[i], int(self._velocity[i]))
                except Exception:
                    pass
        finally:
            try:
                self.backend.close()
            finally:
                self._connected = False
                with self._lock:
                    self._rebuild_snapshot()
        self._emit("info", "elliptec shut down")

    # ------------------------------------------------------------------ #
    # the worker
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        while not self._stop_evt.is_set():
            t0 = time.monotonic()
            try:
                self._execute_queue()
                self._poll_all()
                with self._lock:
                    self._rebuild_snapshot()
            except Exception as exc:        # the loop must never die
                self._emit("error", f"worker: {type(exc).__name__}: {exc}")
            period = 1.0 / max(1.0, float(self.cfg.hardware.poll_hz))
            # time.sleep, not Event.wait(period): on Windows a timed wait rounds
            # up to the 15.6 ms system tick (gotcha #34).
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def _execute_queue(self) -> None:
        """Run the queued backend calls, oldest first.

        A MOTION command for a mount that is still turning is held back (kept
        in the queue, in order) until that mount has finished.  Why: the
        Elliptec answers a move only when it ENDS, and a second move sent into
        a running one is either refused with "busy" (the new target would be
        silently lost while the status shows it) or ends the first one early
        with a completion reply that looks like the end of the second.  Either
        way a scan could read "arrived" at the wrong angle.  Holding it back
        costs nothing: the scan waits for the move anyway.  A stop is never
        held back (it has no move number), and it drops the held moves.
        """
        held = []                 # motion commands deferred this pass
        busy = set()              # axes that are turning, or started a move now
        with self._lock:
            busy.update(i for i in range(self.n) if self._hw_moving[i])
        while True:
            with self._lock:
                if not self._queue:
                    break
                item = self._queue.popleft()
            axis, seq, what, fn = item
            if seq and axis in busy:
                held.append(item)
                continue
            try:
                fn()
            except Exception as exc:
                with self._lock:
                    self._err_text[axis] = f"{what} failed: {exc}"
                self._emit("error", f"{self._label(axis)}: {what} failed: {exc}")
            finally:
                if seq:
                    busy.add(axis)
                    with self._lock:
                        self._exec_seq[axis] = max(self._exec_seq[axis], seq)
        if held:
            with self._lock:
                # Put them back IN FRONT of anything queued meanwhile, keeping
                # their order -- unless a stop dropped them in the meantime
                # (stop() counts dropped moves as executed).
                keep = [q for q in held if q[1] > self._exec_seq[q[0]]]
                self._queue.extendleft(reversed(keep))

    def _poll_all(self) -> None:
        stamps = [None] * self.n
        for i, a in enumerate(self.addresses):
            try:
                tw0 = time.time()
                r = self.backend.poll(a)
                # the reading's time: the middle of the call (wall clock, so a
                # coordinator on another PC can line it up)
                stamps[i] = 0.5 * (tw0 + time.time())
            except Exception as exc:
                with self._lock:
                    if not self._err_text[i].startswith("poll"):
                        self._emit("error", f"{self._label(i)}: poll failed: {exc}")
                    self._err_text[i] = f"poll failed: {exc}"
                continue
            with self._lock:
                dev = r.device_deg
                self._device[i] = None if dev is None or math.isnan(dev) else wrap360(dev)
                self._hw_moving[i] = bool(r.moving)
                code = int(r.error_code)
                if code not in (0, 9) and code != self._err[i]:
                    self._emit("warn", f"{self._label(i)}: mount reports {status_text(code)}")
                self._err[i] = code if code != 9 else 0
                self._err_text[i] = "" if self._err[i] == 0 else status_text(self._err[i])
                # A home is complete when it was executed, the motion is over
                # and the mount reported no error.
                if (self._home_seq[i] and self._exec_seq[i] >= self._home_seq[i]
                        and not self._hw_moving[i]):
                    self._homed[i] = self._err[i] == 0
                    self._home_seq[i] = 0
                    if self._homed[i]:
                        self._emit("info", f"{self._label(i)} homed")
                # The SWEEP has ended when its move was executed and the
                # mount has stopped turning: put the user's speed back.
                if (self._ramping and i == self._ramp_axis
                        and self._exec_seq[i] >= self._ramp_seq and not self._hw_moving[i]):
                    self._end_sweep_locked(halt=False)
                    self._emit("info", f"{self._label(i)}: sweep done at "
                                       f"{self._user(i, self._device[i]):.4g} deg"
                               if self._device[i] is not None else
                               f"{self._label(i)}: sweep done")
        self._record(stamps)

    def _record(self, stamps) -> None:
        """One row of the stream: every axis' USER angle, stamped at the
        middle of the swept axis' poll during a sweep (the angle the fly row
        is binned by), else at the middle of the whole pass.  The angle is
        UNWRAPPED (370, not 10, after 359) relative to the previous row, and
        the first row relative to the commanded target, so a sweep that ends
        at 360 deg does not jump back to 0 in its last pixel."""
        if not self.recorder.running:
            return
        with self._lock:
            i = self._ramp_axis if self._ramping else -1
            if 0 <= i < self.n and stamps[i] is not None:
                t = stamps[i]
            else:
                got = [s for s in stamps if s is not None]
                if not got:
                    return
                t = 0.5 * (min(got) + max(got))
            row = []
            for k in range(self.n):
                dev = self._device[k]
                if dev is None:
                    row.append(float("nan"))
                    continue
                a = self._user(k, dev)
                ref = self._rec_prev[k]
                if ref is None:
                    # first row: plain [0, 360) -- unless the mount sits AT its
                    # target (within 1 deg round the circle), e.g. parked at a
                    # fly row's start of 360 while the encoder says 0.001
                    tgt = self._target[k]
                    near = (tgt is not None
                            and abs(((a - tgt + 180.0) % 360.0) - 180.0) < 1.0)
                    ref = tgt if near else a
                a = min((a - 360.0, a, a + 360.0), key=lambda v: abs(v - ref))
                self._rec_prev[k] = a
                row.append(a)
        self.recorder.append(t, row)

    def _moving(self, i: int) -> bool:
        return self._hw_moving[i] or self._exec_seq[i] != self._seq[i]

    def _rebuild_snapshot(self) -> None:
        """Build a NEW status object (called with the lock held)."""
        n = self.n
        self._snapshot = MountStatus(
            addresses=list(self.addresses),
            names=list(self.names),
            angle_deg=[None if self._device[i] is None else self._user(i, self._device[i])
                       for i in range(n)],
            device_deg=list(self._device),
            target_deg=list(self._target),
            moving=[self._moving(i) for i in range(n)],
            homed=list(self._homed),
            velocity_pct=list(self._velocity),
            offset_deg=list(self._offsets),
            error_code=list(self._err),
            error=list(self._err_text),
            move_id=list(self._seq),
            connected=self._connected,
            n_axes=n,
            ramping=self._ramping,
            ramp_id=self._ramp_id,
            ramp_axis=self._ramp_axis,
            ramp_target_deg=self._ramp_target,
            ramp_rate_deg_per_s=self._ramp_rate,
        )

    def status(self) -> MountStatus:
        """The last snapshot.  NEVER raises, never touches the hardware."""
        with self._lock:
            return self._snapshot

    def status_dict(self) -> dict:
        return asdict(self.status())

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _check_axis(self, axis) -> int:
        a = int(axis)
        if not 0 <= a < self.n:
            raise ValueError(f"no axis {axis} (this service drives {self.n})")
        return a

    def _user(self, i: int, device_deg: float) -> float:
        return wrap360(device_deg - self._offsets[i])

    def _enqueue(self, axis: int, what: str, fn, motion: bool = True) -> int:
        """Queue a backend call; motion commands get a new move_id."""
        with self._lock:
            seq = 0
            if motion:
                self._seq[axis] += 1
                seq = self._seq[axis]
            self._queue.append((axis, seq, what, fn))
            self._rebuild_snapshot()     # "moving" and the target show at once
            return seq

    def _full_circle(self) -> bool:
        lim = self.cfg.limits
        return (not lim.enforce) or (lim.min_angle_deg <= 0.0 and lim.max_angle_deg >= 360.0)

    def _clamp_angle(self, axis: int, deg: float) -> float:
        """Normalise a user angle and clamp it to the angle window.

        Anything outside [0, 360] is first wrapped (370 -> 10): it is the same
        orientation of the optic.  Exactly 360 is kept as 360, so a scan that
        sweeps 0..360 sees its own last setpoint echoed back.
        """
        if not math.isfinite(deg):
            raise ValueError(f"angle {deg!r} is not a number")
        if not 0.0 <= deg <= 360.0:
            wrapped = wrap360(deg)
            self._emit("info", f"{self._label(axis)}: {deg:.4g} deg taken as {wrapped:.4g} deg")
            deg = wrapped
        lim = self.cfg.limits
        if lim.enforce:
            if deg < lim.min_angle_deg:
                self._emit("warn", f"{self._label(axis)}: {deg:.4g} deg clamped to {lim.min_angle_deg:.4g} deg")
                return float(lim.min_angle_deg)
            if deg > lim.max_angle_deg:
                self._emit("warn", f"{self._label(axis)}: {deg:.4g} deg clamped to {lim.max_angle_deg:.4g} deg")
                return float(lim.max_angle_deg)
        return deg

    def _clamp_velocity(self, pct, quiet: bool = False) -> int:
        v = int(round(float(pct)))
        lim = self.cfg.limits
        lo, hi = int(lim.min_velocity_pct), int(lim.max_velocity_pct)
        hi = min(hi, 100)   # the protocol's unit is percent of the maximum
        if not lim.enforce:
            lo, hi = 1, 100
        c = min(hi, max(lo, v))
        if c != v and not quiet:
            self._emit("warn", f"velocity {v} % clamped to {c} %")
        return c

    # ------------------------------------------------------------------ #
    # motion verbs (fire-and-forget: they return once QUEUED)
    # ------------------------------------------------------------------ #
    def move_abs(self, axis, angle_deg) -> dict:
        """Turn one mount to a USER angle (degrees).  Returns the adopted
        target and the move_id."""
        i = self._check_axis(axis)
        target = self._clamp_angle(i, float(angle_deg))
        self._take_over(i)
        device = wrap360(target + self._offsets[i])
        addr = self.addresses[i]
        if self._full_circle():
            fn = lambda: self.backend.start_move_abs(addr, device)
        else:
            # Inside an angle window the move is sent as a RELATIVE step,
            # worked out when the worker sends it (the mount is not turning
            # then, so its angle is final).  Why not "ma": an absolute move
            # goes the direct way in the DEVICE frame, [0, 360) from the home
            # mark.  With an offset the user window can straddle the home mark
            # (window 10..200 deg, offset 300 deg = device 310..140 via 0), and
            # "ma" from device 320 to device 100 would turn backwards through
            # device 200 -- straight out of the window the cable needs.  A
            # step along the user frame stays inside it by construction.
            fn = lambda: self._start_window_move(i, addr, target, device)
        with self._lock:
            self._target[i] = target
            seq = self._enqueue(i, "move", fn)
        self._emit("info", f"{self._label(i)} -> {target:.4g} deg")
        return {"target": target, "move_id": seq}

    def _start_window_move(self, i: int, addr: str, target: float, device: float) -> None:
        """Worker thread: turn axis ``i`` to user angle ``target`` without
        leaving the angle window (see move_abs)."""
        with self._lock:
            dev = self._device[i]
            here = None if dev is None else self._user(i, dev)
        if here is None:
            self.backend.start_move_abs(addr, device)   # angle unknown: best effort
            return
        # The user angle is wrapped to [0, 360); a mount parked on the window's
        # 0-deg end can read 359.999 from encoder rounding.  Take whichever of
        # here / here - 360 lies nearer the window, so the step is the short
        # one inside it and not a full turn the wrong way.
        lim = self.cfg.limits

        def dist(a):
            return max(0.0, lim.min_angle_deg - a, a - lim.max_angle_deg)
        if dist(here - 360.0) < dist(here):
            here -= 360.0
        delta = target - here
        if abs(delta) > 1e-9:
            self.backend.start_move_rel(addr, delta)

    def move_rel(self, axis, delta_deg) -> dict:
        """Turn one mount BY an angle (degrees, + = increasing angle).

        With the full circle allowed this is a true relative move ("mr"), so
        the mount keeps turning the same way -- across 0 deg if need be.  With
        an angle window set it becomes a move_abs to the clamped end point
        instead, which stays inside the window (see move_abs).
        """
        i = self._check_axis(axis)
        d = float(delta_deg)
        if not math.isfinite(d):
            raise ValueError(f"step {delta_deg!r} is not a number")
        self._take_over(i)
        lim = self.cfg.limits
        if lim.enforce and abs(d) > lim.max_relative_deg:
            c = math.copysign(lim.max_relative_deg, d)
            self._emit("warn", f"{self._label(i)}: step {d:.4g} deg clamped to {c:.4g} deg")
            d = c
        with self._lock:
            # Step from the last TARGET when the mount is there (or still on its
            # way), so ten 1-deg steps make exactly 10 deg and do not pick up
            # the encoder's rounding each time.  After a stop mid-move the
            # target is stale, so then step from the measured angle.
            tgt, dev = self._target[i], self._device[i]
            here = None if dev is None else self._user(i, dev)
            if tgt is not None and (self._moving(i) or here is None
                                    or abs(((tgt - here + 180.0) % 360.0) - 180.0) < 0.05):
                base = tgt
            else:
                base = here if here is not None else 0.0
        if not self._full_circle():
            # Inside a window there is no wrapping: -300 deg from 100 deg means
            # "as far as the window allows", i.e. its lower end.
            lim = self.cfg.limits
            raw = base + d
            end = min(max(raw, lim.min_angle_deg), lim.max_angle_deg)
            if end != raw:
                self._emit("warn", f"{self._label(i)}: {raw:.4g} deg clamped to {end:.4g} deg")
            return self.move_abs(i, end)
        target = wrap360(base + d)
        addr = self.addresses[i]
        with self._lock:
            self._target[i] = target
            seq = self._enqueue(i, "relative move", lambda: self.backend.start_move_rel(addr, d))
        self._emit("info", f"{self._label(i)} by {d:+.4g} deg -> {target:.4g} deg")
        return {"target": target, "move_id": seq}

    def home(self, axis, direction: str | None = None) -> dict:
        """Find the home mark (device 0 deg).  direction "cw" | "ccw"."""
        i = self._check_axis(axis)
        direction = (direction or self.cfg.motion.home_direction or "cw").lower()
        if direction not in ("cw", "ccw"):
            raise ValueError(f"home direction {direction!r} (use cw or ccw)")
        ccw = direction == "ccw"
        addr = self.addresses[i]
        self._take_over(i)
        with self._lock:
            self._homed[i] = False
            self._target[i] = self._user(i, 0.0)
            seq = self._enqueue(i, "home", lambda: self.backend.start_home(addr, ccw))
            self._home_seq[i] = seq
        self._emit("info", f"{self._label(i)} homing ({direction})")
        return {"target": self._target[i], "move_id": seq}

    def home_all(self, direction: str | None = None) -> list:
        return [self.home(i, direction) for i in range(self.n)]

    def stop(self, axis) -> None:
        """Halt one mount NOW: queued moves of that axis are dropped first, so
        a stop is never stuck behind the very motion it is meant to end."""
        i = self._check_axis(axis)
        addr = self.addresses[i]
        with self._lock:
            # Only MOTION commands (seq > 0) are dropped; a queued speed change
            # still goes through.
            self._queue = collections.deque(q for q in self._queue
                                            if not (q[0] == i and q[1]))
            # Count every move numbered so far as done -- the dropped ones, and
            # any the worker is holding back right now (it discards those when
            # it sees this) -- or the axis would report "moving" forever.
            self._exec_seq[i] = self._seq[i]
            self._home_seq[i] = 0
            self._queue.appendleft((i, 0, "stop", lambda: self.backend.stop(addr)))
            # a stop ends a sweep of this axis too (and puts the speed back)
            if self._ramping and self._ramp_axis == i:
                self._end_sweep_locked(halt=False)
        self._emit("warn", f"STOP {self._label(i)}")

    def stop_all(self) -> None:
        for i in range(self.n):
            self.stop(i)

    # ------------------------------------------------------------------ #
    # parameters and frame
    # ------------------------------------------------------------------ #
    def set_velocity(self, axis, percent) -> int:
        i = self._check_axis(axis)
        v = self._clamp_velocity(percent)
        addr = self.addresses[i]
        with self._lock:
            self._velocity[i] = v
            if self._ramping and self._ramp_axis == i:
                # the mount runs at the SWEEP's speed now; this one is the
                # user's, and it is what the end of the sweep puts back
                self._rebuild_snapshot()
                sweeping = True
            else:
                self._enqueue(i, "set velocity", lambda: self.backend.set_velocity(addr, v),
                              motion=False)
                sweeping = False
        self._emit("info", f"{self._label(i)} velocity = {v} %"
                   + (" (applies when the sweep ends)" if sweeping else ""))
        return v

    # ------------------------------------------------------------------ #
    # the angle SWEEP (fly scans)
    # ------------------------------------------------------------------ #
    def ramp_rate_limits(self) -> tuple[float, float]:
        """(min, max) sweep rate in deg/s: the velocity window times the
        mount's top speed (hardware.max_speed_deg_s)."""
        lim = self.cfg.limits
        top = max(1e-6, float(self.cfg.hardware.max_speed_deg_s))
        if lim.enforce:
            lo, hi = int(lim.min_velocity_pct), min(100, int(lim.max_velocity_pct))
        else:
            lo, hi = 1, 100
        lo = max(1, min(lo, hi))
        return lo / 100.0 * top, hi / 100.0 * top

    def ramp_angle(self, axis, angle_deg, rate_deg_per_s) -> dict:
        """Turn one mount from where it is to a USER angle at a RATE (deg/s):
        the mount's own "move to at velocity" (a HARDWARE ramp).

        The rate becomes the ELL14's velocity percent (rounded to a whole
        percent and clamped to the velocity window -- warned when the asked
        rate was outside it); the user's velocity is restored when the sweep
        ends.  The sweep turns along the USER frame (a relative step worked
        out when the worker sends it), so it never takes the short way round
        the wrong direction: 300 -> 360 turns up through 330, not down.
        Returns {"ramp_id", "target", "rate"}."""
        i = self._check_axis(axis)
        rate = float(rate_deg_per_s)
        if not math.isfinite(rate) or abs(rate) <= 0:
            raise ValueError(f"rate {rate_deg_per_s!r} must be a number > 0")
        rate = abs(rate)
        target = self._clamp_angle(i, float(angle_deg))
        top = max(1e-6, float(self.cfg.hardware.max_speed_deg_s))
        r_lo, r_hi = self.ramp_rate_limits()
        if not r_lo - 1e-9 <= rate <= r_hi + 1e-9:
            self._emit("warn", f"{self._label(i)}: sweep rate {rate:.4g} deg/s clamped to "
                               f"{min(max(rate, r_lo), r_hi):.4g} deg/s (the ELL14 "
                               f"runs at {r_lo:.4g} .. {r_hi:.4g} deg/s)")
        pct = self._clamp_velocity(rate / top * 100.0, quiet=True)
        real = pct / 100.0 * top
        addr = self.addresses[i]
        # a running sweep (of any mount) ends first: one at a time
        with self._lock:
            if self._ramping:
                self._end_sweep_locked(halt=True)

        def prepare():
            # on the worker thread, BEFORE the move: follow the encoder
            # during the move, and run at the sweep's speed
            self._set_tracking(addr, True)
            self.backend.set_velocity(addr, pct)

        with self._lock:
            self._enqueue(i, "sweep speed", prepare, motion=False)
            self._target[i] = target
            seq = self._enqueue(i, "sweep",
                                lambda: self._start_sweep_move(i, addr, target))
            self._ramp_id += 1
            rid = self._ramp_id
            self._ramping = True
            self._ramp_axis = i
            self._ramp_seq = seq
            self._ramp_target = target
            self._ramp_rate = real
            self._rebuild_snapshot()
        self._emit("info", f"{self._label(i)}: sweep -> {target:.4g} deg at {real:.4g} deg/s "
                           f"({pct} %)")
        return {"ramp_id": rid, "target": target, "rate": real}

    def ramp_stop(self) -> bool:
        """End a sweep WHERE IT IS (the mount stops, the user's speed comes
        back).  True if one was running.  A SAFETY verb over the wire."""
        with self._lock:
            if not self._ramping:
                return False
            i = self._ramp_axis
            self._end_sweep_locked(halt=True)
        self._emit("info", f"{self._label(i)}: sweep stopped")
        return True

    def _take_over(self, i: int) -> None:
        """An ordinary command for axis i ends a sweep of axis i first: the
        mount is halted (a move queued behind a running sweep would otherwise
        wait for it to finish) and the user's speed is put back."""
        with self._lock:
            if not (self._ramping and self._ramp_axis == i):
                return
            self._end_sweep_locked(halt=True)
        self._emit("info", f"{self._label(i)}: sweep stopped by a new command")

    def _end_sweep_locked(self, halt: bool) -> None:
        """Caller holds _lock.  End the sweep: optionally halt its mount (drop
        its queued motion, stop it first thing), then queue the user's speed
        and the end of encoder tracking."""
        i = self._ramp_axis
        self._ramping = False
        if not 0 <= i < self.n:
            return
        addr = self.addresses[i]
        if halt:
            self._queue = collections.deque(q for q in self._queue
                                            if not (q[0] == i and q[1]))
            self._exec_seq[i] = self._seq[i]
            self._queue.appendleft((i, 0, "stop", lambda: self.backend.stop(addr)))
        v = self._velocity[i]

        def restore():
            self._set_tracking(addr, False)
            if v is not None:
                self.backend.set_velocity(addr, int(v))
        self._queue.append((i, 0, "restore speed", restore))
        self._rebuild_snapshot()

    def _start_sweep_move(self, i: int, addr: str, target: float) -> None:
        """Worker thread: the sweep's move, as a RELATIVE step along the user
        frame from where the mount is NOW to `target` (see ramp_angle)."""
        with self._lock:
            dev = self._device[i]
            here = None if dev is None else self._user(i, dev)
        if here is None:
            self.backend.start_move_abs(addr, wrap360(target + self._offsets[i]))
            return
        # a mount parked at 0 can read 359.999: take the nearer of here and
        # here - 360 (the target is in [0, 360])
        if abs((here - 360.0) - target) < abs(here - target):
            here -= 360.0
        delta = target - here
        if abs(delta) > 1e-9:
            self.backend.start_move_rel(addr, delta)

    def _set_tracking(self, addr: str, on: bool) -> None:
        fn = getattr(self.backend, "set_tracking", None)
        if fn is not None:
            fn(addr, on)

    # the stream verbs: every polled angle (group "angle")
    def stream_start(self) -> int:
        with self._lock:
            self._rec_prev = [None] * self.n
        return self.recorder.start()

    def stream_read(self) -> dict:
        return self.recorder.read()

    def stream_stop(self) -> dict:
        return self.recorder.stop()

    def set_offset(self, axis, offset_deg) -> float:
        """Set the user zero: user angle = device angle - offset."""
        i = self._check_axis(axis)
        off = float(offset_deg)
        if not math.isfinite(off):
            raise ValueError("offset is not a number")
        off = wrap360(off)
        with self._lock:
            old = self._offsets[i]
            self._offsets[i] = off
            if self._target[i] is not None:  # same physical target, new frame
                self._target[i] = wrap360(self._target[i] + old - off)
            set_offsets(self.cfg, self._offsets)
            self._rebuild_snapshot()
        self._emit("info", f"{self._label(i)} offset = {off:.4g} deg")
        return off

    def set_zero(self, axis) -> float:
        """Call the CURRENT angle of ``axis`` 0 deg from now on."""
        i = self._check_axis(axis)
        with self._lock:
            dev = self._device[i]
        if dev is None:
            raise RuntimeError(f"{self._label(i)}: angle not known yet")
        return self.set_offset(i, dev)

    def clear_zero(self, axis) -> float:
        return self.set_offset(axis, 0.0)

    # ------------------------------------------------------------------ #
    # config / info
    # ------------------------------------------------------------------ #
    def info(self) -> dict:
        return {
            "idn": self.backend.idn(),
            "addresses": list(self.addresses),
            "names": list(self.names),
            "devices": [dict(d) for d in self._infos],
        }

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-apply the config after it was edited in place (Settings dialog or
        a remote set_config): offsets and speed take effect now; a changed
        address list needs a restart, because the bus was opened with the old."""
        try:
            new_addrs = parse_addresses(self.cfg.axes.addresses)
        except ValueError as exc:
            new_addrs = None
            self._emit("error", f"axes.addresses: {exc}")
        if new_addrs is not None and new_addrs != self.addresses:
            self._emit("warn", "the address list changed: restart the service to use "
                               + ",".join(new_addrs))
        with self._lock:
            new_off = get_offsets(self.cfg, self.n)
            for i in range(self.n):
                if self._target[i] is not None:
                    self._target[i] = wrap360(self._target[i] + self._offsets[i] - new_off[i])
            self._offsets = new_off
        # Push the config speed only when it was CHANGED since the last apply
        # (or since start).  A set_config that edits, say, an offset carries the
        # unchanged default speed too, and must not overwrite the speed the
        # mounts were adopted at (adopt-on-start rule, 2026-09-27).
        v_cfg = int(self.cfg.motion.velocity_pct)
        if v_cfg != self._cfg_velocity:
            self._cfg_velocity = v_cfg
            for i in range(self.n):
                self.set_velocity(i, v_cfg)
        self._emit("info", "config applied")
