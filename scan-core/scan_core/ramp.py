"""ramp.py -- a knob that can SWEEP CONTINUOUSLY, for a fly scan over any knob.

A fly scan (flyscan.py) used to be for stages only: the stage moves slowly
along a row while the detectors are recorded, and every sample is binned by
the stage's measured position. Lukas (2026-10-09): "not just XY scanning ...
magnetic field, RF frequency, RF power, phase, fianium wavelength". Those knobs
do not move by themselves at a speed you set -- an RF generator JUMPS to the
frequency it is told. So a module that can sweep one declares a `ramp` block
on that control in `describe` (INSTRUMENT_MODULE_GUIDE.md section 6b), and
scan-core turns it into a RampSpec here:

    "ramp": {
      "kind":     "software",          # or "hardware": who walks the value --
                                       # the service, or the instrument itself
      "start":    {"verb": "ramp_frequency",
                   "args": {"to": "frequency_Hz", "rate": "rate_Hz_per_s"},
                   "extra": {}},       # optional fixed arguments
      "stop":     {"verb": "ramp_stop"},
      "rate":     {"unit": "MHz/s", "min": 0.001, "max": 1000, "default": 10},
      "readback": {"stream": {"group": "ramp", "channel": "frequency"},
                   "measured": false},
      "done":     {"key": "ramping", "id_key": "ramp_id"}
    }

Units: `to` and the rate are in the descriptor's SCAN unit (MHz, MHz/s) and go
on the wire multiplied by the descriptor's `scale`, exactly like a set
(wire = value x scale). The readback stream carries WIRE units, like every
stream, and is divided by the same scale.

THE READBACK is what the samples are binned by, and "measured" says what it is:
  * measured: true  -- the instrument's real value (clMag's Hall probe, the
                       PPMS magnet's field): binned by MEASUREMENT;
  * measured: false -- the value the service COMMANDED (a generator cannot
                       report its frequency while sweeping, or it would take
                       too long): binned by COMMAND + time stamp.
  It is a stream (`stream`, as for any streamed parameter), or a status key
  (`read_path`) that scan-core then samples itself (coarse: the status rate),
  or absent with measured false: scan-core then computes the commanded value
  from the time the ramp was started and the rate. The data file says which
  one it was: the fly coordinate's attribute `fly_binned_by` is "measurement"
  or "command".

DONE: the start reply carries the ramp's number (`ramp_id`), and the status
publishes the number of the newest ramp taken up (`id_key`) and whether it is
still running (`key`). The ramp is over when the status shows OUR number and
not running -- numbered for the same reason as an acquisition (gotcha #17): a
"ramping: false" frame from before the start must not pass for the end.
"""

from __future__ import annotations

import math
import threading
import time

from .registry import Parameter, StreamSpec


class RampSpec:
    """What scan-core needs to sweep a knob continuously.

    start(to, rate) -> handle   begin a sweep (non-blocking), values in SCAN units
    stop()                      end it where it is (Abort)
    done(handle) -> bool        has THAT sweep ended? (non-blocking)

    kind          "software" | "hardware" (informative: logs, the file)
    rate_unit     the knob's unit per second, e.g. "mT/s"
    rate_limits   (min, max) in rate_unit
    rate_default  a sensible pace, or None
    measured      True = the readback is the instrument's real value
    readback      a Parameter-like object with .stream / .stream_channel /
                  .stream_scale (what the row is binned by), or None = compute
                  the commanded value from start time and rate (CommandTrack)
    """

    def __init__(self, start_fn, stop_fn, done_fn, *, kind="software",
                 rate_unit="", rate_limits=(0.0, math.inf), rate_default=None,
                 measured=False, readback=None):
        self._start, self._stop, self._done = start_fn, stop_fn, done_fn
        self.kind = str(kind or "software")
        self.rate_unit = rate_unit
        lo, hi = rate_limits
        self.rate_limits = (float(lo if lo is not None else 0.0),
                            float(hi if hi is not None else math.inf))
        self.rate_default = None if rate_default is None else float(rate_default)
        self.measured = bool(measured)
        self.readback = readback

    def start(self, to: float, rate: float):
        return self._start(float(to), float(rate))

    def stop(self) -> None:
        self._stop()

    def done(self, handle) -> bool:
        return bool(self._done(handle))

    @property
    def binned_by(self) -> str:
        """What a fly row over this knob is binned by (the file's attribute)."""
        return "measurement" if (self.measured and self.readback is not None) else "command"


class Readback(Parameter):
    """The streamed value a ramp is binned by. Not registered: a ramp's own
    record ("the commanded frequency") is not a parameter anyone else should
    pick, it exists for the fly scan that drives the ramp."""

    def __init__(self, id, label, unit, stream, channel, scale=1.0, get_fn=None):
        super().__init__(id, label, unit, "readback")
        self.stream = stream
        self.stream_channel = channel
        self.stream_scale = float(scale or 1.0)
        self._get = get_fn

    def get(self):
        if self._get is None:
            return float("nan")
        return self._get()


class PolledStream:
    """A stream made by SAMPLING a getter (a status key) at a fixed rate.

    For a module whose ramp readback is only a status value: scan-core reads
    it itself while the row flies. Coarse -- a status cache is a few frames a
    second -- but honest: each sample is stamped when it was READ.
    """

    def __init__(self, group: str, getter, channel: str = "value",
                 rate_hz: float = 20.0, max_samples: int = 200_000):
        self.group = group
        self._get = getter
        self.channel = channel
        self._period = 1.0 / float(rate_hz)
        self._rows: list = []
        self._max = int(max_samples)
        self._overflow = False
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self.stop()
        with self._lock:
            self._rows = []
            self._overflow = False
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._halt,),
                                        name=f"poll-{self.group}", daemon=True)
        self._thread.start()

    def read(self) -> dict:
        with self._lock:
            rows, self._rows = self._rows, []
            ov, self._overflow = self._overflow, False
        return {"t": [r[0] for r in rows], "values": {self.channel: [r[1] for r in rows]},
                "delay_s": {self.channel: 0.0}, "overflow": ov}

    def stop(self) -> dict:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        return self.read()

    def spec(self) -> StreamSpec:
        return StreamSpec(self.group, self.start, self.read, self.stop)

    def _run(self, halt):
        next_t = time.monotonic()
        while not halt.is_set():
            try:
                v = float(self._get())
            except Exception:
                v = float("nan")
            t = time.time()
            with self._lock:
                if len(self._rows) >= self._max:
                    self._rows.pop(0)
                    self._overflow = True
                self._rows.append((t, v))
            next_t += self._period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)          # time.sleep, not Event.wait (gotcha #34)
            else:
                next_t = time.monotonic()


class CommandTrack:
    """The COMMANDED value of a ramp, computed: start value, start time, rate.

    For a ramp with no readback at all (measured false and nothing streamed).
    The commanded value is a straight line in time between the start and the
    end of the sweep, so samples at the moments of each read -- plus the
    corner where the sweep reaches its target -- interpolate it EXACTLY.
    Behaves like a StreamSpec (start / read / stop) so flyscan.py bins with it
    as with any other stream.
    """

    channel = "commanded"

    def __init__(self, group: str = "command"):
        self.group = group
        self._go = None            # (t_go, a, b, rate) on time.time()
        self._value = float("nan")
        self._corner_sent = False
        self._start_sent = False
        self._lock = threading.Lock()

    def rest(self, value: float) -> None:
        """Where the knob sits before the sweep (the lead-in samples)."""
        with self._lock:
            self._value = float(value)
            self._go = None

    def go(self, a: float, b: float, rate: float, t_go: float | None = None) -> None:
        with self._lock:
            self._go = (time.time() if t_go is None else t_go, float(a), float(b),
                        abs(float(rate)))
            self._corner_sent = False
            self._start_sent = False

    def stopped(self) -> None:
        """The sweep was stopped: the value stays where it got to."""
        now = time.time()
        with self._lock:
            self._value = self._at(now)
            self._go = None

    def _at(self, t: float) -> float:
        if self._go is None:
            return self._value
        t0, a, b, rate = self._go
        if rate <= 0:
            return b
        dur = abs(b - a) / rate
        el = max(0.0, t - t0)
        if el >= dur:
            return b
        return a + math.copysign(rate * el, b - a)

    def start(self):
        pass

    def read(self) -> dict:
        now = time.time()
        ts, vs = [], []
        with self._lock:
            if self._go is not None:
                t0, a, b, rate = self._go
                dur = abs(b - a) / rate if rate > 0 else 0.0
                if not self._start_sent:      # the corner where it set off
                    ts.append(t0)
                    vs.append(a)
                    self._start_sent = True
                if not self._corner_sent and now >= t0 + dur:
                    ts.append(t0 + dur)
                    vs.append(b)
                    self._corner_sent = True
            ts.append(now)
            vs.append(self._at(now))
        order = sorted(range(len(ts)), key=lambda i: ts[i])
        return {"t": [ts[i] for i in order],
                "values": {self.channel: [vs[i] for i in order]},
                "delay_s": {self.channel: 0.0}, "overflow": False}

    def stop(self) -> dict:
        return self.read()

    def spec(self) -> StreamSpec:
        return StreamSpec(self.group, self.start, self.read, self.stop)


# ───────────────────────── from a module's describe ──────────────────────────

def ramp_from_descriptor(d: dict, inst, module: str, streams: dict, stream_from,
                         pid: str, on_warn=None) -> RampSpec | None:
    """Build the RampSpec of a control from its `ramp` block (None if absent
    or unusable -- unusable is reported through on_warn, never raised: a
    module with a malformed ramp block still works for stepped scans).

    `streams` / `stream_from` are manifest.py's per-module stream cache and
    factory, so a readback that shares a group with streamed detectors is
    started and read ONCE with them.
    """
    spec = d.get("ramp")
    if not isinstance(spec, dict):
        return None

    def warn(msg):
        if on_warn:
            on_warn(f"{pid}: ramp block {msg}; the knob cannot be flown")

    start = spec.get("start") or {}
    verb = start.get("verb")
    args = start.get("args") or {}
    to_arg, rate_arg = args.get("to"), args.get("rate")
    if not (verb and to_arg and rate_arg):
        warn("needs start.verb and start.args {to, rate}")
        return None
    stop_verb = (spec.get("stop") or {}).get("verb")
    if not stop_verb:
        warn("needs stop.verb")
        return None
    scale = float(d.get("scale", 1.0) or 1.0)
    extra = dict(start.get("extra") or {})
    stop_extra = dict((spec.get("stop") or {}).get("extra") or {})
    done = spec.get("done") or {}
    key = done.get("key", "ramping")
    id_key = done.get("id_key", "ramp_id")
    reply_key = done.get("reply_key", "ramp_id")
    rate = spec.get("rate") or {}

    def start_fn(to, r):
        reply = inst.command(verb, **{to_arg: to * scale, rate_arg: abs(r) * scale},
                             **extra)
        rid = reply.get(reply_key)
        return {"id": rid if isinstance(rid, (int, float)) and not isinstance(rid, bool)
                else None, "seen_running": False, "t": time.monotonic()}

    def stop_fn():
        inst.command(stop_verb, **stop_extra)

    def done_fn(handle):
        from .instrument import _lookup
        st = inst.status()
        running = bool(_lookup(st, key))
        if handle.get("id") is not None:
            got = _lookup(st, id_key)
            if not isinstance(got, (int, float)) or got < handle["id"]:
                return False                    # not even taken up yet (gotcha #2)
            return not running
        # no number in the reply: running seen once, then not -- with a grace
        # period for a ramp so short the status never caught it running
        if running:
            handle["seen_running"] = True
            return False
        return handle["seen_running"] or time.monotonic() - handle["t"] > 2.0

    # the readback
    rb_spec = spec.get("readback") or {}
    measured = bool(rb_spec.get("measured", False))
    readback = None
    if isinstance(rb_spec.get("stream"), dict) and rb_spec["stream"].get("channel"):
        s = rb_spec["stream"]
        group = f"{module}.{s.get('group', 'stream')}"
        k = (group, s.get("start_verb"), s.get("read_verb"), s.get("stop_verb"))
        if k not in streams:
            streams[k] = stream_from(group, s, inst)
        readback = Readback(f"{pid}#readback", f"{d.get('label', pid)} (readback)",
                            d.get("unit", ""), streams[k], s["channel"], scale)
    elif rb_spec.get("read_path"):
        path = list(rb_spec["read_path"])

        def get(_p=path):
            from .instrument import _lookup
            v = _lookup(inst.status(), _p)
            return float(v) / scale if isinstance(v, (int, float)) else float("nan")

        poll = PolledStream(f"{module}.{pid}.poll", get)
        readback = Readback(f"{pid}#readback", f"{d.get('label', pid)} (readback)",
                            d.get("unit", ""), poll.spec(), poll.channel, 1.0, get)
    elif measured:
        warn("says measured but names no stream or read_path; binned by command")
        measured = False

    return RampSpec(start_fn, stop_fn, done_fn, kind=spec.get("kind", "software"),
                    rate_unit=rate.get("unit", f"{d.get('unit', '')}/s"),
                    rate_limits=(rate.get("min", 0.0), rate.get("max", math.inf)),
                    rate_default=rate.get("default"), measured=measured,
                    readback=readback)
