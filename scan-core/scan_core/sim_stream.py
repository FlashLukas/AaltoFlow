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


class SimSweepStreamer:
    """A simulated VNA that sweeps back to back and streams every sweep.

    The module-side twin is vna-control's stream (Analyzer.stream_read); this
    is the same thing in-process for build_sim_registry(), so a fly scan with
    a TRACE detector runs without the lab (2026-10-09).

    What makes it worth simulating is TIME. A sweep is not instantaneous: its
    points are measured one after another over `sweep_s_fn()` seconds, and if
    the field moves meanwhile, point i sees the field of ITS moment. So each
    sweep reads the field when it starts and when it ends, and gives point i
    the field in between at the centre of its dwell:

        t_i = t_start + (i + 0.5) / n * (t_end - t_start)

    (linear in time, which is what a ramp is between two readings). A whole
    trace is time-stamped at its MIDDLE -- the mean of the t_i -- and a single
    frequency point channel at its own t_i: the point of streaming single
    points is exactly this sharper timing.

    quantities: {channel: fn(trace) -> trace}; a fn raising ValueError puts its
                message into the chunk's `errors` (u with no reference)
    points:     fn() -> {channel: index into the frequency grid}
    """

    def __init__(self, group: str, freqs_fn, trace_fn, field_fn, sweep_s_fn,
                 quantities=None, points=None, max_sweeps: int = 5000):
        self.group = group
        self._freqs = freqs_fn
        self._trace = trace_fn
        self._field = field_fn
        self._sweep_s = sweep_s_fn
        self._quantities = dict(quantities or {})
        self._points = points or (lambda: {})
        self._buf: deque = deque(maxlen=int(max_sweeps))
        self._overflow = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self.stop()
        with self._lock:
            self._buf.clear()
            self._overflow = False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"sim-sweep-{self.group}",
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

    def _run(self):
        import numpy as np
        while not self._stop.is_set():
            dur = max(float(self._sweep_s()), 1e-3)
            t0, b0 = time.time(), float(self._field())
            end = time.monotonic() + dur
            while not self._stop.is_set():
                left = end - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(left, 0.01))     # high-resolution sleep (gotcha #34)
            if self._stop.is_set():
                return                          # a sweep cut short is no sweep
            t1, b1 = time.time(), float(self._field())
            f = np.asarray(self._freqs(), dtype=float)
            frac = (np.arange(f.size) + 0.5) / max(f.size, 1)
            z = self._trace(f, b0 + (b1 - b0) * frac)
            with self._lock:
                if len(self._buf) == self._buf.maxlen:
                    self._overflow = True
                self._buf.append((t0, t1, z))

    def _chunk(self, rows, overflow) -> dict:
        import numpy as np
        f = np.asarray(self._freqs(), dtype=float)
        n = f.size
        t0 = np.array([r[0] for r in rows], dtype=float)
        t1 = np.array([r[1] for r in rows], dtype=float)
        traces = (np.array([r[2] for r in rows], dtype=complex) if rows
                  else np.zeros((0, n), dtype=complex))
        values, errors, t_ch = {}, {}, {}
        for ch, fn in self._quantities.items():
            try:
                values[ch] = np.array([fn(z) for z in traces], dtype=complex).reshape(-1, n)
            except ValueError as exc:
                errors[ch] = str(exc)
        pts = dict(self._points())
        for ch, i in pts.items():
            values[ch] = traces[:, i]
            t_ch[ch] = t0 + (i + 0.5) / max(n, 1) * (t1 - t0)
        return {"t": 0.5 * (t0 + t1), "t_start": t0, "t_end": t1,
                "values": values, "t_ch": t_ch, "errors": errors,
                # the samples mean the same thing only while these hold
                "settings": {"points": int(n),
                             "start": float(f[0]) if n else None,
                             "stop": float(f[-1]) if n else None,
                             "channels": {k: int(v) for k, v in pts.items()}},
                # 0: every time stamp is already the CENTRE of its measurement
                "delay_s": {ch: 0.0 for ch in values},
                "overflow": overflow}
