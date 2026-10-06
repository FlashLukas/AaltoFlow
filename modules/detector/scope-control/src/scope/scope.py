"""The Scope: the brain between the wire and the backend (simulated or real).

WHAT IT DOES WITH EVERY TRIGGERED RECORD
  the backend's record (volts at the probe tip, thousands of points)
    -> physical units per channel (phys_scale * V + phys_offset)
    -> reduced to `acquisition.points` samples (neighbours averaged)
    -> into the RUNNING AVERAGE: the mean of the last `averages` records
       ("312 / 500" in the GUI; "Restart average" empties it)
    -> filtered (zero-phase, identical on all channels) for display
    -> per-channel numbers and the loop numbers, live.

THE SCAN-SAFE READ: `acquire()` returns an id at once. The trace thread then
averages the next `averages` FRESH records -- the first record that becomes
ready after the trigger is skipped, because it may have been captured just
before the trigger (the scope's "new data" flag says only that a record was
finished, not when it began) -- and latches the result as THE sample:
filtered (and, with keep_raw, unfiltered) traces, the numbers, the loop.
Callers wait until status shows `acq_id` == their id AND `acquiring` False.
A change of any setting that shapes a record (V/div, offset, time/div,
trigger, points, units) RESTARTS a running acquisition and empties the
running average: averaging records taken under two settings is not a
measurement. A filter or analysis change does not: those are applied to the
average at the end.

THE SCOPE'S OWN SETTINGS are adopted at start (nothing written) and written
only when someone changes them; the trace thread then reads them back --
the scope snaps V/div and time/div to its own steps, and the status shows
what it really holds next to what was asked. A change made at the scope's
front panel is noticed (the settings are re-read every few seconds) and
adopted, and it restarts the average like any other change.

Threads and locks, the ccs200 / pm16 rules:
  * ONE trace thread talks to the backend; every backend call under `_hw`.
  * `status()` never touches the hardware.
  * the sample and "acquiring = False" are published in ONE critical section
    (gotcha #28); acquisitions are numbered (gotcha #17).
  * time.sleep, not Event.wait (gotcha #34).
"""

from __future__ import annotations

import collections
import math
import threading
import time

import numpy as np

from . import analysis
from .config import (Config, CHANNEL_NAMES, COUPLINGS, TRIGGER_SOURCES, TRIGGER_SLOPES,
                     TRIGGER_MODES)

_NAN = float("nan")
_SETTINGS_REREAD_S = 3.0            # how often the scope's settings are re-read

#: limits for the module's own numbers (sanity, not physics)
POINTS_RANGE = (16, 100_000)
AVERAGES_RANGE = (1, 100_000)


def _finite(v, what):
    v = float(v)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {v!r}")
    return v


def parse_channel(ch) -> str:
    key = str(ch).strip().lower()
    if key in ("1", "2"):
        key = "ch" + key
    if key not in CHANNEL_NAMES:
        raise ValueError(f"unknown channel {ch!r} (use 'ch1' or 'ch2')")
    return key


class Scope:
    def __init__(self, backend, cfg: Config | None = None, clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self.caps = dict(backend.capabilities())
        self._hw = threading.RLock()        # serialises every backend call
        self._lock = threading.RLock()      # guards everything below

        self._connected = False
        self._idn = ""
        self._hw_error = ""
        self._last_err_emit = -1e9
        self._actual: dict = {}             # the scope's settings as last read
        self._requested: dict = {}          # what was last ASKED, per setting key
        self._pending: list = []            # setting writes waiting for the thread
        self._settings_gen = 0              # bumped per request; done when pushed + read
        self._settings_done = 0
        self._rev = 0                       # bumped by any change that shapes a record
        self._records = 0                   # triggered records seen since start
        self._trigger_times = collections.deque(maxlen=20)
        self._running = collections.deque() # the running average: (t, {ch: y})
        self._live_t = np.zeros(0)
        self._acq_id = 0
        self._acq: dict | None = None
        self._sample: dict = {}
        self._sample_trace: dict | None = None
        self._live_numbers: dict = {}
        self._last_reread = -1e9

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_event = lambda level, msg: None

    @property
    def simulated(self) -> bool:
        return bool(getattr(self.backend, "simulated", True))

    @property
    def channels(self) -> tuple:
        return tuple(c for c in self.caps.get("channels", CHANNEL_NAMES) if c in CHANNEL_NAMES)

    # ---- lifecycle -----------------------------------------------------------------

    def start(self, run: bool = True) -> None:
        """Open the scope, READ its settings and adopt them, start the thread.
        `run=False` skips the thread so a test can call step() by hand."""
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            got = self.backend.read_settings()
        self._connected = True
        self._emit("info", f"connected: {self._idn or 'scope'}  (settings read, nothing changed)")
        self._adopt(got, at_start=True)
        if run:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="scope-traces",
                                            daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop reading and disconnect. A scope drives nothing: stopping is enough."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
        self._thread = None
        was = self._connected
        try:
            with self._hw:
                self.backend.close()
        finally:
            self._connected = False
            with self._lock:
                self._acq = None
            if was:
                self._emit("info", "disconnected")

    def _adopt(self, got: dict, at_start: bool = False) -> list:
        """Copy the scope's settings into cfg (and `_actual`). Returns the names
        that CHANGED compared with what we knew (empty at start)."""
        changed = []
        unread = list(got.get("unread") or [])
        with self._lock:
            old = dict(self._actual)
            flat = {}
            for ch, vals in (got.get("channels") or {}).items():
                for k, v in vals.items():
                    if v is not None:
                        flat[f"{ch}_{k}"] = v
            for k in ("tdiv_s", "delay_s", "sample_rate_Hz"):
                if got.get(k) is not None:
                    flat[k] = got[k]
            for k, v in (got.get("trigger") or {}).items():
                if v is not None:
                    flat[f"trigger_{k}"] = v
            for key, v in flat.items():
                if key in old and old[key] != v and not at_start:
                    changed.append(key)
                self._actual[key] = v
            # into cfg, so get_config / a saved .ini / the Settings dialog tell the truth
            for ch in CHANNEL_NAMES:
                c = self.cfg.channel(ch)
                for k in ("enabled", "vdiv_V", "offset_V", "coupling", "probe"):
                    if f"{ch}_{k}" in flat:
                        setattr(c, k, flat[f"{ch}_{k}"])
            for k in ("tdiv_s", "delay_s"):
                if k in flat:
                    setattr(self.cfg.timebase, k, flat[k])
            for k in ("source", "level_V", "slope", "mode"):
                if f"trigger_{k}" in flat:
                    setattr(self.cfg.trigger, k, flat[f"trigger_{k}"])
            if at_start:
                for key, v in flat.items():
                    self._requested.setdefault(key, v)
        if unread:
            self._emit("warn", "could not read " + ", ".join(unread)
                       + " -- showing the config value there (NOT written)")
        if at_start:
            t = self.cfg.trigger
            self._emit("info", f"found: {self.cfg.timebase.tdiv_s:g} s/div, trigger "
                               f"{t.source} {t.slope} at {t.level_V:g} V, mode {t.mode}")
            if t.mode == "stop":
                self._emit("warn", "the scope is STOPPED: no new traces until it runs "
                                   "(trigger mode auto/normal, or RUN on the panel)")
        return changed

    # ---- the scope's own settings (queued for the trace thread) ----------------------

    def _queue(self, kind: str, what: str, **values) -> None:
        with self._lock:
            self._settings_gen += 1
            self._pending.append((self._settings_gen, kind, values))
            prefix = {"timebase": "", "trigger": "trigger_"}.get(kind, f"{kind}_")
            for k, v in values.items():
                self._requested[f"{prefix}{k}"] = v
        self._restart(f"{what}")

    def set_channel_enabled(self, ch, on: bool) -> None:
        ch = parse_channel(ch)
        self._queue(ch, f"{ch.upper()} {'on' if on else 'off'}", enabled=bool(on))

    def set_vdiv(self, ch, volts: float) -> None:
        ch = parse_channel(ch)
        v = _finite(volts, "V/div")
        if v <= 0:
            raise ValueError("V/div must be positive")
        self._queue(ch, f"{ch.upper()} {v:g} V/div", vdiv_V=v)

    def set_offset(self, ch, volts: float) -> None:
        ch = parse_channel(ch)
        self._queue(ch, f"{ch.upper()} offset {volts:g} V", offset_V=_finite(volts, "offset"))

    def set_coupling(self, ch, coupling: str) -> None:
        ch = parse_channel(ch)
        if coupling not in COUPLINGS:
            raise ValueError(f"coupling must be one of {', '.join(COUPLINGS)}")
        self._queue(ch, f"{ch.upper()} coupling {coupling}", coupling=coupling)

    def set_probe(self, ch, factor: float) -> None:
        ch = parse_channel(ch)
        f = _finite(factor, "probe factor")
        if f <= 0:
            raise ValueError("probe factor must be positive")
        self._queue(ch, f"{ch.upper()} probe x{f:g}", probe=f)

    def set_tdiv(self, seconds: float) -> None:
        s = _finite(seconds, "time/div")
        if s <= 0:
            raise ValueError("time/div must be positive")
        self._queue("timebase", f"{s:g} s/div", tdiv_s=s)

    def set_delay(self, seconds: float) -> None:
        self._queue("timebase", f"trigger delay {seconds:g} s",
                    delay_s=_finite(seconds, "delay"))

    def set_trigger_source(self, source: str) -> None:
        if source not in TRIGGER_SOURCES:
            raise ValueError(f"trigger source must be one of {', '.join(TRIGGER_SOURCES)}")
        self._queue("trigger", f"trigger source {source}", source=source)

    def set_trigger_level(self, volts: float) -> None:
        self._queue("trigger", f"trigger level {volts:g} V",
                    level_V=_finite(volts, "trigger level"))

    def set_trigger_slope(self, slope: str) -> None:
        if slope not in TRIGGER_SLOPES:
            raise ValueError(f"slope must be one of {', '.join(TRIGGER_SLOPES)}")
        self._queue("trigger", f"trigger slope {slope}", slope=slope)

    def set_trigger_mode(self, mode: str) -> None:
        if mode not in TRIGGER_MODES:
            raise ValueError(f"trigger mode must be one of {', '.join(TRIGGER_MODES)}")
        self._queue("trigger", f"trigger mode {mode}", mode=mode)

    # ---- the module's own settings (immediate) ----------------------------------------

    def set_points(self, n: int) -> None:
        n = int(round(_finite(n, "points")))
        n = min(max(n, POINTS_RANGE[0]), POINTS_RANGE[1])
        self.cfg.acquisition.points = n
        self._restart(f"{n} points per trace")

    def set_averages(self, n: int) -> None:
        n = int(round(_finite(n, "averages")))
        n = min(max(n, AVERAGES_RANGE[0]), AVERAGES_RANGE[1])
        self.cfg.acquisition.averages = n
        with self._lock:
            while len(self._running) > n:
                self._running.popleft()
            if self._acq is not None:
                self._acq["want"] = n
        self._emit("info", f"{n} traces averaged")

    def set_keep_raw(self, on: bool) -> None:
        self.cfg.acquisition.keep_raw = bool(on)
        self._emit("info", "raw traces recorded too" if on else "only filtered traces recorded")

    def set_filter(self, lowpass_Hz: float | None = None, highpass_Hz: float | None = None,
                   order: int | None = None) -> None:
        f = self.cfg.filter
        if lowpass_Hz is not None:
            f.lowpass_Hz = max(0.0, _finite(lowpass_Hz, "low-pass"))
        if highpass_Hz is not None:
            f.highpass_Hz = max(0.0, _finite(highpass_Hz, "high-pass"))
        if order is not None:
            f.order = min(max(int(order), 1), 8)
        self._emit("info", f"filter: low-pass {f.lowpass_Hz:g} Hz, high-pass "
                           f"{f.highpass_Hz:g} Hz, order {f.order} (0 = off; zero phase)")

    def set_physical(self, ch, scale: float | None = None, offset: float | None = None,
                     unit: str | None = None, label: str | None = None) -> None:
        """What channel `ch` measures: quantity = scale * volts + offset."""
        ch = parse_channel(ch)
        c = self.cfg.channel(ch)
        if scale is not None:
            s = _finite(scale, "scale")
            if s == 0:
                raise ValueError("scale must not be 0")
            c.phys_scale = s
        if offset is not None:
            c.phys_offset = _finite(offset, "offset")
        if unit is not None:
            c.phys_unit = str(unit)
        if label is not None:
            c.phys_label = str(label)
        self._restart(f"{ch.upper()} in {c.phys_unit}: {c.phys_scale:g} {c.phys_unit}/V "
                      f"{c.phys_offset:+g} {c.phys_unit}")

    def set_loop(self, x: str | None = None, y: str | None = None) -> None:
        a = self.cfg.analysis
        if x is not None:
            a.loop_x = parse_channel(x)
        if y is not None:
            a.loop_y = parse_channel(y)
        self._emit("info", f"loop: {a.loop_y.upper()} against {a.loop_x.upper()}")

    def set_analysis(self, sat_fraction: float | None = None,
                     subtract_background: bool | None = None,
                     normalise: bool | None = None) -> None:
        a = self.cfg.analysis
        if sat_fraction is not None:
            a.sat_fraction = min(max(_finite(sat_fraction, "fraction"), 0.3), 0.98)
        if subtract_background is not None:
            a.subtract_background = bool(subtract_background)
        if normalise is not None:
            a.normalise = bool(normalise)
        self._emit("info", f"loop analysis: ends above {a.sat_fraction:g} of max |X|, "
                           f"background {'subtracted' if a.subtract_background else 'kept'}")

    def set_sim(self, name: str, value) -> None:
        """SIMULATOR only: change the pretend bench (e.g. the coercive field)."""
        if not self.simulated:
            raise ValueError("only the simulator has a pretend bench")
        if not hasattr(self.cfg.sim, name):
            raise ValueError(f"unknown sim parameter {name!r}")
        cur = getattr(self.cfg.sim, name)
        if isinstance(cur, str):
            if name == "scene" and value not in ("moke", "bench"):
                raise ValueError("scene must be 'moke' or 'bench'")
            setattr(self.cfg.sim, name, str(value))
        else:
            setattr(self.cfg.sim, name, _finite(value, name))
        self._emit("info", f"sim {name} = {value}")

    def restart_average(self) -> None:
        self._restart("average restarted")

    # ---- the scan-safe read ----------------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its id at once (see module docstring).
        Refused when the scope cannot deliver: not connected, STOPPED, or in
        SINGLE mode with more than one trace to average."""
        if not self._connected:
            raise ValueError("not connected")
        mode = self.cfg.trigger.mode
        if mode == "stop":
            raise ValueError("the scope is stopped: no new traces will come "
                             "(set the trigger mode to normal or auto)")
        if mode == "single" and int(self.cfg.acquisition.averages) > 1:
            raise ValueError("single-shot mode gives ONE trace, the acquisition wants "
                             f"{self.cfg.acquisition.averages}: use normal mode")
        with self._lock:
            self._acq_id += 1
            self._acq = self._new_acq(self._acq_id)
            return self._acq_id

    def abort(self) -> None:
        """Cancel a running acquisition; latched as ABORTED (a caller waiting
        for "acq_id == n and not acquiring" must not read the previous sample)."""
        with self._lock:
            a = self._acq
            if a is None:
                return
            self._acq = None
            self._sample = {"acq_id": a["id"], "aborted": True, "time": time.time()}
            self._sample_trace = None
        self._emit("warn", f"acquisition #{a['id']} aborted")

    def _new_acq(self, acq_id: int) -> dict:
        return {"id": acq_id, "t0": self._clock(), "want": max(1, int(self.cfg.acquisition.averages)),
                "n": 0, "skip": 1, "sum": None, "t": None, "clipped": set()}

    def get_trace(self, which: str = "live") -> dict:
        """Traces and what they were measured under.

        which "live" (the running average now) or "sample" (the latched
        acquisition). Arrays: "time_s", "<ch>" (filtered), "<ch>_raw"
        (unfiltered average), in the channel's physical unit."""
        with self._lock:
            if which == "sample":
                if self._sample.get("aborted"):
                    raise ValueError(f"acquisition #{self._sample['acq_id']} was aborted")
                if self._sample.get("error"):
                    raise ValueError(f"acquisition #{self._sample['acq_id']} failed: "
                                     f"{self._sample['error']}")
                if self._sample_trace is None:
                    raise ValueError("no acquisition latched yet")
                return {k: (v.copy() if isinstance(v, np.ndarray) else v)
                        for k, v in self._sample_trace.items()}
            if which != "live":
                raise ValueError(f"which must be 'live' or 'sample', got {which!r}")
            if not self._running:
                raise ValueError("no trace yet (is the scope triggering?)")
            t = self._live_t.copy()
            recs = [r for _, r in self._running]
            n = len(recs)
        chans = list(recs[-1].keys())
        raw = {ch: np.mean([r[ch] for r in recs], axis=0) for ch in chans}
        return self._process(t, raw, n)

    def get_time(self) -> list:
        """The time axis of the latched sample (or of the live average)."""
        with self._lock:
            if self._sample_trace is not None:
                return list(self._sample_trace["time_s"])
            return list(self._live_t)

    def _process(self, t: np.ndarray, raw: dict, n: int) -> dict:
        """Filter, numbers and loop for an averaged record (pure; no lock)."""
        f = self.cfg.filter
        dt = float(t[1] - t[0]) if t.size > 1 else 0.0
        filt = {ch: analysis.zero_phase(y, dt, f.lowpass_Hz, f.highpass_Hz, f.order)
                for ch, y in raw.items()}
        out = {"time_s": t, "averages": n, "lowpass_Hz": f.lowpass_Hz,
               "highpass_Hz": f.highpass_Hz, "filter_order": f.order}
        for ch, y in filt.items():
            c = self.cfg.channel(ch)
            out[ch] = y
            out[f"{ch}_raw"] = raw[ch]
            out[f"{ch}_values"] = analysis.channel_values(t, y)
            out[f"{ch}_unit"] = c.phys_unit
        if "ch1" in filt and "ch2" in filt:
            out["phase_21_deg"] = analysis.phase_deg(t, filt["ch1"], filt["ch2"])
        else:
            out["phase_21_deg"] = _NAN
        a = self.cfg.analysis
        if a.loop_x in filt and a.loop_y in filt and a.loop_x != a.loop_y:
            loop = analysis.loop_numbers(filt[a.loop_x], filt[a.loop_y],
                                         a.sat_fraction, a.subtract_background)
            out["loop_y"] = loop.pop("y_corrected")
            out["loop"] = loop
            if a.normalise:
                out["loop_y_norm"] = analysis.normalised(out["loop_y"], loop["ms"], loop["mid"])
        else:
            out["loop"] = {k: _NAN for k in analysis.LOOP_KEYS}
        return out

    # ---- status ------------------------------------------------------------------------

    def status(self) -> dict:
        """A flat snapshot (plus `sample` and `live` blocks). No hardware."""
        c = self.cfg
        with self._lock:
            a = self._acq
            st = {"connected": self._connected, "idn": self._idn, "hw_error": self._hw_error,
                  "simulated": self.simulated, "model": self.caps.get("model", ""),
                  "channels": list(self.channels),
                  "generator_channels": int(self.caps.get("generator_channels", 0)),
                  "records": self._records,
                  "trigger_rate_Hz": self._trigger_rate_locked(),
                  "running_n": len(self._running),
                  "averages": int(c.acquisition.averages),
                  "points": int(c.acquisition.points),
                  "keep_raw": bool(c.acquisition.keep_raw),
                  "lowpass_Hz": c.filter.lowpass_Hz, "highpass_Hz": c.filter.highpass_Hz,
                  "filter_order": int(c.filter.order),
                  "loop_x": c.analysis.loop_x, "loop_y": c.analysis.loop_y,
                  "sat_fraction": c.analysis.sat_fraction,
                  "subtract_background": bool(c.analysis.subtract_background),
                  "settings_settled": (self._connected and not self._hw_error
                                       and self._settings_done == self._settings_gen),
                  "acq_id": self._acq_id, "acquiring": a is not None,
                  "acq_progress": (a["n"] / a["want"]) if a else 0.0,
                  "sample": dict(self._sample),
                  "live": dict(self._live_numbers)}
            for ch in CHANNEL_NAMES:
                ch_cfg = c.channel(ch)
                for k in ("enabled", "vdiv_V", "offset_V", "coupling", "probe"):
                    st[f"{ch}_{k}"] = self._actual.get(f"{ch}_{k}", getattr(ch_cfg, k))
                    st[f"{ch}_{k}_set"] = self._requested.get(f"{ch}_{k}", getattr(ch_cfg, k))
                st[f"{ch}_unit"] = ch_cfg.phys_unit
                st[f"{ch}_label"] = ch_cfg.phys_label or ch.upper()
                st[f"{ch}_phys_scale"] = ch_cfg.phys_scale
                st[f"{ch}_phys_offset"] = ch_cfg.phys_offset
            for k in ("tdiv_s", "delay_s"):
                st[k] = self._actual.get(k, getattr(c.timebase, k))
                st[f"{k}_set"] = self._requested.get(k, getattr(c.timebase, k))
            st["sample_rate_Hz"] = self._actual.get("sample_rate_Hz", _NAN)
            if self.simulated:                  # the pretend bench's knobs
                for k in ("hc_mT", "ms_V", "drive_Hz", "noise_V"):
                    st[f"sim_{k}"] = getattr(c.sim, k)
            for k in ("source", "level_V", "slope", "mode"):
                st[f"trigger_{k}"] = self._actual.get(f"trigger_{k}", getattr(c.trigger, k))
                st[f"trigger_{k}_set"] = self._requested.get(f"trigger_{k}",
                                                             getattr(c.trigger, k))
        return st

    def _trigger_rate_locked(self) -> float:
        ts = self._trigger_times
        if len(ts) < 2 or ts[-1] - ts[0] <= 0:
            return _NAN
        return (len(ts) - 1) / (ts[-1] - ts[0])

    # ---- config ------------------------------------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """cfg was edited in place (Settings dialog / set_config). The SCOPE's
        settings that now differ from what it holds are queued (the only way
        a config value reaches the scope); the module's own take effect at once
        and restart the average."""
        c = self.cfg
        with self._lock:
            actual = dict(self._actual)
        for ch in self.channels:
            cc = c.channel(ch)
            diff = {k: getattr(cc, k) for k in ("enabled", "vdiv_V", "offset_V", "coupling", "probe")
                    if f"{ch}_{k}" in actual and actual[f"{ch}_{k}"] != getattr(cc, k)}
            if diff:
                self._queue(ch, f"{ch.upper()} settings", **diff)
        tb = {k: getattr(c.timebase, k) for k in ("tdiv_s", "delay_s")
              if k in actual and actual[k] != getattr(c.timebase, k)}
        if tb:
            self._queue("timebase", "timebase", **tb)
        tr = {k: getattr(c.trigger, k) for k in ("source", "level_V", "slope", "mode")
              if f"trigger_{k}" in actual and actual[f"trigger_{k}"] != getattr(c.trigger, k)}
        if tr:
            self._queue("trigger", "trigger", **tr)
        c.acquisition.points = int(min(max(int(c.acquisition.points), POINTS_RANGE[0]),
                                       POINTS_RANGE[1]))
        c.acquisition.averages = int(min(max(int(c.acquisition.averages), 1),
                                         AVERAGES_RANGE[1]))
        self._restart("settings applied")

    # ---- the trace thread ----------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                got = self.step()
            except Exception as exc:              # never let the thread die
                self._report_hw_error(exc)
                time.sleep(0.5)
                continue
            if not got:
                time.sleep(max(0.001, float(self.cfg.hardware.poll_s)))

    def step(self) -> bool:
        """One pass: push queued settings, re-read the settings now and then,
        take a new record if the scope has one. True if a record was taken.
        Public so tests can drive it by hand."""
        self._push_settings()
        now = self._clock()
        if now - self._last_reread >= _SETTINGS_REREAD_S:
            self._last_reread = now
            with self._hw:
                got = self.backend.read_settings()
            changed = self._adopt(got)
            if changed:
                self._emit("info", "changed at the scope: " + ", ".join(changed))
                self._restart(None)
        with self._hw:
            ready = self.backend.new_trace_ready()
        if not ready:
            return False
        with self._lock:
            rev0 = self._rev
            chans = [ch for ch in self.channels if self._actual.get(f"{ch}_enabled", True)]
        if not chans:
            return False
        with self._hw:
            t, volts = self.backend.read_traces(chans, int(self.cfg.hardware.max_points))
        self._take(t, volts, rev0, now)
        if self._hw_error:
            with self._lock:
                self._hw_error = ""
            self._emit("info", "scope answering again")
        return True

    def _push_settings(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
        if not pending:
            return
        with self._hw:
            for _, kind, values in pending:
                if kind in CHANNEL_NAMES:
                    self.backend.set_channel(kind, **values)
                elif kind == "timebase":
                    self.backend.set_timebase(**values)
                else:
                    self.backend.set_trigger(**values)
            got = self.backend.read_settings()
        self._adopt(got)
        with self._lock:
            self._settings_done = pending[-1][0]
            self._last_reread = self._clock()
        self._restart(None)

    def _take(self, t, volts: dict, rev0: int, now: float) -> None:
        """One triggered record into the running average and the acquisition."""
        c = self.cfg
        phys = {}
        clipped = set()
        for ch, v in volts.items():
            cc = c.channel(ch)
            v = np.asarray(v, dtype=float)
            # the 8-bit ADC reports the screen edge for anything beyond it
            vdiv, off = float(self._actual.get(f"{ch}_vdiv_V", cc.vdiv_V)), \
                float(self._actual.get(f"{ch}_offset_V", cc.offset_V))
            lo, hi = -4 * vdiv - off, 4 * vdiv - off
            if np.any(v >= hi - 1e-9 * abs(hi)) or np.any(v <= lo + 1e-9 * abs(lo)):
                clipped.add(ch)
            phys[ch] = cc.phys_scale * v + cc.phys_offset
        tr, red = analysis.reduce_points(np.asarray(t, float), phys, int(c.acquisition.points))
        latched = None
        with self._lock:
            if self._rev != rev0:
                return                          # settings changed during the read
            self._records += 1
            self._trigger_times.append(now)
            if self._live_t.size != tr.size or not np.allclose(self._live_t, tr):
                self._running.clear()           # a new time axis: start again
                self._live_t = tr
            self._running.append((now, red))
            while len(self._running) > max(1, int(c.acquisition.averages)):
                self._running.popleft()
            a = self._acq
            if a is not None and now >= a["t0"]:
                if a["skip"] > 0:
                    a["skip"] -= 1              # may have begun before the trigger
                else:
                    if a["sum"] is None or a["t"].size != tr.size:
                        a["sum"] = {ch: y.copy() for ch, y in red.items()}
                        a["t"] = tr
                        a["n"] = 1
                    else:
                        for ch, y in red.items():
                            a["sum"][ch] = a["sum"][ch] + y
                        a["n"] += 1
                    a["clipped"] |= clipped
                    if a["n"] >= a["want"]:
                        mean = {ch: s / a["n"] for ch, s in a["sum"].items()}
                        proc = self._process(a["t"], mean, a["n"])
                        proc["acq_id"] = a["id"]
                        sample = {"acq_id": a["id"], "time": time.time(), "averages": a["n"],
                                  "clipped": sorted(a["clipped"]),
                                  "phase_21_deg": proc["phase_21_deg"],
                                  "loop": dict(proc["loop"]),
                                  "lowpass_Hz": proc["lowpass_Hz"],
                                  "highpass_Hz": proc["highpass_Hz"]}
                        for ch in mean:
                            sample[ch] = dict(proc[f"{ch}_values"])
                        # sample + "acquiring = False" in ONE critical section (gotcha #28)
                        self._sample = sample
                        self._sample_trace = proc
                        self._acq = None
                        latched = sample
            recs = [r for _, r in self._running]
            n_live = len(recs)
        # live numbers (outside the lock: a few ms of numpy)
        mean = {ch: np.mean([r[ch] for r in recs], axis=0) for ch in red}
        proc = self._process(tr, mean, n_live)
        live = {"phase_21_deg": proc["phase_21_deg"], "loop": dict(proc["loop"]),
                "clipped": sorted(clipped), "n": n_live}
        for ch in red:
            live[ch] = dict(proc[f"{ch}_values"])
        with self._lock:
            self._live_numbers = live
        if latched is not None and latched["clipped"]:
            self._emit("warn", f"acquisition #{latched['acq_id']}: "
                               f"{', '.join(c.upper() for c in latched['clipped'])} CLIPPED "
                               f"at the screen edge -- raise V/div")

    def _restart(self, msg: str | None) -> None:
        """A record-shaping change: empty the running average and restart a
        running acquisition from scratch."""
        restarted = None
        with self._lock:
            self._rev += 1
            self._running.clear()
            if self._acq is not None:
                restarted = self._acq["id"]
                self._acq = self._new_acq(restarted)
        if msg:
            self._emit("info", msg)
        if restarted is not None:
            self._emit("warn", f"acquisition #{restarted} restarted: settings changed")

    def _report_hw_error(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._hw_error = msg
            # an acquisition cannot complete through a dead link: fail it loudly
            a = self._acq
            if a is not None:
                self._acq = None
                self._sample = {"acq_id": a["id"], "error": msg, "time": time.time()}
                self._sample_trace = None
        now = self._clock()
        if now - self._last_err_emit >= 5.0:
            self._last_err_emit = now
            self._emit("error", f"scope: {msg}")

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
