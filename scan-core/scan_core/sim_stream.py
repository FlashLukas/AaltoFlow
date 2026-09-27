"""sim_stream.py -- a simulated STREAM, so a fly scan can run without the lab.

A real module records its stream in its own poll thread (hf2's demodulator
poller, kim's position recorder) and hands it over with `stream_read`. This is
the same thing in-process for `build_sim_registry()`: a thread that samples a
function at a fixed rate, stamps each sample with time.time(), and keeps it
until read.

It can also put a lock-in's LOW-PASS FILTER on some channels, because the
filter lag is the one physical effect a fly scan has to correct and a
simulator without it would prove nothing: an edge swept forwards and backwards
would line up whether or not the correction worked. The filter is `order`
identical first-order stages of time constant `tau` -- the Zurich demodulator
model (hf2's filters.py) -- and the declared delay is its GROUP DELAY,
order * tau. Why that number: filtering is a convolution with the filter's
impulse response, and a convolution moves the centroid of any feature by the
kernel's mean, which for n stages of tau is exactly n * tau.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque

from .registry import StreamSpec

#: Filter sub-steps per stream sample (see _run).
SUB_STEPS = 16


class SimStreamer:
    """Samples `sample_fn()` -> {channel: value} at `rate_hz` while started."""

    def __init__(self, group: str, sample_fn, rate_hz: float = 200.0,
                 filtered=(), tau_fn=None, order: int = 2,
                 derive=None, max_samples: int = 500_000):
        self.group = group
        self._sample = sample_fn
        self._period = 1.0 / float(rate_hz)
        self._filtered = set(filtered)
        self._tau_fn = tau_fn or (lambda: 0.0)
        self._order = max(1, int(order))
        # derive(vals) -> vals, applied AFTER the filter: R and phase must be
        # computed from the filtered X and Y (as a lock-in does), not filtered
        # themselves -- a filtered phase would average across the +-180 wrap.
        self._derive = derive
        #: channels computed from filtered ones: they carry the same lag
        self.derived: set = set()
        self._buf: deque = deque(maxlen=max_samples)
        self._overflow = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stages: dict = {}           # channel -> [stage values]

    # ---- StreamSpec callbacks ---------------------------------------------

    def start(self):
        self.stop()
        with self._lock:
            self._buf.clear()
            self._overflow = False
            self._stages = {}
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"sim-stream-{self.group}",
                                        daemon=True)
        self._thread.start()

    def read(self) -> dict:
        with self._lock:
            rows = list(self._buf)
            self._buf.clear()
            overflow, self._overflow = self._overflow, False
        return self._chunk(rows, overflow)

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        return self.read()

    def spec(self) -> StreamSpec:
        return StreamSpec(self.group, self.start, self.read, self.stop)

    # ---- internals ----------------------------------------------------------

    def _chunk(self, rows, overflow) -> dict:
        t = [r[0] for r in rows]
        chans = list(rows[0][1].keys()) if rows else []
        values = {c: [r[1][c] for r in rows] for c in chans}
        delay = self._order * float(self._tau_fn())
        return {"t": t, "values": values,
                "delay_s": {c: (delay if c in self._filtered or c in self.derived else 0.0)
                            for c in chans},
                "overflow": overflow}

    def _run(self):
        last = time.time()
        next_t = time.monotonic()
        while not self._stop.is_set():
            now = time.time()
            vals = dict(self._sample())
            dt = max(0.0, now - last)
            last = now
            tau = float(self._tau_fn())
            for c in self._filtered:
                if c not in vals:
                    continue
                x = float(vals[c])
                st = self._stages.get(c)
                if st is None:                      # start settled on the first sample
                    st = self._stages[c] = [x] * (self._order + 1)
                # The input between two samples is taken as a straight line
                # (st[-1] holds the previous one), and the filter is stepped in
                # SUB_STEPS small steps along it. Applying the new sample to the
                # whole interval at once makes every stage run ahead by about
                # half a sample period -- at 200 Hz and order 2 that is 5 ms of
                # lag missing, which is exactly the kind of error a test of the
                # lag correction must not have hidden inside the simulator.
                prev = st[-1]
                k = 1.0 if tau <= 0 else 1.0 - math.exp(-dt / SUB_STEPS / tau)
                for m in range(1, SUB_STEPS + 1):
                    inp = prev + (x - prev) * m / SUB_STEPS
                    for i in range(self._order):
                        st[i] += (inp - st[i]) * k
                        inp = st[i]
                st[-1] = x
                vals[c] = st[self._order - 1]
            if self._derive is not None:
                vals = self._derive(vals)
            with self._lock:
                if len(self._buf) == self._buf.maxlen:
                    self._overflow = True
                self._buf.append((now, vals))
            next_t += self._period
            delay = next_t - time.monotonic()
            if delay > 0:
                # time.sleep, not Event.wait: on Windows a timed wait on a lock
                # rounds up to the 15.6 ms system tick, which caps the "200 Hz"
                # stream at ~64 Hz; sleep uses a high-resolution timer.
                time.sleep(delay)
            else:
                next_t = time.monotonic()           # fell behind: do not burst
