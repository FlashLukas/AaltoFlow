"""The SpectrumAnalyzer: the brain between the wire and the backend (simulated or real).

Its SETTINGS (frequency range, points, RBW, VBW, reference level, attenuation,
sweep time, detector, preamp, tracking generator) are set-and-forget: clamp,
store, report. Its MEASUREMENT is a trace, and a scan must never record a
trace that was swept before the scan step it is filed under -- the
fire-and-forget contract makes that easy to get wrong, and nothing raises when
it happens.

Two ways to get a trace:

  continuous  the analyser sweeps on its own, like the front panel; the latest
              trace is `get_trace("last")`. Right for looking at it.

  acquire     the scan-safe read. `acquire()` returns an id at once. The sweep
              thread then averages the next `sweep.averages` sweeps that STARTED
              after the trigger (a sweep already running is thrown away) and
              latches the mean as the sample: `get_trace("sample")` +
              `status().sample`. Callers wait until status shows `acq_id` ==
              their id AND `acquiring` False.

Averaging is done in LINEAR POWER (mW), then converted back to dBm: averaging
dB numbers would put a noise floor 2.5 dB too low (see model.power_average_dBm).

A settings change in the middle of an acquisition RESTARTS it: averaging a
trace taken at 3 MHz RBW with one at 10 kHz is not a measurement of anything.

THE REFERENCE (scalar network analysis with the tracking generator). Connect a
THRU where the device will go, `take_reference`, put the device in: the
normalised trace
        norm_dB = trace_dBm - reference_dBm
is the device's transmission |S21| in dB, with the generator's ripple and the
cables' loss divided out. The brain owns the reference, so the GUI, the
console and a scan all mean the same one:
  take_reference   an acquisition exactly like `acquire` whose sample is ALSO
                   stored as the reference -- in the same critical section that
                   latches the sample (gotcha #28).
  get_trace(quantity="norm")  refused, with a message saying what differs, when
                   there is no reference, the tracking generator was off for
                   either trace, or start / stop / points / TG level differ.

Threads and locks, the pm16/hf2 rules:
  * ONE sweep thread talks to the backend. `status()` only copies what it
    stored and never touches the hardware (gotcha #1).
  * Every backend call runs under `_hw`, but the WAIT during a sweep does not --
    a narrow-RBW sweep can take minutes, and a setter must not hang that long.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import model
from .config import Config
from .model import DETECTORS, DUTS

_NAN = float("nan")

#: Sanity envelope for the simulated bench's numbers, name -> (min, max).
BENCH_LIMITS = {
    "dut_center_Hz": (1e3, 3e9),
    "dut_bw_Hz": (1e3, 2e9),
    "dut_order": (1, 10),
    "dut_loss_dB": (0.0, 60.0),
    "dut_isolation_dB": (0.0, 120.0),
    "cable_loss_dB_at_1GHz": (0.0, 20.0),
    "tg_ripple_dB": (0.0, 6.0),
}

#: the array quantities a trace can carry
QUANTITIES = {"power": "power_dBm", "norm": "norm_dB"}


def _no_reference() -> dict:
    """The `reference` status block when there is none. Same keys as a present
    one (NaN -> null on the wire), so a client never has to test for a key."""
    return {"present": False, "acq_id": 0, "tg_on": False, "tg_level_dBm": _NAN,
            "start_Hz": _NAN, "stop_Hz": _NAN, "points": 0, "age_s": _NAN}


@dataclass
class Status:
    """One snapshot of the analyser, for status() and the wire. No arrays: the
    status goes out 10 times a second; traces are fetched with get_trace."""

    connected: bool
    idn: str = ""
    hw_error: str = ""
    simulated: bool = True
    # frequency
    start_Hz: float = _NAN
    stop_Hz: float = _NAN
    center_Hz: float = _NAN
    span_Hz: float = _NAN
    points: int = 0
    # bandwidths etc.: *_set = what was typed (the settle echo), the plain
    # name = what is IN USE (auto resolved; on the real one, read back from it)
    rbw_Hz: float = _NAN
    rbw_set_Hz: float = _NAN
    rbw_auto: bool = True
    vbw_Hz: float = _NAN
    vbw_set_Hz: float = _NAN
    vbw_auto: bool = True
    ref_level_dBm: float = _NAN
    atten_dB: float = _NAN
    atten_set_dB: float = _NAN
    atten_auto: bool = True
    sweep_time_s: float = _NAN
    sweep_time_set_s: float = _NAN
    sweep_time_auto: bool = True
    detector: str = "auto"
    detector_in_use: str = ""
    preamp: bool = False
    averages: int = 1
    continuous: bool = True
    # tracking generator
    tg_on: bool = False
    tg_level_dBm: float = _NAN
    # what the sweep thread is doing
    sweeping: bool = False
    sweep_progress: float = 0.0
    sweeps: int = 0                    # completed sweeps since start
    trace_id: int = 0                  # id of the latest trace (any kind)
    peak_Hz: float = _NAN              # marker on the highest point of the latest trace
    peak_dBm: float = _NAN
    floor_dBm: float = _NAN            # median of the latest trace
    overload: bool = False             # the mixer was driven into compression
    # the simulated bench (NaN / "" on a real analyser)
    dut: str = ""
    dut_center_Hz: float = _NAN
    dut_bw_Hz: float = _NAN
    dut_order: int = 0
    dut_loss_dB: float = _NAN
    dut_isolation_dB: float = _NAN
    cable_loss_dB_at_1GHz: float = _NAN
    tg_ripple_dB: float = _NAN
    # acquisition
    acq_id: int = 0
    acquiring: bool = False
    acq_progress: float = 0.0
    acq_is_reference: bool = False
    sample: dict = field(default_factory=dict)
    reference: dict = field(default_factory=_no_reference)


def _clamp(value, lo, hi):
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def _finite(value, what: str) -> float:
    """float(value), refusing NaN and inf -- NaN passes every `<`/`>` clamp."""
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return v


def _fmt_Hz(hz: float) -> str:
    """1.8e9 -> '1.8 GHz' for the event log."""
    for unit, scale in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if abs(hz) >= scale:
            return f"{hz / scale:.6g} {unit}"
    return f"{hz:.6g} Hz"


class SpectrumAnalyzer:
    def __init__(self, backend, cfg: Config | None = None, clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards the snapshot, traces, acquisition

        self._connected = False
        self._idn = ""
        self._hw_error = ""
        self._last_err_emit = -1e9

        # everything below is written under _lock
        self._rev = 0                       # bumped by any change that alters a trace
        self._applied: model.SweepSettings | None = None   # what the backend was last configured with
        self._readback: dict = {}           # what the backend says it uses
        self._sweeping = False
        self._sweep_t0 = 0.0
        self._sweep_dt = 0.0
        self._sweeps = 0
        self._trace_id = 0
        self._last: dict | None = None      # latest trace, any kind
        self._acq_id = 0
        self._acq: dict | None = None
        self._sample: dict = {}
        self._sample_trace: dict | None = None
        self._reference: dict | None = None  # a latched sample trace + "taken_at"

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    @property
    def simulated(self) -> bool:
        return bool(getattr(self.backend, "simulated", True))

    # ---- lifecycle ---------------------------------------------------------------

    def start(self, run: bool = True) -> None:
        """Open the analyser, ADOPT its current settings, start the sweep thread.

        Lukas's rule (2026-09-27): starting the software must not change the
        instrument. So nothing is pushed here -- not the .ini, not a safe
        tracking-generator state. The backend is only QUERIED; what the
        instrument is doing (span, RBW, reference level, TG on and its level,
        ...) is copied into cfg, so status, describe and the GUI show the
        instrument as it is, and the sweep thread's first `configure` finds
        nothing to change. The .ini values of the sweep/tracking groups are
        therefore only a fallback for a setting the instrument does not report;
        they are written only when the user sets something (a setter or
        set_config). `run=False` skips the thread, so a test can drive `step()`."""
        self._sanitise_config()
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            state = self.backend.read_state()
        self._connected = True
        self._emit("info", f"connected: {self._idn}")
        self._adopt(state)
        with self._hw:
            # Knobs the instrument did not report keep the .ini value; tell the
            # backend to treat them as already applied, so they are written only
            # when the user changes them -- never as a side effect of starting.
            self.backend.mark_in_sync(model.resolve(self.cfg))
        if run:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="gsp818-sweep", daemon=True)
            self._thread.start()

    #: instrument state key -> (config group, config field)
    _ADOPT = {
        "start_Hz": ("sweep", "start_Hz"), "stop_Hz": ("sweep", "stop_Hz"),
        "points": ("sweep", "points"),
        "rbw_Hz": ("sweep", "rbw_Hz"), "rbw_auto": ("sweep", "rbw_auto"),
        "vbw_Hz": ("sweep", "vbw_Hz"), "vbw_auto": ("sweep", "vbw_auto"),
        "ref_level_dBm": ("sweep", "ref_level_dBm"),
        "atten_dB": ("sweep", "atten_dB"), "atten_auto": ("sweep", "atten_auto"),
        "sweep_time_s": ("sweep", "sweep_time_s"),
        "sweep_time_auto": ("sweep", "sweep_time_auto"),
        "detector": ("sweep", "detector"), "preamp": ("sweep", "preamp"),
        "tg_on": ("tracking", "tg_on"), "tg_level_dBm": ("tracking", "level_dBm"),
    }

    def _adopt(self, state: dict) -> None:
        """Copy the instrument's reported settings into cfg.

        NOT clamped to Limits: the instrument is doing it, and clamping would
        make the first configure WRITE the clamped value -- a change on the
        instrument caused only by starting the software. A value outside the
        configured envelope is kept and announced (several limits are
        themselves # VERIFY guesses, e.g. the point range)."""
        state = dict(state or {})
        missing = []
        for key, (group, name) in self._ADOPT.items():
            if key not in state or state[key] is None:
                missing.append(key)
                continue
            old = getattr(getattr(self.cfg, group), name)
            v = state[key]
            if isinstance(old, bool):
                v = bool(v)
            elif isinstance(old, int):
                v = int(v)
            elif isinstance(old, float):
                v = float(v)
                if not math.isfinite(v):
                    missing.append(key)
                    continue
            else:
                v = str(v)
            setattr(getattr(self.cfg, group), name, v)
        sw, tg, lim = self.cfg.sweep, self.cfg.tracking, self.cfg.limits
        outside = []
        for label, v, lo, hi in (
                ("start", sw.start_Hz, lim.freq_min_Hz, lim.freq_max_Hz),
                ("stop", sw.stop_Hz, lim.freq_min_Hz, lim.freq_max_Hz),
                ("points", sw.points, lim.points_min, lim.points_max),
                ("reference level", sw.ref_level_dBm, lim.ref_level_min_dBm, lim.ref_level_max_dBm),
                ("TG level", tg.level_dBm, lim.tg_level_min_dBm, lim.tg_level_max_dBm)):
            if not lo <= v <= hi:
                outside.append(f"{label} {v:g} (limits {lo:g}..{hi:g})")
        if sw.detector not in DETECTORS:
            outside.append(f"detector {sw.detector!r}")
        self._emit("info", "adopted from the instrument: "
                   f"{_fmt_Hz(sw.start_Hz)} - {_fmt_Hz(sw.stop_Hz)}, {int(sw.points)} points, "
                   f"RBW {'auto' if sw.rbw_auto else _fmt_Hz(sw.rbw_Hz)}, "
                   f"ref {sw.ref_level_dBm:g} dBm, detector {sw.detector}, "
                   f"preamp {'on' if sw.preamp else 'off'}, tracking generator "
                   f"{'ON' if tg.tg_on else 'off'} ({tg.level_dBm:g} dBm)")
        if outside:
            self._emit("warn", "the instrument is set outside the configured limits, kept "
                               "as it is (nothing written): " + "; ".join(outside))
        if missing:
            self._emit("warn", "the instrument did not report " + ", ".join(missing)
                       + ": showing the configured value (not written to it)")
        for note in (state.get("notes") or []):
            self._emit("warn", str(note))

    def shutdown(self) -> None:
        """Stop sweeping, switch the tracking generator off and disconnect.
        Safe to call more than once."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        was = self._connected
        try:
            with self._hw:
                self.backend.close()        # the backend turns the TG off first
        finally:
            self._connected = False
            self.cfg.tracking.tg_on = False
            with self._lock:
                self._acq = None
                self._sweeping = False
                self._applied = None
            if was:
                self._emit("info", "disconnected (tracking generator off)")

    # ---- frequency (each clamps, stores in cfg, reports) ----------------------------

    def start_limits(self) -> tuple[float, float]:
        lim = self.cfg.limits
        return lim.freq_min_Hz, self.cfg.sweep.stop_Hz - lim.min_span_Hz

    def stop_limits(self) -> tuple[float, float]:
        lim = self.cfg.limits
        return self.cfg.sweep.start_Hz + lim.min_span_Hz, lim.freq_max_Hz

    def center_limits(self) -> tuple[float, float]:
        """The centre may go anywhere a minimum span still fits; if the current
        span does not fit around the new centre, the SPAN shrinks (a centre
        typed near the band edge is what the user meant, not the old span)."""
        lim = self.cfg.limits
        half = lim.min_span_Hz / 2.0
        return lim.freq_min_Hz + half, lim.freq_max_Hz - half

    def span_limits(self) -> tuple[float, float]:
        """The span may grow until one edge hits the band edge (the centre stays)."""
        lim, sw = self.cfg.limits, self.cfg.sweep
        c = (sw.start_Hz + sw.stop_Hz) / 2.0
        return lim.min_span_Hz, 2.0 * min(c - lim.freq_min_Hz, lim.freq_max_Hz - c)

    def set_start(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "start"), *self.start_limits())
        self.cfg.sweep.start_Hz = v
        self._changed(f"start {_fmt_Hz(v)}", clamped)

    def set_stop(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "stop"), *self.stop_limits())
        self.cfg.sweep.stop_Hz = v
        self._changed(f"stop {_fmt_Hz(v)}", clamped)

    def set_center(self, hz: float) -> None:
        sw, lim = self.cfg.sweep, self.cfg.limits
        v, clamped = _clamp(_finite(hz, "center"), *self.center_limits())
        half = (sw.stop_Hz - sw.start_Hz) / 2.0
        room = min(v - lim.freq_min_Hz, lim.freq_max_Hz - v)
        shrunk = half > room
        half = min(half, room)
        sw.start_Hz, sw.stop_Hz = v - half, v + half
        self._changed(f"center {_fmt_Hz(v)}"
                      + (f", span reduced to {_fmt_Hz(2 * half)} to fit the band" if shrunk else ""),
                      clamped or shrunk)

    def set_span(self, hz: float) -> None:
        sw = self.cfg.sweep
        v, clamped = _clamp(_finite(hz, "span"), *self.span_limits())
        c = (sw.start_Hz + sw.stop_Hz) / 2.0
        sw.start_Hz, sw.stop_Hz = c - v / 2.0, c + v / 2.0
        self._changed(f"span {_fmt_Hz(v)}", clamped)

    def set_points(self, n: int) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(int(round(_finite(n, "points"))), lim.points_min, lim.points_max)
        self.cfg.sweep.points = int(v)
        self._changed(f"{int(v)} points", clamped)

    # ---- bandwidths, amplitude, sweep --------------------------------------------------

    def set_rbw(self, hz: float) -> None:
        """A typed RBW switches the auto coupling off (like the front panel)."""
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(hz, "RBW"), lim.rbw_min_Hz, lim.rbw_max_Hz)
        self.cfg.sweep.rbw_Hz, self.cfg.sweep.rbw_auto = v, False
        self._changed(f"RBW {_fmt_Hz(v)}", clamped)

    def set_rbw_auto(self, on: bool) -> None:
        self.cfg.sweep.rbw_auto = bool(on)
        self._changed("RBW auto" if on else "RBW manual", False)

    def set_vbw(self, hz: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(hz, "VBW"), lim.vbw_min_Hz, lim.vbw_max_Hz)
        self.cfg.sweep.vbw_Hz, self.cfg.sweep.vbw_auto = v, False
        self._changed(f"VBW {_fmt_Hz(v)}", clamped)

    def set_vbw_auto(self, on: bool) -> None:
        self.cfg.sweep.vbw_auto = bool(on)
        self._changed("VBW auto" if on else "VBW manual", False)

    def set_ref_level(self, dbm: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(dbm, "reference level"),
                            lim.ref_level_min_dBm, lim.ref_level_max_dBm)
        self.cfg.sweep.ref_level_dBm = v
        self._changed(f"reference level {v:g} dBm", clamped)

    def set_atten(self, db: float) -> None:
        """Input attenuation in whole dB (the instrument's 1 dB steps)."""
        v, clamped = _clamp(float(round(_finite(db, "attenuation"))), 0.0,
                            self.cfg.limits.atten_max_dB)
        self.cfg.sweep.atten_dB, self.cfg.sweep.atten_auto = v, False
        self._changed(f"attenuation {v:g} dB", clamped)

    def set_atten_auto(self, on: bool) -> None:
        self.cfg.sweep.atten_auto = bool(on)
        self._changed("attenuation auto" if on else "attenuation manual", False)

    def set_sweep_time(self, s: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(s, "sweep time"), lim.sweep_time_min_s, lim.sweep_time_max_s)
        self.cfg.sweep.sweep_time_s, self.cfg.sweep.sweep_time_auto = v, False
        self._changed(f"sweep time {v:g} s", clamped)

    def set_sweep_time_auto(self, on: bool) -> None:
        self.cfg.sweep.sweep_time_auto = bool(on)
        self._changed("sweep time auto" if on else "sweep time manual", False)

    def set_detector(self, detector: str) -> None:
        detector = str(detector).lower()
        if detector not in DETECTORS:
            raise ValueError(f"detector must be one of {DETECTORS}, got {detector!r}")
        self.cfg.sweep.detector = detector
        self._changed(f"detector {detector}", False)

    def set_preamp(self, on: bool) -> None:
        self.cfg.sweep.preamp = bool(on)
        self._changed("preamp on (+20 dB, lower noise floor)" if on else "preamp off", False)

    def set_averages(self, n: int) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(int(round(_finite(n, "averages"))), lim.averages_min, lim.averages_max)
        self.cfg.sweep.averages = int(v)
        self._changed(f"{int(v)} averages per acquisition", clamped)

    def set_continuous(self, on: bool) -> None:
        self.cfg.acquisition.continuous = bool(on)
        self._emit("info", "continuous sweep on" if on else "sweep on trigger only")

    # ---- tracking generator ---------------------------------------------------------

    def set_tg(self, on: bool) -> None:
        """Tracking generator output on/off. Takes effect at the sweep thread's
        next pass (within ~0.1 s), even when nothing is sweeping."""
        self.cfg.tracking.tg_on = bool(on)
        self._changed(f"tracking generator {'ON' if on else 'off'} "
                      f"({self.cfg.tracking.level_dBm:g} dBm)", False)

    def tg_off(self) -> None:
        """Tracking generator output off. Same as set_tg(False); a name of its
        own because over the wire it is the SAFETY verb a viewer may always
        send (net/service.py, control) -- set_tg could also switch RF ON."""
        self.set_tg(False)

    def set_tg_level(self, dbm: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(dbm, "TG level"), lim.tg_level_min_dBm, lim.tg_level_max_dBm)
        self.cfg.tracking.level_dBm = v
        self._changed(f"tracking generator level {v:g} dBm", clamped)

    # ---- the simulated bench ----------------------------------------------------------

    def set_dut(self, dut: str) -> None:
        dut = str(dut).lower()
        if dut not in DUTS:
            raise ValueError(f"dut must be one of {DUTS}, got {dut!r}")
        self.cfg.bench.dut = dut
        # NOT _changed: swapping the device is the physical world changing (like
        # a sample being moved), not a setting of the analyser.
        self._emit("info", f"bench: {dut} between GEN OUTPUT and RF INPUT")

    def set_bench(self, name: str, value: float) -> None:
        if name not in BENCH_LIMITS:
            raise ValueError(f"unknown bench parameter {name!r}; one of {', '.join(BENCH_LIMITS)}")
        v, clamped = _clamp(_finite(value, name), *BENCH_LIMITS[name])
        if name == "dut_order":
            v = int(round(v))
        setattr(self.cfg.bench, name, v)
        self._emit("warn" if clamped else "info",
                   f"bench {name} = {v:g}" + (" (clamped)" if clamped else ""))

    def set_carriers(self, text: str) -> None:
        model.parse_carriers(text)          # raises on nonsense before anything changes
        self.cfg.bench.carriers = str(text)
        self._emit("info", f"bench carriers: {text}")

    # ---- the scan-safe read ------------------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its id immediately. The clock starts
        NOW: call it after everything the measurement depends on has been set."""
        return self._trigger(reference=False)

    def take_reference(self) -> int:
        """Start an acquisition that becomes THE reference when it completes.
        Returns its id; wait for it exactly as for `acquire`."""
        if not self.cfg.tracking.tg_on:
            self._emit("warn", "taking a reference with the tracking generator OFF: "
                               "it cannot normalise anything (norm will be refused)")
        return self._trigger(reference=True)

    def clear_reference(self) -> None:
        with self._lock:
            had = self._reference is not None
            self._reference = None
        if had:
            self._emit("info", "reference cleared")

    def abort(self) -> None:
        """Cancel a running acquisition. It is latched as ABORTED rather than
        simply dropped: a caller waiting for "acq_id == n and not acquiring"
        would otherwise see exactly that and read the PREVIOUS trace.

        Aborting a take_reference also CLEARS the old reference, so a scan that
        asked for a new one never divides by an older one by accident."""
        cleared = False
        with self._lock:
            a = self._acq
            if a is None:
                return
            self._acq = None
            self._sample = {"acq_id": a["id"], "aborted": True, "time": time.time(),
                            "reference": bool(a["reference"])}
            self._sample_trace = None
            if a["reference"] and self._reference is not None:
                self._reference, cleared = None, True
        self._emit("warn", f"acquisition #{a['id']} aborted")
        if cleared:
            self._emit("warn", "reference cleared: taking the new one was aborted")

    def get_sample(self) -> dict:
        with self._lock:
            return dict(self._sample)

    def get_trace(self, which: str = "sample", quantity: str = "power") -> dict:
        """A trace and what it was measured under.

        which    "sample" (the latched acquisition, what a scan records), "last"
                 (the newest sweep of any kind) or "reference".
        quantity "power"  the spectrum in dBm (key "power_dBm")
                 "norm"   trace minus the reference, in dB (key "norm_dB"):
                          the DUT's transmission, generator ripple and cables
                          divided out.

        Raises ValueError when there is nothing honest to return."""
        if quantity not in QUANTITIES:
            raise ValueError(f"quantity must be one of {tuple(QUANTITIES)}, got {quantity!r}")
        with self._lock:
            ref = self._reference
            if which == "last":
                t = self._last
                if t is None:
                    raise ValueError("no sweep finished yet")
            elif which == "sample":
                if self._sample.get("aborted"):
                    raise ValueError(f"acquisition #{self._sample['acq_id']} was aborted")
                t = self._sample_trace
                if t is None:
                    raise ValueError("no acquisition latched yet")
            elif which == "reference":
                t = ref
                if t is None:
                    raise ValueError("no reference: take one first (take_reference)")
            else:
                raise ValueError(f"which must be 'sample', 'last' or 'reference', got {which!r}")
            t = dict(t)
            now = self._clock()
        t.pop("taken_at", None)
        # The arrays are never modified in place after they are published, so
        # the subtraction can run outside the lock.
        if quantity == "norm":
            if ref is None:
                raise ValueError("norm needs a reference and there is none: connect a thru "
                                 "and take a reference first (take_reference)")
            diffs = _reference_mismatch(t, ref)
            if diffs:
                raise ValueError("norm refused: the reference does not match this trace ("
                                 + "; ".join(diffs) + "). Take a new reference.")
            t["norm_dB"] = t.pop("power_dBm") - ref["power_dBm"]
            t["reference_acq_id"] = ref["acq_id"]
            t["reference_age_s"] = now - ref["taken_at"]
        # the grid is a linspace, so it is rebuilt rather than stored with every trace
        t["freqs_Hz"] = np.linspace(t["start_Hz"], t["stop_Hz"], int(t["points"]))
        return t

    def frequencies(self) -> np.ndarray:
        """The grid the NEXT sweep will use (a scan reads it once, before starting)."""
        s = self.cfg.sweep
        return np.linspace(s.start_Hz, s.stop_Hz, int(s.points))

    # ---- status ---------------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        c = self.cfg
        sw, tg, b = c.sweep, c.tracking, c.bench
        want = model.resolve(c)            # pure arithmetic, no hardware
        with self._lock:
            a, rb = self._acq, self._readback
            # the instrument's readback counts only if it belongs to THESE settings
            if self._applied != want:
                rb = {}
            last = self._last or {}
            progress = 0.0
            if self._sweeping and self._sweep_dt > 0:
                progress = min(1.0, (self._clock() - self._sweep_t0) / self._sweep_dt)
            acq_progress = 0.0
            if a is not None:
                acq_progress = min(1.0, (a["n"] + progress * self._sweeping) / a["want"])
            sim = self.simulated
            return Status(
                connected=self._connected, idn=self._idn, hw_error=self._hw_error,
                simulated=sim,
                start_Hz=sw.start_Hz, stop_Hz=sw.stop_Hz,
                center_Hz=(sw.start_Hz + sw.stop_Hz) / 2.0, span_Hz=sw.stop_Hz - sw.start_Hz,
                points=int(sw.points),
                rbw_Hz=rb.get("rbw_Hz", want.rbw_Hz), rbw_set_Hz=sw.rbw_Hz, rbw_auto=sw.rbw_auto,
                vbw_Hz=rb.get("vbw_Hz", want.vbw_Hz), vbw_set_Hz=sw.vbw_Hz, vbw_auto=sw.vbw_auto,
                ref_level_dBm=sw.ref_level_dBm,
                atten_dB=rb.get("atten_dB", want.atten_dB), atten_set_dB=sw.atten_dB,
                atten_auto=sw.atten_auto,
                sweep_time_s=rb.get("sweep_time_s", want.sweep_time_s),
                sweep_time_set_s=sw.sweep_time_s, sweep_time_auto=sw.sweep_time_auto,
                detector=sw.detector,
                detector_in_use=model.effective_detector(sw.detector, want.span_Hz),
                preamp=bool(sw.preamp), averages=int(sw.averages),
                continuous=c.acquisition.continuous,
                tg_on=bool(tg.tg_on), tg_level_dBm=tg.level_dBm,
                sweeping=self._sweeping, sweep_progress=progress,
                sweeps=self._sweeps, trace_id=self._trace_id,
                peak_Hz=last.get("peak_Hz", _NAN), peak_dBm=last.get("peak_dBm", _NAN),
                floor_dBm=last.get("floor_dBm", _NAN), overload=bool(last.get("overload", False)),
                dut=b.dut if sim else "",
                dut_center_Hz=b.dut_center_Hz if sim else _NAN,
                dut_bw_Hz=b.dut_bw_Hz if sim else _NAN,
                dut_order=int(b.dut_order) if sim else 0,
                dut_loss_dB=b.dut_loss_dB if sim else _NAN,
                dut_isolation_dB=b.dut_isolation_dB if sim else _NAN,
                cable_loss_dB_at_1GHz=b.cable_loss_dB_at_1GHz if sim else _NAN,
                tg_ripple_dB=b.tg_ripple_dB if sim else _NAN,
                acq_id=self._acq_id, acquiring=a is not None, acq_progress=acq_progress,
                acq_is_reference=bool(a is not None and a["reference"]),
                sample=dict(self._sample),
                reference=self._reference_status_locked(),
            )

    def _reference_status_locked(self) -> dict:
        r = self._reference
        if r is None:
            return _no_reference()
        return {"present": True, "acq_id": r["acq_id"], "tg_on": r["tg_on"],
                "tg_level_dBm": r["tg_level_dBm"], "start_Hz": r["start_Hz"],
                "stop_Hz": r["stop_Hz"], "points": r["points"],
                "age_s": self._clock() - r["taken_at"]}

    # ---- config (Settings dialog / wire) ------------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp cfg (possibly edited in place over the wire)."""
        self._sanitise_config()
        self._changed("settings applied", False)

    # ---- the sweep thread -------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()

    def step(self) -> bool:
        """One pass of the sweep thread: push changed settings to the backend
        (even when idle -- switching the tracking generator OFF must not wait
        for a sweep), then one sweep if there is a reason to sweep. Returns True
        if a sweep finished. Public so tests can drive it without the thread."""
        try:
            # The revision is read BEFORE the settings are resolved and pushed.
            # If it were read after (inside _sweep_once), a setter landing
            # between "configure" and "sweep" would leave the backend on the OLD
            # settings while the sweep believed itself current -- and a restarted
            # acquisition would average that old-settings sweep in. Read first,
            # any later change bumps _rev and the sweep is thrown away.
            with self._lock:
                rev0 = self._rev
            self._configure_if_changed()
            with self._lock:
                wanted = self._acq is not None or self.cfg.acquisition.continuous
            if not wanted:
                self._stop.wait(0.1)
                return False
            return self._sweep_once(rev0)
        except Exception as exc:              # never let the sweep thread die
            with self._lock:
                self._sweeping = False
            self._report_hw_error(exc)
            self._stop.wait(0.5)
            return False

    def _configure_if_changed(self) -> model.SweepSettings:
        s = model.resolve(self.cfg)
        with self._lock:
            same = s == self._applied
        if not same:
            with self._hw:
                rb = self.backend.configure(s)
            with self._lock:
                self._applied, self._readback = s, dict(rb or {})
        return s

    def _sweep_once(self, rev0: int) -> bool:
        """One sweep. `rev0` = the settings revision the backend was configured
        for (read by step() before configuring)."""
        with self._lock:
            acq0 = self._acq_id
            s, rb = self._applied, dict(self._readback)
            stale = self._rev != rev0
        if s is None or stale:
            return False
        with self._lock:
            acquiring = self._acq is not None
        if acquiring:
            # An acquisition is an explicit request for a FRESH trace. If the
            # front panel left the trace frozen (View), holding (Max Hold) or
            # averaged by the instrument, reading it would not be one; the
            # backend fixes that now -- never at start-up -- and says what it
            # changed.
            with self._hw:
                notes = self.backend.ensure_live()
            for note in notes or []:
                self._emit("warn", note)
        t0 = self._clock()
        with self._hw:
            dt = float(self.backend.start_sweep(s))
        with self._lock:
            self._sweeping, self._sweep_t0, self._sweep_dt = True, t0, dt

        # Wait out the sweep WITHOUT the hardware lock. Abandon it the moment it
        # can no longer count: settings changed (the trace would be neither), or
        # a new trigger arrived (the running sweep started before it).
        while True:
            left = t0 + dt - self._clock()
            with self._lock:
                abandoned = self._rev != rev0 or self._acq_id != acq0
            if self._stop.is_set() or abandoned:
                with self._hw:
                    self._discard_pending()
                with self._lock:
                    self._sweeping = False
                return False
            if left <= 0:
                break
            self._stop.wait(min(0.02, left))

        with self._hw:
            y, meta = self.backend.finish_sweep()
        y = np.asarray(y, dtype=float)
        freqs = s.freqs()
        peak_Hz, peak_dBm = model.find_peak(freqs, y)
        trace = {"start_Hz": s.start_Hz, "stop_Hz": s.stop_Hz, "points": int(s.points),
                 "rbw_Hz": rb.get("rbw_Hz", s.rbw_Hz), "vbw_Hz": rb.get("vbw_Hz", s.vbw_Hz),
                 "atten_dB": rb.get("atten_dB", s.atten_dB),
                 "sweep_time_s": rb.get("sweep_time_s", s.sweep_time_s),
                 "ref_level_dBm": s.ref_level_dBm, "preamp": s.preamp,
                 "detector": model.effective_detector(s.detector, s.span_Hz),
                 "tg_on": s.tg_on, "tg_level_dBm": s.tg_level_dBm,
                 "power_dBm": y, "peak_Hz": peak_Hz, "peak_dBm": peak_dBm,
                 "floor_dBm": model.noise_floor(y),
                 "overload": bool(meta.get("overload", False)), "time": time.time()}

        latched = None
        with self._lock:
            self._sweeping = False
            if self._rev != rev0:
                return False                  # changed during the final read-out
            self._hw_error = ""
            self._sweeps += 1
            self._trace_id += 1
            self._last = {**trace, "trace_id": self._trace_id}
            a = self._acq
            if a is not None and t0 >= a["t0"]:
                lin = 10.0 ** (y / 10.0)
                a["sum"] = lin if a["sum"] is None else a["sum"] + lin
                a["n"] += 1
                a["overload"] = a["overload"] or trace["overload"]
                if a["n"] >= a["want"]:
                    # Clear "acquiring", publish the sample AND (for
                    # take_reference) the reference in the SAME critical
                    # section. Split, a status snapshot could land in between
                    # saying "#n finished" while `sample` or `reference` is
                    # still the old one (suite gotcha #28).
                    self._latch_locked(a, trace)
                    self._acq = None
                    latched = a
        if latched is not None:
            if latched["overload"]:
                self._emit("warn", f"acquisition #{latched['id']}: the input mixer was "
                                   "overloaded -- raise the attenuation or the reference level")
            if latched["reference"]:
                self._emit("info", f"reference taken (#{latched['id']})")
        return True

    def _latch_locked(self, a: dict, last: dict) -> None:
        """Average an acquisition's sweeps (in linear power) and publish them as
        THE sample (and as the reference, if that is what it was for). Called
        with _lock held."""
        with np.errstate(divide="ignore"):
            mean_dBm = 10.0 * np.log10(a["sum"] / a["n"])
        freqs = np.linspace(last["start_Hz"], last["stop_Hz"], last["points"])
        peak_Hz, peak_dBm = model.find_peak(freqs, mean_dBm)
        self._sample = {
            "acq_id": a["id"], "time": time.time(), "averages": a["n"],
            "reference": bool(a["reference"]),
            **{k: last[k] for k in ("start_Hz", "stop_Hz", "points", "rbw_Hz", "vbw_Hz",
                                    "atten_dB", "sweep_time_s", "ref_level_dBm", "preamp",
                                    "detector", "tg_on", "tg_level_dBm")},
            "peak_Hz": peak_Hz, "peak_dBm": peak_dBm,
            "floor_dBm": model.noise_floor(mean_dBm),
            "overload": bool(a["overload"]),
        }
        self._sample_trace = {**self._sample, "power_dBm": mean_dBm}
        if a["reference"]:
            self._reference = {**self._sample_trace, "taken_at": self._clock()}

    # ---- internals ----------------------------------------------------------------------------

    def _trigger(self, reference: bool) -> int:
        if not self._connected:
            raise ValueError("not connected")
        cleared = False
        with self._lock:
            # A new trigger abandons a running acquisition. If that one was a
            # take_reference, the reference it was meant to replace is now
            # stale by intent: clear it (see abort()).
            old = self._acq
            if old is not None and old["reference"] and self._reference is not None:
                self._reference, cleared = None, True
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can ever show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = self._new_acq(self._acq_id, reference)
            n = self._acq_id
        if cleared:
            self._emit("warn", "reference cleared: taking it was interrupted by a new trigger")
        return n

    def _new_acq(self, acq_id: int, reference: bool) -> dict:
        return {"id": acq_id, "t0": self._clock(), "want": max(1, int(self.cfg.sweep.averages)),
                "n": 0, "sum": None, "overload": False, "reference": bool(reference)}

    def _changed(self, msg: str, clamped: bool) -> None:
        """A trace-altering change: bump the revision and restart a running
        acquisition from scratch under the new settings (a take_reference stays
        a take_reference)."""
        restarted = None
        with self._lock:
            self._rev += 1
            if self._acq is not None:
                restarted = self._acq["id"]
                self._acq = self._new_acq(restarted, self._acq["reference"])
        self._emit("warn" if clamped else "info", msg + (" (clamped)" if clamped else ""))
        if restarted is not None:
            self._emit("warn", f"acquisition #{restarted} restarted: settings changed")

    def _discard_pending(self) -> None:
        try:
            self.backend.abort_sweep()
        except Exception:
            pass

    def _sanitise_config(self) -> None:
        """Clamp everything a hand-edited .ini or a set_config could have put
        out of range -- silently, since it is not a user action to warn about."""
        sw, lim, tg = self.cfg.sweep, self.cfg.limits, self.cfg.tracking
        sw.stop_Hz = _clamp(float(sw.stop_Hz), lim.freq_min_Hz + lim.min_span_Hz, lim.freq_max_Hz)[0]
        sw.start_Hz = _clamp(float(sw.start_Hz), lim.freq_min_Hz, sw.stop_Hz - lim.min_span_Hz)[0]
        sw.points = int(_clamp(int(sw.points), lim.points_min, lim.points_max)[0])
        sw.rbw_Hz = _clamp(float(sw.rbw_Hz), lim.rbw_min_Hz, lim.rbw_max_Hz)[0]
        sw.vbw_Hz = _clamp(float(sw.vbw_Hz), lim.vbw_min_Hz, lim.vbw_max_Hz)[0]
        sw.ref_level_dBm = _clamp(float(sw.ref_level_dBm), lim.ref_level_min_dBm,
                                  lim.ref_level_max_dBm)[0]
        sw.atten_dB = float(round(_clamp(float(sw.atten_dB), 0.0, lim.atten_max_dB)[0]))
        sw.sweep_time_s = _clamp(float(sw.sweep_time_s), lim.sweep_time_min_s,
                                 lim.sweep_time_max_s)[0]
        sw.averages = int(_clamp(int(sw.averages), lim.averages_min, lim.averages_max)[0])
        sw.detector = str(sw.detector).lower()
        if sw.detector not in DETECTORS:
            sw.detector = "auto"
        for name in ("rbw_auto", "vbw_auto", "atten_auto", "sweep_time_auto", "preamp"):
            setattr(sw, name, bool(getattr(sw, name)))
        tg.tg_on = bool(tg.tg_on)
        tg.level_dBm = _clamp(float(tg.level_dBm), lim.tg_level_min_dBm, lim.tg_level_max_dBm)[0]
        b = self.cfg.bench
        for name, (lo, hi) in BENCH_LIMITS.items():
            setattr(b, name, _clamp(float(getattr(b, name)), lo, hi)[0])
        b.dut_order = int(round(b.dut_order))
        if b.dut not in DUTS:
            b.dut = "bandpass"
        try:
            model.parse_carriers(b.carriers)
        except ValueError:
            b.carriers = ""

    def _report_hw_error(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._hw_error = msg
        now = self._clock()
        if now - self._last_err_emit >= 5.0:      # rate-limit: one event per 5 s
            self._last_err_emit = now
            self._emit("error", f"sweep failed: {msg}")

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)


def _reference_mismatch(trace: dict, ref: dict) -> list[str]:
    """What makes `ref` unusable for `trace`, in words; [] if it matches.

    Only what changes the MEANING of the subtraction counts: another grid
    subtracts point i from a different frequency; a trace or a reference
    without the tracking generator is not a transmission measurement; another
    generator level shifts every point by the difference. RBW, VBW and the
    reference level change the noise and the screen, not the meaning."""
    out = []
    if not ref.get("tg_on"):
        out.append("the reference was taken with the tracking generator off")
    if not trace.get("tg_on"):
        out.append("this trace was taken with the tracking generator off")
    for key, label in (("start_Hz", "start"), ("stop_Hz", "stop")):
        a, b = float(trace.get(key, _NAN)), float(ref.get(key, _NAN))
        if not math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-3):
            out.append(f"{label} {_fmt_Hz(a)} vs reference {_fmt_Hz(b)}")
    if int(trace.get("points", -1)) != int(ref.get("points", -2)):
        out.append(f"{trace.get('points')} points vs reference {ref.get('points')}")
    a, b = float(trace.get("tg_level_dBm", _NAN)), float(ref.get("tg_level_dBm", _NAN))
    if trace.get("tg_on") and ref.get("tg_on") and not math.isclose(a, b, abs_tol=1e-9):
        out.append(f"TG level {a:g} dBm vs reference {b:g} dBm")
    return out
