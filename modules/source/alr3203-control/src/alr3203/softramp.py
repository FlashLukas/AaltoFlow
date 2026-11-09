"""softramp.py -- walk a setter from where it is to a target at a set PACE.

Why this exists (Lukas, 2026-10-09: "when it makes sense I want modules to be
able to continuously sweep at a certain pace between the values"). A fly scan
(scan-core, `type: fly` axis) records the detectors while a knob moves
CONTINUOUSLY, then sorts every sample into the pixel of the value the knob
had at that moment. A stage moves continuously by itself. Most knobs do not:
an RF generator jumps to the frequency it is told and stays there. So the
SERVICE walks the setpoint in small steps -- "a software ramp" -- and this
file is that walk, written once for every module that needs one.

What it does:

  * a thread calls `setter(value)` every `dt_s` seconds (10-50 ms typically),
    the value moving in a straight line from the start to `to` at `rate`
    (units per second). The value is computed from the ELAPSED TIME, not by
    adding one step per tick: a tick that comes late (a busy serial line, a
    Windows timer) then simply sends a value further along, and the pace
    stays exactly `rate`. The ticks are scheduled on DEADLINES with
    time.sleep (a timed Event.wait sleeps at least one 15.6 ms Windows tick,
    docs/DEVELOPER_NOTES.md gotcha #34, and a "50 Hz" loop runs at 32);
  * every value is clamped to `limits` before it is sent, and the target too;
  * it can be stopped at any time -- the module's stop verb, its shutdown, or
    a new set_* that takes the knob over all call stop();
  * it RECORDS every value it sent with the time the setter returned (the
    moment the instrument had it) into a bounded ring buffer, in the suite's
    stream format, so the module's stream verbs (stream_start / _read /
    _stop) can serve it as is. A fly scan bins by these COMMANDED values
    when the instrument cannot report the real one while sweeping
    (readback "measured": false in describe). If reading the instrument
    back is cheap, pass `readback=` and it is recorded next to the command.

Threads (gotcha #1 in docs/DEVELOPER_NOTES.md). The setter runs on THIS
object's thread, so it must take the module's own hardware lock itself, as
every other backend call does. Two rules keep it deadlock-free:

  * call stop() BEFORE taking the lock the setter needs: stop() waits for the
    walk to finish its current step, and that step may be waiting for the
    lock (a module's set_frequency: stop the ramp first, then lock and set);
  * the setter must not call start() (it may raise; the ramp then ends and
    `error` says why).

Status: `status()` returns plain values for the module to copy into its
status snapshot (`ramping`, `ramp_id`, ...). The ramp id is numbered like an
acquisition (gotcha #17): start() returns it, the service puts it in the
reply, and a caller waits until the status shows THAT id finished -- a
"ramping: false" from before the start can then never pass for the end.

The suite's convention: this file is COPIED, byte for byte, into each module
that uses it (like hwlock.py), so the modules stay independent packages;
tools/check_modules.py compares the copies with this master. Standard
library only.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque

#: Default time between two steps (s). 20 ms = 50 steps a second: smooth for a
#: scan pixel of a few hundred ms, and gentle on a serial line.
DEFAULT_DT_S = 0.02


def _finite(name: str, value) -> float:
    """A finite float, or ValueError. A NaN rate or target would make every
    comparison False and the walk would never end (or run to a limit)."""
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return v


def _num(v):
    """JSON-safe: NaN / inf travel as null (Python would write the token NaN)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class SoftRamp:
    """A software ramp of ONE knob.

    setter    -- setter(value): send one value to the instrument (blocking is
                 fine: the next value then simply lies further along)
    getter    -- getter() -> the present value; where a ramp starts when
                 start() is not told (None: start() needs `start=`)
    limits    -- (lo, hi) or a function returning it (live limits, read at
                 every start and step); None = unlimited
    dt_s      -- time between two steps (default 20 ms)
    readback  -- optional readback() -> the instrument's ACTUAL value, recorded
                 next to the command on every step. Only if it is CHEAP: it
                 runs inside the step and slows the walk down.
    on_done   -- optional on_done(ramp_id, reason) when a walk ends: "done"
                 (reached the target), "stopped", or "error: ..."
    channel   -- the name of the commanded value in the stream (default
                 "commanded"); the readback is "<channel>_readback"
    """

    def __init__(self, setter, getter=None, *, limits=None, dt_s: float = DEFAULT_DT_S,
                 readback=None, on_done=None, channel: str = "commanded",
                 max_samples: int = 200_000, name: str = "softramp"):
        self._set = setter
        self._get = getter
        self._limits = limits
        self.dt_s = max(0.001, float(dt_s))
        self._readback = readback
        self._on_done = on_done or (lambda ramp_id, reason: None)
        self.channel = str(channel)
        self.name = name
        self._lock = threading.Lock()         # guards everything below
        self._thread: threading.Thread | None = None
        self._halt = threading.Event()        # tells the current walk to end
        self._ramp_id = 0                     # numbered: see the module doc
        self._running = False
        self._target = float("nan")
        self._rate = float("nan")
        self._value = float("nan")            # the last value SENT
        self._error = ""
        self._last_reason = ""
        # the record (stream format); only filled while a stream is started
        self._buf: deque = deque(maxlen=int(max_samples))
        self._recording = False
        self._overflow = False
        self._stream_id = 0

    # ---- the walk -------------------------------------------------------------

    def limits(self) -> tuple[float, float]:
        lim = self._limits() if callable(self._limits) else self._limits
        if lim is None:
            return float("-inf"), float("inf")
        lo, hi = float(lim[0]), float(lim[1])
        return (lo, hi) if lo <= hi else (hi, lo)

    def _clamp(self, v: float) -> float:
        lo, hi = self.limits()
        return max(lo, min(hi, v))

    def start(self, to: float, rate: float, start: float | None = None) -> int:
        """Begin a walk to `to` at `rate` (units/s, > 0). Returns the ramp id.

        A walk already running is stopped first (the new one takes over from
        wherever that one got to). `start` = where to begin; default the
        getter's value, else the last value sent. Raises ValueError for a
        non-finite or non-positive rate, or no known start.
        """
        to = _finite("to", to)
        rate = abs(_finite("rate", rate))
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.stop()
        if start is None:
            if self._get is not None:
                start = self._get()
            else:
                start = self._value
        start = _finite("start", start)
        to = self._clamp(to)
        start = self._clamp(start)
        with self._lock:
            self._ramp_id += 1
            rid = self._ramp_id
            self._running = True
            self._target, self._rate = to, rate
            self._error = ""
            self._last_reason = ""
            self._halt = threading.Event()
            halt = self._halt
        t = threading.Thread(target=self._walk, args=(rid, start, to, rate, halt),
                             name=f"{self.name}-{rid}", daemon=True)
        with self._lock:
            self._thread = t
        t.start()
        return rid

    def stop(self, timeout_s: float = 5.0) -> bool:
        """End the walk where it is. True if one was running.

        Waits (up to timeout_s) for the step in progress to finish, so that
        when stop() returns no further value will be sent -- except when
        called from the walk's own thread (from inside the setter), where
        waiting for ourselves would hang."""
        with self._lock:
            t = self._thread
            was = self._running
            self._halt.set()
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout_s)
        return was

    def wait(self, timeout_s: float | None = None) -> bool:
        """Block until the walk has ended. True if it has."""
        with self._lock:
            t = self._thread
        if t is None:
            return True
        t.join(timeout=timeout_s)
        return not t.is_alive()

    def _walk(self, rid: int, start: float, to: float, rate: float, halt) -> None:
        span = to - start
        dur = abs(span) / rate
        sign = 1.0 if span >= 0 else -1.0
        t0 = time.monotonic()
        next_t = t0
        reason = "done"
        try:
            while True:
                if halt.is_set():
                    reason = "stopped"
                    break
                el = time.monotonic() - t0
                last = el >= dur
                v = to if last else start + sign * rate * el
                self._send(self._clamp(v))
                if last:
                    break
                next_t += self.dt_s
                delay = next_t - time.monotonic()
                if delay > 0:
                    # sleep in short slices so stop() is answered quickly even
                    # with a long dt; time.sleep, not Event.wait (gotcha #34)
                    end = time.monotonic() + delay
                    while not halt.is_set():
                        left = end - time.monotonic()
                        if left <= 0:
                            break
                        time.sleep(min(left, 0.01))
                else:
                    next_t = time.monotonic()        # fell behind: no burst
        except Exception as exc:                     # noqa: BLE001
            reason = f"error: {type(exc).__name__}: {exc}"
        with self._lock:
            if self._ramp_id == rid:
                self._running = False
                self._last_reason = reason
                if reason.startswith("error"):
                    self._error = reason[len("error: "):]
        try:
            self._on_done(rid, reason)
        except Exception:                            # noqa: BLE001
            pass

    def _send(self, v: float) -> None:
        self._set(v)
        t = time.time()      # wall clock: the coordinator may sit on another PC
        rb = None
        if self._readback is not None:
            try:
                rb = self._readback()
            except Exception:                        # noqa: BLE001
                rb = None
        with self._lock:
            self._value = v
            if self._recording:
                if len(self._buf) == self._buf.maxlen:
                    self._overflow = True
                self._buf.append((t, v, rb))

    # ---- status -------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def ramp_id(self) -> int:
        return self._ramp_id

    @property
    def value(self) -> float:
        """The last value sent (NaN before the first)."""
        return self._value

    def status(self) -> dict:
        """Plain values for the module's status snapshot."""
        with self._lock:
            return {"ramping": bool(self._running), "ramp_id": int(self._ramp_id),
                    "ramp_target": _num(self._target), "ramp_rate": _num(self._rate),
                    "ramp_value": _num(self._value), "ramp_error": self._error,
                    "ramp_end": self._last_reason}

    # ---- the record, in the suite's stream format ----------------------------------

    def channels(self) -> list[str]:
        out = [self.channel]
        if self._readback is not None:
            out.append(f"{self.channel}_readback")
        return out

    def _rest_value(self) -> float:
        """The value AT REST: the module's own (the getter) -- a plain set
        after the last walk changed it without telling us -- else the last
        value this walk sent. NaN if neither is known."""
        if self._get is not None:
            try:
                return float(self._get())
            except Exception:                        # noqa: BLE001
                pass
        return self._value

    def stream_start(self) -> int:
        """Clear the record and start recording. Returns the stream id."""
        # the PRESENT value as the first sample: a fly row's lead-in, at rest,
        # needs a value to look up before the walk sends its first step
        rest = None if self._running else self._rest_value()
        with self._lock:
            self._buf.clear()
            self._overflow = False
            self._stream_id += 1
            self._recording = True
            if rest is not None and math.isfinite(rest):
                self._buf.append((time.time(), rest, None))
            return self._stream_id

    def stream_read(self) -> dict:
        """Everything recorded since the last read (drains it)."""
        rest = None if self._running else self._rest_value()
        with self._lock:
            rows = list(self._buf)
            self._buf.clear()
            overflow, self._overflow = self._overflow, False
            sid = self._stream_id
            if (rest is not None and not self._running and math.isfinite(rest)
                    and self._recording):
                # at rest the value does not change, but the record must go on
                # covering time (a lagging detector's tail is looked up later)
                rows.append((time.time(), rest, None))
        return self._chunk(rows, overflow, sid)

    def stream_stop(self) -> dict:
        """Stop recording; returns the rest."""
        chunk = self.stream_read()
        with self._lock:
            self._recording = False
        return chunk

    def _chunk(self, rows, overflow, sid) -> dict:
        values = {self.channel: [_num(r[1]) for r in rows]}
        if self._readback is not None:
            values[f"{self.channel}_readback"] = [_num(r[2]) for r in rows]
        return {"id": sid, "t": [r[0] for r in rows], "values": values,
                "delay_s": {c: 0.0 for c in values},
                "overflow": overflow, "now": time.time()}
