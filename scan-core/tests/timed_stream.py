"""Test helpers for fly scans: a stage with a MEMORY and a HARDWARE-TIMED stream.

Why these exist (2026-10-01). The fly tests used to record their fake streams
the way a Python poll thread does: wake up every 1/rate s, sample the world
NOW, sleep again. Under CPU load (a full test run, another suite in parallel)
that thread is not woken on time -- Windows hands the core back a scheduler
quantum later, ~35 ms, sometimes 170 ms -- so a "400 Hz" stream delivered one
sample per 35 ms and a pixel that should hold 10 samples held 0-2. The tests
then failed on `samples per pixel >= 3` although the fly engine had done
everything right: they were measuring the test machine, not the engine.

The fix models what a lock-in or a DAQ really does: the instrument samples on
its OWN clock, into a buffer, and the computer merely collects the buffer
later, however late it is. Here the samples sit on a fixed grid of times and
are computed when the engine READS the stream, each from the world's state AT
ITS OWN TIME. For that the world must remember where the stage was at any
past moment -- hence `Track`, a stage kept as a history of straight moves.

What the tests still prove is unchanged, and it is all about POSITION: a pixel
the stage never crossed at the fly speed (a row ended early, gotcha #35; an
approach turned round, 2026-09-28) gets no samples, however punctually they
are taken. What they no longer prove is that this PC can run a 400 Hz thread
under load -- which was never the engine's job.
"""

from __future__ import annotations

import bisect
import threading
import time


class Track:
    """One stage axis moving in straight lines at a set speed, with a history.

    `pos(t)` is exact for ANY past time t, not only for now: every move is kept
    as a segment (t0, p0, t1, p1), and a move commanded while another is under
    way starts a new segment from where the stage is at that moment.
    """

    def __init__(self, clock=time.time, p=0.0):
        self.clock = clock
        self.lock = threading.Lock()
        t = clock()
        self._t0s = [t]                     # segment start times, ascending
        self._segs = [(t, p, t, p)]         # (t0, p0, t1, p1)

    @staticmethod
    def _on(seg, t):
        t0, p0, t1, p1 = seg
        if t >= t1 or t1 == t0:
            return p1
        if t <= t0:
            return p0
        return p0 + (p1 - p0) * (t - t0) / (t1 - t0)

    def _seg_at(self, t):
        return self._segs[max(0, bisect.bisect_right(self._t0s, t) - 1)]

    def move(self, target, speed):
        """Start a move to `target` at `speed`; returns the time it arrives."""
        with self.lock:
            t = self.clock()
            here = self._on(self._segs[-1], t)
            t1 = t + abs(float(target) - here) / float(speed)
            self._segs.append((t, here, t1, float(target)))
            self._t0s.append(t)
            return t1

    def pos(self, t=None):
        with self.lock:
            t = self.clock() if t is None else t
            return self._on(self._seg_at(t), t)

    def moving(self, t=None):
        with self.lock:
            t = self.clock() if t is None else t
            return t < self._segs[-1][2]


class TimedStream:
    """A stream sampled on a fixed time grid, like hardware with its own clock.

    `sample_at(t)` -> {channel: value} must describe the world at time t (on
    `clock`). Samples between the last read and now are produced at every
    read/stop, so none is ever missing, however late the reader comes.
    `stamp(t)` gives the time stamp the samples carry (a module on another PC
    can stamp them on a wrong clock).
    """

    def __init__(self, sample_at, rate_hz, clock=time.time, stamp=None,
                 delay_s=None, extra=None):
        self._sample_at = sample_at
        self._period = 1.0 / float(rate_hz)
        self._clock = clock
        self._stamp = stamp or (lambda t: t)
        self._delay = delay_s or {}
        self._extra = extra or (lambda: {})   # more keys for each chunk
        self._lock = threading.Lock()
        self._next = None                     # grid time of the next sample; None = off

    def start(self):
        with self._lock:
            self._next = self._clock()

    def _take(self):
        rows = []
        if self._next is not None:
            now = self._clock()
            while self._next <= now:
                rows.append((self._stamp(self._next), self._sample_at(self._next)))
                self._next += self._period
        chans = list(rows[0][1]) if rows else []
        return {"t": [r[0] for r in rows],
                "values": {c: [r[1][c] for r in rows] for c in chans},
                "delay_s": {c: self._delay.get(c, 0.0) for c in chans},
                "overflow": False, **self._extra()}

    def read(self):
        with self._lock:
            return self._take()

    def stop(self):
        with self._lock:
            chunk = self._take()
            self._next = None
            return chunk
