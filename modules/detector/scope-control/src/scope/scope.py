"""The Scope: the brain between the wire and the backend (simulated or real).

WHAT IT DOES WITH EVERY TRIGGERED RECORD
  the backend's record (volts at the probe tip, thousands of points)
    -> physical units per channel (phys_scale * V + phys_offset)
    -> reduced to `acquisition.points` samples (neighbours averaged)
    -> into the RUNNING AVERAGE: the mean of the last `averages` records
       ("312 / 500" in the GUI; "Restart average" empties it)
    -> filtered (zero-phase, identical on all channels) for display
    -> per-channel numbers (mean, rms, peak-to-peak, amplitude, frequency)
       and the phase of CH2 against CH1, live.

THE SCAN-SAFE READ: `acquire()` returns an id at once. The trace thread then
averages the next `averages` FRESH records -- the first record that becomes
ready after the trigger is skipped, because it may have been captured just
before the trigger (the scope's "new data" flag says only that a record was
finished, not when it began) -- and latches the result as THE sample:
filtered (and, with keep_raw, unfiltered) traces and the numbers.
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
#: divisions across the scope's screen (RSDS1102CML+: 14; the memory holds more)
_SCREEN_DIV = 14.0


def _tdiv_key(tdiv: float) -> str:
    """A time/div as a dict key (0.5 and 0.5000000001 are the same setting)."""
    return f"{float(tdiv):.6g}"


def _flatten(got: dict) -> dict:
    """read_settings() as flat keys: ch1_vdiv_V, tdiv_s, trigger_mode, ..."""
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
    return flat


def _brief(info: dict) -> str:
    """The record diagnostics as one ASCII line for an event."""
    return ", ".join(f"{k} {v:g}" if isinstance(v, float) else f"{k} {v}"
                     for k, v in info.items())
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
        self._pending_keys: set = set()     # settings queued, not yet pushed + read back
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
        self._record_s = 0.0                # length of the latest record
        # How long a record is, PER TIME/DIV: the divisions in a record are NOT
        # the same at every time/div on the RSDS1102CML+ (lab PC 2026-10-07:
        # 41 at 1 ms/div, more at 0.5 s/div), so nothing learned at one
        # time/div may be carried to another. Two sources: the scope's own
        # SANU/SARA (read with the settings) and the span of the records
        # actually received at that time/div (`record_s`).
        self._sanu = 0                      # SANU? -- points in the record, scope's word
        self._span_at: dict = {}            # "tdiv" -> span of the records received
        self._last_record: dict = {}        # what the last read looked like (diagnostics)
        self._suspect_warned: set = set()
        self._prev_take = None              # (settings rev, tdiv, when) of the last record
        self._cycle_ms: dict = {}
        self._next_poll = -1e9              # rate limit of the record poll
        self._poll_prev = None              # the poll before the one that found a record
        self._poll_now = None
        self._alias_warned = False
        self._roll_warned = False

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_event = lambda level, msg: None
        # Where the MODULE's settings are kept on this PC (physical units --
        # a probe's calibration --, averaging, points, filter): set by
        # run_service.py to scope.ini next to the project. Every change is
        # written there at once, atomically, so a restart (also a Restart that
        # keeps the instrument's state) does not lose a calibration. None =
        # nothing is written (tests, a private local GUI).
        self.persist_path = None
        self._persist_lock = threading.Lock()

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
        for w in getattr(self.backend, "open_warnings", []):
            self._emit("warn", f"scope: {w}")
        self._emit("info", f"connected: {self._idn or 'scope'}  (settings read, nothing changed)")
        self._adopt(got, at_start=True)
        if run:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="scope-traces",
                                            daemon=True)
            self._thread.start()

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Stop reading and disconnect. A scope drives nothing, so a plain stop
        and a RESTART (`keep_outputs`, docs/DEVELOPER_NOTES.md section 4) are
        the same: neither writes to the instrument; the next start adopts its
        settings either way. (A backend with a generator -- the Analog
        Discovery -- would switch it off only when keep_outputs is False.)"""
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
            if got.get("record_points"):
                self._sanu = int(got["record_points"])
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
                self._pending_keys.add(f"{prefix}{k}")
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
        self._persist()

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
        self._persist()

    def set_keep_raw(self, on: bool) -> None:
        self.cfg.acquisition.keep_raw = bool(on)
        self._emit("info", "raw traces recorded too" if on else "only filtered traces recorded")
        self._persist()

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
        self._persist()

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
        self._persist()

    def set_sim(self, name: str, value) -> None:
        """SIMULATOR only: change the pretend bench (e.g. CH2's phase shift)."""
        if not self.simulated:
            raise ValueError("only the simulator has a pretend bench")
        if not hasattr(self.cfg.sim, name):
            raise ValueError(f"unknown sim parameter {name!r}")
        setattr(self.cfg.sim, name, _finite(value, name))
        self._emit("info", f"sim {name} = {value}")

    def restart_average(self) -> None:
        self._restart("average restarted")

    def _persist(self) -> None:
        """Write the whole config to `persist_path`: first to a temporary file
        in the same folder, then os.replace -- an interrupted write (crash,
        power cut) leaves the old file whole, never half a file. The scope's
        own settings are in it too, harmlessly: at start they are READ from the
        scope and the file's values only stand in for what cannot be read."""
        path = self.persist_path
        if not path:
            return
        import os
        tmp = f"{path}.tmp"
        try:
            with self._persist_lock:
                self.cfg.save(tmp)
                os.replace(tmp, path)
        except OSError as exc:
            self._emit("warn", f"could not save the settings to {path}: {exc}")

    # ---- the scan-safe read ----------------------------------------------------------

    def _setting(self, key: str, default):
        """A scope setting as it will be: the value ASKED while it is still
        queued (lab PC 2026-10-07: after set_tdiv the status showed the old
        time/div, record_s and roll state for up to ~2 s), else what the scope
        reported."""
        with self._lock:
            if key in self._pending_keys:
                return self._requested.get(key, default)
            return self._actual.get(key, default)

    def _tdiv(self) -> float:
        return float(self._setting("tdiv_s", self.cfg.timebase.tdiv_s) or 0.0)

    def record_s(self) -> float:
        """How long one record lasts at the CURRENT time/div: the longest of
        the screen (14 divisions), the scope's SANU / SARA, and the span of the
        records received at this time/div. A time/div still being pushed has
        no SANU / SARA yet: the screen, until the scope answers."""
        tdiv = self._tdiv()
        best = _SCREEN_DIV * tdiv
        with self._lock:
            if "tdiv_s" not in self._pending_keys:
                sara = float(self._actual.get("sample_rate_Hz") or 0.0)
                if self._sanu > 0 and sara > 0:
                    best = max(best, self._sanu / sara)
            best = max(best, self._span_at.get(_tdiv_key(tdiv), 0.0))
        return best

    def rolling(self) -> bool:
        """AUTO trigger mode at a slow time/div: the scope free-runs / rolls and
        makes no triggered records (lab PC 2026-10-07: one record in 120 s at
        0.5 s/div in AUTO). In NORMAL mode triggered records still come at
        every time/div, slowly -- measured one per ~8 s at 0.1 - 0.5 s/div --
        so NORMAL is never "rolling"."""
        mode = self._setting("trigger_mode", self.cfg.trigger.mode)
        return (mode == "auto"
                and self._tdiv() >= float(self.cfg.hardware.roll_tdiv_s) > 0)

    def acquire(self) -> int:
        """Start an acquisition; returns its id at once (see module docstring).
        Refused when the scope cannot deliver: not connected, STOPPED, or in
        SINGLE mode with more than one trace to average."""
        if not self._connected:
            raise ValueError("not connected")
        mode = self._setting("trigger_mode", self.cfg.trigger.mode)
        if mode == "stop":
            raise ValueError("the scope is stopped: no new traces will come "
                             "(set the trigger mode to normal or auto)")
        if self.rolling():
            raise ValueError(
                f"at {self._tdiv():g} s/div in AUTO trigger mode the scope free-runs "
                f"(rolls): it makes no triggered records to average (lab PC: one record "
                f"in 120 s). Set the trigger mode to NORMAL -- triggered records then "
                f"come at any time/div, one per record length (~{self.record_s():.3g} s "
                f"here) -- or use a time/div faster than "
                f"{self.cfg.hardware.roll_tdiv_s:g} s/div.")
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
                "n": 0, "skip": 0, "sum": None, "t": None, "clipped": set(),
                # where the time goes (logged when it completes)
                "seen": 0, "skipped_first": 0, "not_fresh": 0, "resampled": 0}

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
        # (full-resolution records; _process reduces them for the trace)
        raw = {ch: np.mean([r[ch] for r in recs], axis=0) for ch in chans}
        return self._process(t, raw, n)

    def get_time(self) -> list:
        """The time axis of the latched sample (or of the live average)."""
        with self._lock:
            if self._sample_trace is not None:
                return list(self._sample_trace["time_s"])
            if self._live_t.size < 2:
                return []
            tr, _ = analysis.reduce_points(self._live_t, {}, int(self.cfg.acquisition.points))
            return list(tr)

    def _process(self, t: np.ndarray, raw: dict, n: int) -> dict:
        """Filter, numbers and the stored trace for an averaged record (pure;
        no lock). `t` / `raw` are the FULL record as read from the scope.

        Filter and numbers (pk-pk, frequency, phase ...) run on the full
        record; only then is the trace reduced to `points` for storage and
        display. Reducing first made the numbers depend on the reduction:
        lab PC 2026-10-07, 0.5 s/div, a 1 Vpp 50 Hz sine read 0.26 Vpp at
        "11 Hz" because 20 samples (0.8 period) were averaged into one."""
        f = self.cfg.filter
        dt = float(t[1] - t[0]) if t.size > 1 else 0.0
        filt = {ch: analysis.zero_phase(y, dt, f.lowpass_Hz, f.highpass_Hz, f.order)
                for ch, y in raw.items()}
        points = int(self.cfg.acquisition.points)
        out = {"averages": n, "lowpass_Hz": f.lowpass_Hz,
               "highpass_Hz": f.highpass_Hz, "filter_order": f.order,
               "record_points": int(t.size)}
        freqs = []
        for ch, y in filt.items():
            out[f"{ch}_values"] = analysis.channel_values(t, y)
            out[f"{ch}_unit"] = self.cfg.channel(ch).phys_unit
            fr = out[f"{ch}_values"]["frequency"]
            if fr > 0:
                freqs.append(fr)
        if "ch1" in filt and "ch2" in filt:
            out["phase_21_deg"], out["phase_21_reason"] = analysis.phase_detail(
                t, filt["ch1"], filt["ch2"])
        else:
            out["phase_21_deg"] = _NAN
            out["phase_21_reason"] = "needs CH1 and CH2 both on"
        # the stored trace: average neighbours only within 1/20 of the
        # shortest period seen, else sample (no amplitude lost)
        max_bin = 1.0 / (20.0 * max(freqs)) if freqs else 0.0
        both = {**{ch: y for ch, y in filt.items()},
                **{f"{ch}_raw": y for ch, y in raw.items()}}
        tr, red = analysis.reduce_points(t, both, points, max_bin_s=max_bin)
        out["time_s"] = tr
        out.update(red)
        span = float(t[-1] - t[0]) if t.size > 1 else 0.0
        # too few stored points per period: the trace shows an alias (the
        # numbers above do not -- they come from the full record)
        per_period = (points / (span * max(freqs))) if (freqs and span > 0) else _NAN
        out["points_per_period"] = per_period
        out["trace_aliased"] = bool(per_period == per_period and per_period < 4.0)
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
                  "record_s": self.record_s(),
                  "last_record": dict(self._last_record),
                  "rolling": self.rolling(),
                  "trigger_rate_Hz": self._trigger_rate_locked(),
                  "running_n": len(self._running),
                  "averages": int(c.acquisition.averages),
                  "points": int(c.acquisition.points),
                  "keep_raw": bool(c.acquisition.keep_raw),
                  "lowpass_Hz": c.filter.lowpass_Hz, "highpass_Hz": c.filter.highpass_Hz,
                  "filter_order": int(c.filter.order),
                  "settings_settled": (self._connected and not self._hw_error
                                       and self._settings_done == self._settings_gen),
                  "acq_id": self._acq_id, "acquiring": a is not None,
                  "acq_progress": (a["n"] / a["want"]) if a else 0.0,
                  "sample": dict(self._sample),
                  "live": dict(self._live_numbers)}
            for ch in CHANNEL_NAMES:
                ch_cfg = c.channel(ch)
                for k in ("enabled", "vdiv_V", "offset_V", "coupling", "probe"):
                    st[f"{ch}_{k}"] = self._shown(f"{ch}_{k}", getattr(ch_cfg, k))
                    st[f"{ch}_{k}_set"] = self._requested.get(f"{ch}_{k}", getattr(ch_cfg, k))
                st[f"{ch}_unit"] = ch_cfg.phys_unit
                st[f"{ch}_label"] = ch_cfg.phys_label or ch.upper()
                st[f"{ch}_phys_scale"] = ch_cfg.phys_scale
                st[f"{ch}_phys_offset"] = ch_cfg.phys_offset
            for k in ("tdiv_s", "delay_s"):
                st[k] = self._shown(k, getattr(c.timebase, k))
                st[f"{k}_set"] = self._requested.get(k, getattr(c.timebase, k))
            st["sample_rate_Hz"] = self._actual.get("sample_rate_Hz", _NAN)
            if self.simulated:                  # the pretend bench's knobs
                for k in ("frequency_Hz", "ch2_phase_deg", "noise_V"):
                    st[f"sim_{k}"] = getattr(c.sim, k)
            for k in ("source", "level_V", "slope", "mode"):
                st[f"trigger_{k}"] = self._shown(f"trigger_{k}", getattr(c.trigger, k))
                st[f"trigger_{k}_set"] = self._requested.get(f"trigger_{k}",
                                                             getattr(c.trigger, k))
        return st

    def _shown(self, key: str, default):
        """Lock held: the queued value of a pending setting, else the scope's."""
        if key in self._pending_keys:
            return self._requested.get(key, default)
        return self._actual.get(key, default)

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
        self._persist()

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
        t_cycle = time.perf_counter()
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
        roll = self.rolling()
        if roll and not self._roll_warned:
            self._roll_warned = True
            self._emit("warn", f"{self._tdiv():g} s/div in AUTO: the scope rolls -- no "
                               f"triggered records; acquisitions are refused (set the "
                               f"trigger mode to NORMAL, or a time/div faster than "
                               f"{self.cfg.hardware.roll_tdiv_s:g} s/div)")
        elif not roll:
            self._roll_warned = False
        # Poll no more often than a quarter of a record (max 2 s apart): a
        # slow time/div must not hammer the bus with reads of the same record.
        if now < self._next_poll:
            return False
        self._next_poll = now + min(0.25 * self.record_s(), 2.0)
        with self._lock:
            rev0 = self._rev
            chans = [ch for ch in self.channels if self._actual.get(f"{ch}_enabled", True)]
        if not chans:
            return False
        # a record found now was not there at the previous poll: it ENDED
        # after that moment (used for the freshness of an acquisition)
        self._poll_prev, self._poll_now = self._poll_now, now
        t_poll = time.perf_counter()
        mp = int(self.cfg.hardware.max_points)
        if hasattr(self.backend, "read_new_traces"):
            # the backend tells a new record by its CONTENT (Siglent: INR?
            # blocks ~0.5 s per call while the scope runs -- lab PC
            # 2026-10-07 -- so it is not asked at all)
            with self._hw:
                got = self.backend.read_new_traces(chans, mp)
            t_ready = t_read = time.perf_counter()
            if got is None:
                return False
            t, volts = got
        else:
            with self._hw:
                ready = self.backend.new_trace_ready()
            t_ready = time.perf_counter()
            if not ready:
                return False
            with self._hw:
                t, volts = self.backend.read_traces(chans, mp)
            t_read = time.perf_counter()
        # where one cycle's time goes (status last_record.cycle_ms): settings
        # push / re-read, the poll for a new record, the record read, the numbers
        self._cycle_ms = {"settings": 1000 * (t_poll - t_cycle),
                          "poll": 1000 * (t_ready - t_poll),
                          "read": 1000 * (t_read - t_ready)}
        self._take(t, volts, rev0, now)
        self._cycle_ms["take"] = 1000 * (time.perf_counter() - t_read)
        with self._lock:
            if self._last_record:
                self._last_record["cycle_ms"] = {k: round(v, 1)
                                                 for k, v in self._cycle_ms.items()}
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
        got_flat = _flatten(got)                # THIS push's read-back, nothing older
        snapped = []
        with self._lock:
            self._settings_done = pending[-1][0]
            self._last_reread = self._clock()
            still_queued = set()
            for _, kind, values in self._pending:          # asked again meanwhile
                prefix = {"timebase": "", "trigger": "trigger_"}.get(kind, f"{kind}_")
                still_queued |= {f"{prefix}{k}" for k in values}
            # Only the LAST request per setting in this batch is compared with
            # the read-back of this push: requests in quick succession (0.5,
            # then 0.01 s/div) were each compared with the final value and gave
            # "asked 0.5, the scope set 0.01" (lab PC 2026-10-07). A setting
            # asked again meanwhile is not compared at all -- its own push is.
            last = {}
            for _, kind, values in pending:
                prefix = {"timebase": "", "trigger": "trigger_"}.get(kind, f"{kind}_")
                for k, asked in values.items():
                    last[f"{prefix}{k}"] = asked
            for key, asked in last.items():
                if key in still_queued:
                    continue
                self._pending_keys.discard(key)
                held = got_flat.get(key)
                # the scope snaps to its own steps (lab PC 2026-10-07: TDIV
                # 20 ms -> 10 ms, 200 ms -> 100 ms; 50 / 100 / 500 ms
                # stick): say so, the value it HOLDS is what is shown
                if (isinstance(asked, float) and isinstance(held, (int, float))
                        and not isinstance(held, bool)
                        and abs(held - asked) > 1e-3 * max(abs(asked), 1e-12)):
                    snapped.append(f"{key.replace('_', ' ')}: asked {asked:g}, "
                                   f"the scope set {held:g}")
        for msg in snapped:
            self._emit("warn", msg)
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
        # the running average and the acquisition keep the FULL record; the
        # reduction to `points` happens in _process, after the numbers
        tr, red = np.asarray(t, float), phys
        span = float(t[-1] - t[0]) if len(t) > 1 else 0.0
        info = dict(getattr(self.backend, "last_record", {}) or {})
        info.update({"points": int(len(t)), "span_s": span})
        latched = None
        suspect = None
        took = None
        with self._lock:
            if self._rev != rev0:
                return                          # settings changed during the read
            tdiv = float(self._actual.get("tdiv_s") or 0.0)
            info["tdiv_s"] = tdiv
            self._last_record = info
            if span > 0 and tdiv > 0:
                self._record_s = span           # for the acquisition timeout
                # the span received AT THIS time/div (a record of another
                # time/div never teaches this one -- divisions differ)
                self._span_at[_tdiv_key(tdiv)] = span
                # Only the POST-trigger part of a record has to be recorded
                # after the previous one: the pre-trigger part comes from the
                # buffer, which keeps running while the scope re-arms. Lab PC
                # 2026-10-07, 0.5 s/div, delay 0: a 16.4 s record (8.2 s after
                # the trigger) every 7.93 s -- consecutive records OVERLAP by
                # about half, and each is still one coherent capture. So a
                # record is suspect only if its post-trigger part (t[-1]) is
                # longer than the time since the previous record at the SAME
                # settings (the first after a change is never compared).
                # Not measured: the FIRST record after a change (it may have
                # been under way during the change -- lab PC 2026-10-07, a
                # false "records every 2 s" right after 1 ms -> 0.5 s/div),
                # nor a record found by the INR? fallback (its time is the
                # fallback's, not the record's).
                key = _tdiv_key(tdiv)
                post = max(0.0, float(t[-1]))
                prev = self._prev_take
                nth = prev[3] + 1 if (prev is not None and prev[0] == rev0
                                      and prev[1] == key) else 1
                if nth >= 3 and not info.get("identical") and not prev[4]:
                    gap = now - prev[2]
                    if 0 < gap and post > 1.5 * gap + 0.5 and key not in self._suspect_warned:
                        self._suspect_warned.add(key)
                        suspect = (post, gap, info)
                self._prev_take = (rev0, key, now, nth, bool(info.get("identical")))
            self._records += 1
            self._trigger_times.append(now)
            if self._live_t.size != tr.size or not np.allclose(self._live_t, tr):
                if self._running and self._live_t.size > 1 and \
                        abs((tr[-1] - tr[0]) - (self._live_t[-1] - self._live_t[0])) \
                        <= 0.01 * (self._live_t[-1] - self._live_t[0]):
                    # the same window, a point more or less: resample
                    red = {ch: np.interp(self._live_t, tr, y) for ch, y in red.items()}
                    tr = self._live_t
                else:
                    self._running.clear()       # a new time axis: start again
                    self._live_t = tr
            self._running.append((now, red))
            while len(self._running) > max(1, int(c.acquisition.averages)):
                self._running.popleft()
            a = self._acq
            # FRESH = the whole record, pre-trigger part included, was recorded
            # after acquire(). The record ended before `now` (the poll that
            # found it) and after the previous poll, so it began no earlier
            # than now - span - (one poll + margin). At a fast time/div that
            # is "the next record"; at 0.5 s/div (16 s records that overlap by
            # half, see above) it waits for one whose pre-trigger part, too,
            # is new. Plus the old rule: the first record after the trigger is
            # skipped anyway.
            # (This replaces "skip the first record after the trigger": it
            # says the same thing with the record's length in it, and at a
            # slow time/div it costs no whole record.)
            # Only the FIRST counted record needs the test (lab PC
            # 2026-10-07: applied to every record with a 0.5 s margin it made
            # acquires 2-4x slower); every later one is a newer record, fresh
            # by construction. The record ended after the previous poll, so
            # it began after (previous poll - span).
            ended_after = self._poll_prev if (self._poll_prev is not None
                                              and self._poll_prev < now) else \
                now - float(self.cfg.hardware.poll_s) - 0.5
            if a is not None and now >= a["t0"]:
                a["seen"] += 1
                # "trigger" freshness: only the part AFTER the trigger (t > 0)
                # must be newer than the request (config: Acquisition.freshness)
                need = span if self.cfg.acquisition.freshness != "trigger" \
                    else max(0.0, float(tr[-1]))
                fresh = a["n"] > 0 or ended_after - need >= a["t0"]
                if a["skip"] > 0:
                    a["skip"] -= 1              # may have begun before the trigger
                    a["skipped_first"] += 1
                elif not fresh:
                    a["not_fresh"] += 1         # its pre-trigger part predates acquire()
                else:
                    if a["sum"] is None:
                        a["sum"] = {ch: y.copy() for ch, y in red.items()}
                        a["t"] = tr
                        a["n"] = 1
                    else:
                        if a["t"].size != tr.size or not np.allclose(a["t"], tr):
                            # a record one point longer or shorter (the
                            # transfer is thinned by the scope): onto the
                            # first record's time axis -- restarting the sum
                            # here threw records away, acquires got slow
                            red = {ch: np.interp(a["t"], tr, y) for ch, y in red.items()}
                            a["resampled"] += 1
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
                                  "phase_21_reason": proc["phase_21_reason"],
                                  "lowpass_Hz": proc["lowpass_Hz"],
                                  "highpass_Hz": proc["highpass_Hz"]}
                        for ch in mean:
                            sample[ch] = dict(proc[f"{ch}_values"])
                        # sample + "acquiring = False" in ONE critical section (gotcha #28)
                        self._sample = sample
                        self._sample_trace = proc
                        self._acq = None
                        latched = sample
                        took = f"acquisition #{a['id']}: {a['n']} records in " \
                               f"{now - a['t0']:.1f} s ({a['seen']} seen: " \
                               f"{a['not_fresh']} begun before acquire, " \
                               f"{a['resampled']} resampled; last cycle ms " \
                               f"{_brief({k: round(v, 1) for k, v in self._cycle_ms.items()})}" \
                               f"; read ms {_brief(info.get('ms') or {})})"
            recs = [r for _, r in self._running]
            n_live = len(recs)
        # live numbers (outside the lock: a few ms of numpy)
        mean = {ch: np.mean([r[ch] for r in recs], axis=0) for ch in red}
        proc = self._process(tr, mean, n_live)
        if proc["trace_aliased"] and not self._alias_warned:
            self._alias_warned = True
            self._emit("warn", f"the stored trace has {proc['points_per_period']:.2g} points "
                               f"per signal period ({int(c.acquisition.points)} points over "
                               f"{proc['record_points']} read): it shows an alias -- raise "
                               f"'points' or use a faster time/div. The NUMBERS are from the "
                               f"full record and are right.")
        elif not proc["trace_aliased"]:
            self._alias_warned = False
        live = {"phase_21_deg": proc["phase_21_deg"],
                "phase_21_reason": proc["phase_21_reason"],
                "clipped": sorted(clipped), "n": n_live}
        for ch in red:
            live[ch] = dict(proc[f"{ch}_values"])
        with self._lock:
            self._live_numbers = live
        if took is not None:
            self._emit("info", took)
        if suspect is not None:
            span, gap, info = suspect
            self._emit("warn", f"at {info['tdiv_s']:g} s/div the record read has "
                               f"{span:.3g} s after its trigger but records arrive every "
                               f"{gap:.3g} s: it cannot be ONE fresh record -- treat these "
                               f"traces as suspect (details: {_brief(info)})")
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
