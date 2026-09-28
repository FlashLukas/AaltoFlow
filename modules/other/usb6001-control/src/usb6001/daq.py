"""The Daq: the brain between the wire and the NI USB-6001 backend.

Lukas, 2026-09-28: "a module for general usb DAQ - 6001. I want to control AI
and AOs. And define some digital ports as ins and some as outs ...
reconfigured upon restart."

What it does:
  * OUTPUTS (ao0/ao1, digital lines configured "out"): clamp to the limits,
    write, and only AFTER the write succeeded remember the value as the echo a
    scan waits for (gotcha #40: an echo stored before the write could let a
    frame show the new value while the card still outputs the old one).
  * INPUTS: one POLL THREAD reads every enabled analog input (the mean of
    `samples_per_read` samples) and every input line, `poll_hz` times a
    second. Those are the LIVE values for a front panel.
  * FRESH READS for a scan (`acquire`, and read_ai / read_di which use it): the
    poll thread latches the next reading that STARTED AFTER the trigger as
    `sample`, numbered. A scan waits for THAT number (gotcha #17), so it can
    never file a voltage measured before its scan point was set.

THREAD RULES (the same as hf2 and pm16, learned the hard way there):
  * ONE poll thread owns every hardware READ. status() only copies what that
    thread stored and never touches the card, so a slow USB call cannot stall
    the status publisher, and a dead link shows as `hw_error` instead of a
    healthy-looking panel full of old numbers.
  * EVERY backend call (reads and writes) runs under `_hw`, an RLock: the
    command thread and the poll thread would otherwise talk to the card at
    the same moment.
  * Live control state lives in brain attributes; setters never touch a
    snapshot (gotcha #1).

ADOPT ON START (Lukas's rule for every module: read the instrument, change
nothing). start() creates the tasks for the configured layout -- that is
configuration, not a value -- and writes NOTHING to AO or DO. The 6001 cannot
read its analog outputs back, so AO shows as UNKNOWN until the first set_ao of
this session. Output lines are read back if the card allows it, else unknown.
The one exception is a line whose config asks for `initial = low/high`: that
level is written once, deliberately, because only then is the level KNOWN.
"""

from __future__ import annotations

import copy
import math
import threading
import time
from dataclasses import dataclass, field

from .backends.base import DaqBackend, Layout
from .config import (AI_CHANNELS, AO_CHANNELS, DIO_LINES, Config, line_index,
                     sanitise)

_NAN = float("nan")


@dataclass
class Status:
    """One snapshot of the card, for status() and the wire. Lists are indexed
    by channel (ai: 0..7, ao: 0..1) or by line (dio: 0..12, config.DIO_LINES)."""

    connected: bool = False
    idn: str = ""
    hw_error: str = ""                 # "" = the last hardware read worked
    fault: str = ""                    # never set by this module (nothing to latch)
    # live analog inputs (NaN for a channel that is not enabled)
    ai_V: list = field(default_factory=lambda: [_NAN] * 8)
    ai: list = field(default_factory=lambda: [_NAN] * 8)       # scaled
    ai_enabled: list = field(default_factory=lambda: [False] * 8)
    reads: int = 0                     # live readings since start
    read_ms: float = _NAN              # how long the last reading took
    # analog outputs: the ECHO of the last successful write, NaN = unknown
    ao_V: list = field(default_factory=lambda: [_NAN, _NAN])
    ao_known: list = field(default_factory=lambda: [False, False])
    # digital lines: level (bool, or None = unknown / unused) and the ACTIVE direction
    dio: list = field(default_factory=lambda: [None] * 13)
    dio_dir: list = field(default_factory=lambda: ["unused"] * 13)
    # the config's layout differs from the one the service started with
    restart_pending: bool = False
    # the scan-safe reading
    acq_id: int = 0
    acquiring: bool = False
    sample: dict = field(default_factory=dict)


def _finite(value, what: str) -> float:
    """float(value), refusing NaN and inf: NaN passes every `<`/`>` clamp and
    would be sent straight to the output."""
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return v


def layout_from(cfg: Config) -> Layout:
    """The part of the config that decides which tasks exist."""
    ai = tuple(i for i, ch in enumerate(cfg.ai.channels) if ch.enabled)
    dirs = tuple(d.direction for d in cfg.dio.lines)
    return Layout(ai=ai,
                  ai_terminal=tuple(cfg.ai.channels[i].terminal for i in ai),
                  di=tuple(i for i, d in enumerate(dirs) if d == "in"),
                  do=tuple(i for i, d in enumerate(dirs) if d == "out"),
                  directions=dirs)


class Daq:
    def __init__(self, backend: DaqBackend, cfg: Config | None = None,
                 clock=time.monotonic, config_path: str | None = None):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        # Where apply_config saves, so a layout change survives to the restart
        # that applies it (None: the local simulator GUI, nothing is saved).
        self.config_path = config_path
        self._hw = threading.RLock()       # serialises EVERY backend call
        self._lock = threading.Lock()      # guards the values + acquisition
        self._new_sample = threading.Condition(self._lock)

        self._startup_msgs = sanitise(self.cfg)
        self.layout = layout_from(self.cfg)   # replaced by the ACTIVE one at start()

        self._connected = False
        self._idn = ""
        self._hw_error = ""
        # values (under _lock)
        self._ai_V = [_NAN] * 8
        self._reads = 0
        self._read_ms = _NAN
        self._ao_V = [_NAN, _NAN]
        self._ao_known = [False, False]
        self._dio: list = [None] * 13
        # acquisition (under _lock)
        self._acq_id = 0
        self._acq: dict | None = None
        self._sample: dict = {}

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_event = lambda level, msg: None   # replaced by the service / GUI

    # ---- lifecycle ------------------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Create the tasks for the configured layout and ADOPT the card's state.

        Writes nothing to AO. Writes a digital line only if its config says
        initial = low/high. `poll=False` skips the thread (tests call
        poll_once() by hand).
        """
        for msg in self._startup_msgs:
            self._emit("warn", f"config: {msg}")
        self._startup_msgs = []
        self.layout = layout_from(self.cfg)
        lay = self.layout
        with self._hw:
            # open() either succeeds fully (tasks + claim held) or raises having
            # released both -- e.g. HardwareBusy when another service already
            # drives this card. Nothing below runs then, so we never write to
            # (or close) a card we do not own.
            self.backend.open(lay)
            try:
                self._idn = self.backend.idn()
                # Output lines: what are they driving now? A query only.
                try:
                    levels = self.backend.read_do() if lay.do else {}
                except Exception:
                    levels = {}
                initial = []
                for line in lay.do:
                    want = self.cfg.dio.lines[line].initial
                    if want in ("low", "high"):
                        self.backend.write_do(line, want == "high")
                        initial.append((line, want == "high"))
            except BaseException:
                self.backend.close()
                raise
            self._connected = True
        with self._lock:
            for line in lay.do:
                self._dio[line] = levels.get(line)
            for line, level in initial:
                self._dio[line] = level       # stored AFTER the write (gotcha #40)
        self._emit("info", f"connected: {self._idn or 'USB-6001'}")
        self._emit("info", "layout: AI " + (", ".join(AI_CHANNELS[i] + "/" + t for i, t in
                                                         zip(lay.ai, lay.ai_terminal)) or "none")
                   + "; inputs " + (", ".join(DIO_LINES[i] for i in lay.di) or "none")
                   + "; outputs " + (", ".join(DIO_LINES[i] for i in lay.do) or "none"))
        self._emit("info", "adopted: nothing written to AO (the 6001 cannot read AO back, "
                           "so AO shows 'unknown' until set)"
                   + ("; " + ", ".join(f"{DIO_LINES[l]} set {'high' if v else 'low'} "
                                       f"(initial)" for l, v in initial) if initial else ""))
        unknown = [DIO_LINES[l] for l in lay.do if self._dio[l] is None]
        if unknown:
            self._emit("warn", "output level unknown until first write: " + ", ".join(unknown))
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="usb6001-poll", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop polling, write any configured safe states, disconnect.
        Safe to call more than once, and after a failed start()."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
        self._thread = None
        was = self._connected
        try:
            if was:
                with self._hw:
                    for line in self.layout.do:
                        safe = self.cfg.dio.lines[line].safe_state
                        if safe in ("low", "high"):
                            try:
                                self.backend.write_do(line, safe == "high")
                                self._emit("info", f"{DIO_LINES[line]} -> {safe} (safe state)")
                            except Exception as exc:
                                self._emit("error", f"{DIO_LINES[line]}: safe state failed: {exc}")
        finally:
            try:
                with self._hw:
                    self.backend.close()
            finally:
                self._connected = False
                with self._new_sample:
                    self._acq = None
                    self._new_sample.notify_all()
                if was:
                    self._emit("info", "disconnected")

    # ---- outputs --------------------------------------------------------------------

    def ao_limits(self, channel: int) -> tuple[float, float]:
        ch = self.cfg.ao.channels[channel]
        return float(ch.min_V), float(ch.max_V)

    def ao_index(self, channel) -> int:
        """0, 1, 'ao0', 'AO1' or a configured name -> 0/1."""
        if isinstance(channel, bool):
            raise ValueError(f"not an AO channel: {channel!r}")
        if isinstance(channel, int):
            if channel in (0, 1):
                return channel
            raise ValueError(f"AO channel {channel} does not exist (0 or 1)")
        s = str(channel).strip()
        if s.lower() in AO_CHANNELS:
            return AO_CHANNELS.index(s.lower())
        if s.isdigit():
            return self.ao_index(int(s))
        for i, ch in enumerate(self.cfg.ao.channels):
            if ch.name and ch.name.strip().lower() == s.lower():
                return i
        raise ValueError(f"unknown AO channel {channel!r} (ao0, ao1 or a configured name)")

    def set_ao(self, channel, volts: float) -> float:
        """Drive an analog output; clamps to its limits. Returns what was written."""
        i = self.ao_index(channel)
        v = _finite(volts, "volts")
        lo, hi = self.ao_limits(i)
        value = min(max(v, lo), hi)
        if not self._connected:
            raise ValueError("not connected")
        with self._hw:
            self.backend.write_ao(i, value)
        with self._lock:                       # the echo only AFTER the write
            self._ao_V[i] = value
            self._ao_known[i] = True
        name = self.cfg.ao.channels[i].name or AO_CHANNELS[i]
        if value != v:
            self._emit("warn", f"{name}: {v:g} V clamped to {value:g} V "
                               f"(limit {lo:g}..{hi:g} V)")
        else:
            self._emit("info", f"{name} = {value:g} V")
        return value

    def set_do(self, line, state: bool) -> bool:
        """Drive a digital line that is configured (and started) as an OUTPUT."""
        i = self._line(line)
        if isinstance(state, str):
            state = state.strip().lower() in ("1", "true", "on", "high", "yes")
        level = bool(state)
        active = self.layout.directions[i] if self.layout.directions else "unused"
        if active != "out":
            raise ValueError(
                f"{DIO_LINES[i]} is configured as '{active}', not 'out': it cannot be "
                f"driven. Change its direction in Settings > DIO and restart the service.")
        if not self._connected:
            raise ValueError("not connected")
        with self._hw:
            self.backend.write_do(i, level)
        with self._lock:
            self._dio[i] = level
        self._emit("info", f"{self._line_name(i)} -> {'high' if level else 'low'}")
        return level

    # ---- fresh reads ------------------------------------------------------------------

    def acquire(self) -> int:
        """Start a scan-safe reading; returns its number at once. The poll
        thread latches the next reading that STARTED after this call."""
        if not self._connected:
            raise ValueError("not connected")
        with self._lock:
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = {"id": self._acq_id, "t0": self._clock()}
            return self._acq_id

    def fresh_sample(self, timeout_s: float | None = None) -> dict:
        """acquire() and wait for it: the reading behind read_ai / read_di."""
        acq = self.acquire()
        if self._thread is None:
            self.poll_once()                   # single-threaded use (tests, scripts)
        timeout = timeout_s if timeout_s is not None else self.acquire_timeout_s()
        deadline = self._clock() + timeout
        with self._new_sample:
            while self._sample.get("acq_id", 0) < acq:
                left = deadline - self._clock()
                if left <= 0 or not self._connected:
                    raise TimeoutError(f"no fresh reading within {timeout:g} s"
                                       + (f" ({self._hw_error})" if self._hw_error else ""))
                self._new_sample.wait(min(left, 0.1))
            return copy.deepcopy(self._sample)

    def read_ai(self, channel=None) -> dict:
        """A FRESH averaged reading of one enabled input (or all of them).
        {"acq_id", "values_V": {ai: V}, "values": {ai: scaled}, "units": {ai: unit}}"""
        idx = None if channel in (None, "") else self._ai_index(channel)
        if idx is not None and idx not in self.layout.ai:
            raise ValueError(f"{AI_CHANNELS[idx]} is not enabled (Settings > AI, then restart)")
        s = self.fresh_sample()
        chans = [idx] if idx is not None else list(self.layout.ai)
        return {"acq_id": s["acq_id"],
                "values_V": {AI_CHANNELS[i]: s["ai_V"][i] for i in chans},
                "values": {AI_CHANNELS[i]: s["ai"][i] for i in chans},
                "units": {AI_CHANNELS[i]: self.cfg.ai.channels[i].unit for i in chans}}

    def read_di(self, line=None) -> dict:
        """A FRESH reading of one input line (or all of them): {"acq_id", "levels": {p0.0: bool}}."""
        idx = None if line in (None, "") else self._line(line)
        if idx is not None and idx not in self.layout.di:
            raise ValueError(f"{DIO_LINES[idx]} is not configured as an input")
        s = self.fresh_sample()
        lines = [idx] if idx is not None else list(self.layout.di)
        return {"acq_id": s["acq_id"],
                "levels": {DIO_LINES[i]: s["dio"][i] for i in lines}}

    def get_sample(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._sample)

    def acquire_timeout_s(self) -> float:
        """Generous: one poll period + one reading, times a margin, >= 5 s."""
        a = self.cfg.ai
        read = a.samples_per_read / max(a.rate_Hz, 1e-9)
        period = 1.0 / max(float(self.cfg.hardware.poll_hz), 0.1)
        return max(5.0, 3.0 * (read + period) + float(self.cfg.hardware.timeout_s))

    # ---- status -------------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        chans = self.cfg.ai.channels
        with self._lock:
            a = self._acq
            ai_V = list(self._ai_V)
            return Status(
                connected=self._connected, idn=self._idn, hw_error=self._hw_error,
                ai_V=ai_V,
                ai=[v * chans[i].slope + chans[i].offset for i, v in enumerate(ai_V)],
                ai_enabled=[i in self.layout.ai for i in range(8)],
                reads=self._reads, read_ms=self._read_ms,
                ao_V=list(self._ao_V), ao_known=list(self._ao_known),
                dio=list(self._dio),
                dio_dir=list(self.layout.directions) or ["unused"] * 13,
                restart_pending=self.restart_pending(),
                acq_id=self._acq_id, acquiring=a is not None,
                sample=copy.deepcopy(self._sample),
            )

    def restart_pending(self) -> bool:
        """True when the config asks for a layout the running tasks do not have."""
        return layout_from(self.cfg) != self.layout

    # ---- config ----------------------------------------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Called after set_config / the Settings dialog edited self.cfg in place.

        Names, scales, AO limits, rate and samples apply at once. The layout
        (AI enabled/terminal, line directions) is SAVED and applied at the next
        service start. A known AO value outside new limits is re-clamped (a
        write the user caused by narrowing the limits).
        """
        for msg in sanitise(self.cfg):
            self._emit("warn", f"config: {msg}")
        for i in (0, 1):
            with self._lock:
                known, v = self._ao_known[i], self._ao_V[i]
            lo, hi = self.ao_limits(i)
            if known and self._connected and not lo <= v <= hi:
                self.set_ao(i, v)
        if self.config_path:
            try:
                self.cfg.save(self.config_path)
            except OSError as exc:
                self._emit("error", f"could not save {self.config_path}: {exc}")
        if self.restart_pending():
            self._emit("warn", "AI channels / terminals / line directions changed: "
                               "saved; they apply after a service RESTART")
        else:
            self._emit("info", "settings applied")

    def save_config(self, path: str | None = None) -> str:
        p = path or self.config_path
        if not p:
            raise ValueError("no config file: start the service with --config or give a path")
        self.cfg.save(p)
        return p

    # ---- polling --------------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            t = self._clock()
            self.poll_once()
            period = 1.0 / max(float(self.cfg.hardware.poll_hz), 0.1)
            # time.sleep, not self._stop.wait: on Windows a timed Event.wait
            # rounds up to the 15.6 ms timer tick (gotcha #34). A pending
            # acquisition skips the wait: a scan should not idle for a period.
            end = t + period
            while not self._stop.is_set() and self._clock() < end:
                with self._lock:
                    if self._acq is not None:
                        break
                time.sleep(0.005)

    def poll_once(self) -> None:
        """One reading of every enabled AI and every input line, then advance
        any acquisition. Public so tests and single-threaded scripts can drive it."""
        lay = self.layout
        a = self.cfg.ai
        try:
            with self._hw:
                t_start = self._clock()
                volts = self.backend.read_ai(a.samples_per_read, a.rate_Hz) if lay.ai else []
                di = self.backend.read_di() if lay.di else {}
                t_end = self._clock()
        except Exception as exc:              # never let the poll thread die
            self._report_hw_error(exc)
            return
        chans = self.cfg.ai.channels
        recovered = False
        with self._new_sample:
            recovered = bool(self._hw_error)
            self._hw_error = ""
            for i, v in zip(lay.ai, volts):
                self._ai_V[i] = float(v)
            for line, level in di.items():
                self._dio[line] = bool(level)
            self._reads += 1
            self._read_ms = (t_end - t_start) * 1e3
            acq = self._acq
            if acq is not None and t_start >= acq["t0"]:
                # Busy cleared and result published in ONE critical section
                # (gotcha #28): no frame can say "done" with the old sample.
                ai_V = [(self._ai_V[i] if i in lay.ai else _NAN) for i in range(8)]
                self._sample = {
                    "acq_id": acq["id"], "time": time.time(),
                    "ai_V": ai_V,
                    "ai": [v * chans[i].slope + chans[i].offset for i, v in enumerate(ai_V)],
                    "dio": list(self._dio),
                    "samples": int(a.samples_per_read), "rate_Hz": float(a.rate_Hz),
                }
                self._acq = None
                self._new_sample.notify_all()
        if recovered:
            self._emit("info", "hardware reads recovered")

    def _report_hw_error(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        with self._lock:
            first = not self._hw_error
            self._hw_error = msg
        if first:                               # one event per episode, not per poll
            self._emit("error", f"hardware read failed: {msg} (last good values kept)")

    # ---- internals --------------------------------------------------------------------------

    def _line(self, line) -> int:
        try:
            return line_index(line)
        except ValueError:
            s = str(line).strip().lower()
            for i, d in enumerate(self.cfg.dio.lines):
                if d.name and d.name.strip().lower() == s:
                    return i
            raise

    def _line_name(self, i: int) -> str:
        name = self.cfg.dio.lines[i].name
        return f"{DIO_LINES[i]} ({name})" if name and name.upper() != DIO_LINES[i].upper() else DIO_LINES[i]

    def _ai_index(self, channel) -> int:
        if isinstance(channel, int) and not isinstance(channel, bool):
            if 0 <= channel < 8:
                return channel
            raise ValueError(f"AI channel {channel} does not exist (0..7)")
        s = str(channel).strip().lower()
        if s in AI_CHANNELS:
            return AI_CHANNELS.index(s)
        if s.isdigit():
            return self._ai_index(int(s))
        for i, ch in enumerate(self.cfg.ai.channels):
            if ch.name and ch.name.strip().lower() == s:
                return i
        raise ValueError(f"unknown AI channel {channel!r} (ai0..ai7 or a configured name)")

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
