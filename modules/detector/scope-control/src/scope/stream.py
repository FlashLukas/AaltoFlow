"""stream.py -- a continuous record of the scope's RECORDS, for a fly scan.

scan-core's fly scan moves a stage (or sweeps a knob) without stopping and
records the detectors and the position all the way; afterwards it averages the
detector samples per pixel of MEASURED position. For that it needs every
reading with the time it was taken. For a scope one reading is one triggered
RECORD: the brain's trace thread computes the record's numbers (mean, rms,
peak-to-peak, amplitude, frequency per channel, the phase CH2 - CH1) and
appends them here while a stream runs.

The wire (docs/DEVELOPER_NOTES.md, the stream verbs):

    stream_start  -> {"ok": true, "stream_id": n}        clear and start recording
    stream_read   -> {"ok": true, "stream": {...}}       everything since the last read
    stream_stop   -> {"ok": true, "stream": {...}}       the rest, and stop

    "stream": {"id": n, "t": [...], "values": {"ch1_pk2pk": [...], ...},
               "t_start": [...], "t_end": [...],
               "delay_s": {"ch1_pk2pk": 0.035, ...}, "overflow": false,
               "missed": 0, "now": <time.time()>}

THE COPY. This is hf2's StreamRecorder (the suite copies stream.py into each
module that streams, like theme.py), with two additions a record needs:
  * `t_start` / `t_end` per sample: when the record began and ended. A record
    lasts as long as 14 divisions of the time base -- scan-core keeps a row
    recording that long after the stage stops (flyscan._sample_span), or the
    record under way at the end of a row is lost.
  * `missed`: records the scope made that the module did NOT read (it holds
    only its latest record; a slow read lets one go by unseen). The module can
    only ESTIMATE that number from the gaps between the records it did read;
    it is said, never hidden. `overflow` stays what it is everywhere: this
    bounded buffer dropped samples because nobody read them.

Times are `time.time()` of THIS computer; `now` is the clock at the reply, so
a coordinator on another PC can work out how far apart the two clocks are.
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
        self._missed = 0
        self._id = 0

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> int:
        """Clear the buffer and start recording. Returns the new stream id."""
        with self._lock:
            self._buf.clear()
            self._overflow = False
            self._missed = 0
            self._id += 1
            self._running = True
            return self._id

    def append(self, t: float, values, t_start: float | None = None,
               t_end: float | None = None) -> None:
        """Record one reading (a sequence in `channels` order). No-op when stopped.

        Called from the brain's trace thread for every record it takes, so it
        must stay cheap: one lock and one deque append."""
        if not self._running:
            return
        with self._lock:
            if not self._running:
                return
            if len(self._buf) == self._buf.maxlen:
                self._overflow = True
            self._buf.append((float(t), tuple(values),
                              float(t if t_start is None else t_start),
                              float(t if t_end is None else t_end)))

    def note_missed(self, n: int) -> None:
        """`n` records went by unread (an estimate, see the module docstring)."""
        if n > 0 and self._running:
            with self._lock:
                self._missed += int(n)

    def read(self) -> dict:
        """Everything recorded since the last read; removes it from the buffer."""
        with self._lock:
            rows = list(self._buf)
            self._buf.clear()
            overflow, self._overflow = self._overflow, False
            missed, self._missed = self._missed, 0
            sid = self._id
        return self._chunk(rows, overflow, missed, sid)

    def stop(self) -> dict:
        """Stop recording and return what was left."""
        with self._lock:
            self._running = False
        return self.read()

    def _chunk(self, rows, overflow, missed, sid) -> dict:
        try:
            delays = dict(self._delay_fn() or {})
        except Exception:
            delays = {}
        values = {c: [_num(r[1][i]) for r in rows] for i, c in enumerate(self.channels)}
        return {"id": sid, "t": [r[0] for r in rows], "values": values,
                "t_start": [r[2] for r in rows], "t_end": [r[3] for r in rows],
                "delay_s": {c: float(delays.get(c, 0.0)) for c in self.channels},
                "overflow": overflow, "missed": int(missed), "now": time.time()}


def _num(v):
    """JSON-safe: NaN / inf travel as null (Python would write the token NaN)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
