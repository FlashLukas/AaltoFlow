"""stream.py -- a continuous record of readings, for a FLY SCAN.

scan-core's fly scan moves a stage without stopping and records the detectors
and the stage position all the way; afterwards it averages the detector
samples per pixel of MEASURED position. For that it needs every reading with
the time it was taken, not one settled value on request. This file is that
record: the brain's poll thread appends each reading it takes anyway, and the
service hands out what has accumulated.

The wire (docs/DEVELOPER_NOTES.md, the stream verbs):

    stream_start  -> {"ok": true, "stream_id": n}        clear and start recording
    stream_read   -> {"ok": true, "stream": {...}}       everything since the last read
    stream_stop   -> {"ok": true, "stream": {...}}       the rest, and stop

    "stream": {"id": n, "t": [...], "values": {"x1": [...], ...},
               "delay_s": {"x1": 0.02, ...}, "overflow": false, "now": <time.time()>}

Times are `time.time()` of THIS computer (high resolution on Windows since
Python 3.13). `now` is the clock at the moment of the reply, which lets a
coordinator on another PC work out how far apart the two clocks are.
`delay_s` is how late each channel is -- for a lock-in, its filter's group
delay -- stated here because only the module knows its current settings.

The buffer is bounded (a scan that forgets to read cannot eat the memory);
when it overflows the oldest samples go and `overflow` says so.

The suite's convention: this file is COPIED into each module that streams
(hf2, kim, smaract), like theme.py, so the modules stay independent packages.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque


class StreamRecorder:
    def __init__(self, channels, delay_fn=None, max_samples: int = 200_000):
        self.channels = list(channels)
        self._delay_fn = delay_fn or (lambda: {})
        self._buf: deque = deque(maxlen=int(max_samples))
        self._lock = threading.Lock()
        self._running = False
        self._overflow = False
        self._id = 0

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> int:
        """Clear the buffer and start recording. Returns the new stream id."""
        with self._lock:
            self._buf.clear()
            self._overflow = False
            self._id += 1
            self._running = True
            return self._id

    def append(self, t: float, values) -> None:
        """Record one reading (a sequence in `channels` order). No-op when stopped.

        Called from the brain's poll thread for every reading it takes, so it
        must stay cheap: one lock and one deque append.
        """
        if not self._running:
            return
        with self._lock:
            if not self._running:
                return
            if len(self._buf) == self._buf.maxlen:
                self._overflow = True
            self._buf.append((float(t), tuple(values)))

    def read(self) -> dict:
        """Everything recorded since the last read; removes it from the buffer."""
        with self._lock:
            rows = list(self._buf)
            self._buf.clear()
            overflow, self._overflow = self._overflow, False
            sid = self._id
        return self._chunk(rows, overflow, sid)

    def stop(self) -> dict:
        """Stop recording and return what was left."""
        with self._lock:
            self._running = False
        return self.read()

    def _chunk(self, rows, overflow, sid) -> dict:
        try:
            delays = dict(self._delay_fn() or {})
        except Exception:
            delays = {}
        values = {c: [_num(r[1][i]) for r in rows] for i, c in enumerate(self.channels)}
        return {"id": sid, "t": [r[0] for r in rows], "values": values,
                "delay_s": {c: float(delays.get(c, 0.0)) for c in self.channels},
                "overflow": overflow, "now": time.time()}


def _num(v):
    """JSON-safe: NaN / inf travel as null (Python would write the token NaN)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
