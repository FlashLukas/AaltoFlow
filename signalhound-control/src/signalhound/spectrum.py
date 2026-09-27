"""SpectrumAnalyzer: the brain between the wire and the backend (simulated or real).

Its SETTINGS (centre, span, reference level, RBW, VBW, detector, averages,
tracking generator on/level/points) are set-and-forget: clamp, store, report.
Its MEASUREMENT is a trace, and a scan must never record a trace that was
swept before the scan step it is filed under -- the fire-and-forget contract
makes that easy to get wrong, and nothing raises when it happens.

Two ways to get a trace (the suite's rules for slow detectors):

  continuous  the analyser sweeps on its own, like a front panel; the latest
              trace is `get_trace("last")`. Right for looking at it.

  acquire     the scan-safe read. `acquire()` returns an id at once. The sweep
              thread then averages the next `sweep.averages` sweeps that
              STARTED after the trigger and latches the mean as the sample:
              `get_trace("sample")` + `status().sample`. Callers wait until
              status shows `acq_id` == their id AND `acquiring` False.

Averaging is done in linear POWER (mW), not in dB: averaging dB values of
noise reads ~2.5 dB low, and a spectrum analyser's "power average" is what
people expect.

A settings change in the middle of an acquisition RESTARTS it: averaging a
sweep at 100 kHz RBW with one at 1 kHz is not a measurement of anything.

THE GRID. The analyser -- not we -- decides the frequency bins (from span and
RBW, or the TG point count). The brain re-`configure`s the backend when a
setting changed and keeps the Grid it returns; `frequencies()` gives it to a
scan, and every trace carries start/bin/points so it can be rebuilt exactly.

THE THRU REFERENCE (tracking-generator mode). Transmission is measured as

        T(f) [dB] = P_dut(f) - P_thru(f)

with P_thru a sweep taken with the device under test replaced by a thru. The
TG's output flatness and the cable loss cancel; the device remains. The brain
owns the reference, so every client (GUI, console, scan) uses the SAME one:
  take_reference   an acquisition exactly like `acquire` whose sample is ALSO
                   stored as the reference -- in the same critical section that
                   latches the sample (gotcha #28).
  get_trace(quantity="transmission")  refused, with a message saying what
                   differs, with no reference or one taken on another grid or
                   TG level.

THE MODEL. What is connected (SA44B: 1 Hz - 4.4 GHz, RBW up to 250 kHz;
SA124B: 100 kHz - 12.4 GHz, up to 6 MHz) is read when the analyser opens, and
the frequency envelope follows it; with the tracking generator on it narrows to
the TG44A's 10 Hz - 4.4 GHz. describe's limits (and so its revision) follow.

Threads and locks (the pm16/hf2/vna rules):
  * ONE sweep thread talks to the backend. `status()` only copies what it
    stored and never touches the hardware (gotcha #1).
  * Every backend call runs under `_hw`, but the WAIT during a sweep does not.
  * Safety: the tracking generator is off at start (whatever the config said)
    and the backend is aborted and closed on shutdown or a crash.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field, fields

import numpy as np

from . import physics
from .config import Config, Scene
from .instruments import (DETECTORS, Grid, SweepSettings, TG_RANGE_HZ, model_range,
                          snap_rbw)

_NAN = float("nan")

#: Sanity envelope for the simulated scene, name -> (min, max). Loose on
#: purpose: it stops nonsense (a negative bandwidth), not experiments.
SCENE_LIMITS = {
    "tone_Hz": (1.0, 13e9),
    "tone_dBm": (-150.0, 20.0),
    "harmonic_dBc": (-120.0, 0.0),
    "danl_dBm_per_Hz": (-180.0, -100.0),
    "dut_center_Hz": (1e3, 13e9),
    "dut_bandwidth_Hz": (1e3, 5e9),
    "dut_order": (1, 8),
    "dut_loss_dB": (0.0, 40.0),
    "cable_loss_dB_at_1GHz": (0.0, 20.0),
    "tg_ripple_dB": (0.0, 5.0),
}
SCENE_BOOLS = ("tone_on", "dut_inserted", "tg_attached")


def _no_reference() -> dict:
    """The `reference` status block when there is none. Same keys as a present
    one (NaN -> null on the wire), so a client never has to test for a key."""
    return {"present": False, "acq_id": 0, "start_Hz": _NAN, "stop_Hz": _NAN,
            "points": 0, "tg_level_dBm": _NAN, "age_s": _NAN}


@dataclass
class Status:
    """One snapshot of the analyser, for status() and the wire. No arrays: the
    status goes out ten times a second; traces are fetched with get_trace."""

    connected: bool
    idn: str = ""
    hw_error: str = ""
    simulated: bool = True
    device_model: str = ""
    tg_attached: bool = False
    # the envelope in force now (model, limits, TG)
    freq_min_Hz: float = _NAN
    freq_max_Hz: float = _NAN
    # settings
    center_Hz: float = _NAN
    span_Hz: float = _NAN
    start_Hz: float = _NAN
    stop_Hz: float = _NAN
    ref_level_dBm: float = _NAN
    rbw_Hz: float = _NAN
    vbw_Hz: float = _NAN
    reject: bool = True
    detector: str = "average"
    averages: int = 1
    tg_on: bool = False
    tg_level_dBm: float = _NAN
    tg_points: int = 0
    continuous: bool = True
    # the grid the analyser uses for the configured settings
    points: int = 0
    bin_Hz: float = _NAN
    grid_start_Hz: float = _NAN
    sweep_time_s: float = _NAN
    # what the sweep thread is doing
    sweeping: bool = False
    sweep_progress: float = 0.0
    sweeps: int = 0
    trace_id: int = 0
    peak_Hz: float = _NAN              # of the latest sweep
    peak_dBm: float = _NAN
    floor_dBm: float = _NAN            # median of the latest sweep: the noise floor
    overload: bool = False
    # acquisition
    acq_id: int = 0
    acquiring: bool = False
    acq_progress: float = 0.0
    acq_is_reference: bool = False
    sample: dict = field(default_factory=dict)
    reference: dict = field(default_factory=_no_reference)
    # the simulated scene (empty on a real analyser)
    scene: dict = field(default_factory=dict)


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


def _peak_and_floor(freqs, db) -> tuple[float, float, float]:
    finite = np.isfinite(db)
    if not finite.any():
        return _NAN, _NAN, _NAN
    i = int(np.nanargmax(db))
    return float(freqs[i]), float(db[i]), float(np.nanmedian(db))


class SpectrumAnalyzer:
    def __init__(self, backend, cfg: Config | None = None, clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards the snapshot, traces, acquisition

        self._connected = False
        self._idn = ""
        self._model = ""
        self._tg = False
        self._hw_error = ""
        self._last_err_emit = -1e9

        # everything below is written under _lock
        self._rev = 0                       # bumped by any change that alters a trace
        self._configured: SweepSettings | None = None
        self._grid: Grid | None = None
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
        self._reference: dict | None = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    @property
    def simulated(self) -> bool:
        return bool(getattr(self.backend, "simulated", True))

    # ---- lifecycle -----------------------------------------------------------------

    def start(self, run: bool = True) -> None:
        """Open the analyser and start the sweep thread.

        `run=False` skips the thread, so a test can drive `step()` by hand."""
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            self._model = self.backend.device_model()
            self._tg = bool(self.backend.tg_attached())
        # SAFETY: never come up emitting. Whatever the saved config said, the
        # tracking generator starts OFF; turning it on is a deliberate act.
        if self.cfg.tracking.on:
            self.cfg.tracking.on = False
            self._emit("info", "tracking generator off at start (turn it on deliberately)")
        self._sanitise_config()
        self._connected = True
        self._emit("info", f"connected: {self._idn}"
                   + ("  + tracking generator" if self._tg else "  (no tracking generator)"))
        try:
            with self._hw:
                self._ensure_configured()
        except Exception as exc:
            self._report_hw_error(exc)
        if run:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="signalhound-sweep",
                                            daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop sweeping, tracking generator off, disconnect. Safe to call twice."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        was = self._connected
        try:
            with self._hw:
                self.backend.close()        # aborts the sweep: the TG stops emitting
        finally:
            self._connected = False
            with self._lock:
                self._acq = None
                self._sweeping = False
                self._configured = None
            if was:
                self._emit("info", "disconnected")

    # ---- the envelope ------------------------------------------------------------

    def freq_range(self) -> tuple[float, float]:
        """The frequencies allowed NOW: config limits, the model, and the TG
        range when the tracking generator is on."""
        lim = self.cfg.limits
        fmin, fmax, _ = model_range(self._model or self.cfg.hardware.model)
        lo, hi = max(lim.freq_min_Hz, fmin), min(lim.freq_max_Hz, fmax)
        if self.cfg.tracking.on:
            lo, hi = max(lo, TG_RANGE_HZ[0]), min(hi, TG_RANGE_HZ[1])
        return lo, hi

    def rbw_max(self) -> float:
        _, _, rmax = model_range(self._model or self.cfg.hardware.model)
        return min(self.cfg.limits.rbw_max_Hz, rmax)

    def center_limits(self) -> tuple[float, float]:
        lo, hi = self.freq_range()
        ms = self.cfg.limits.min_span_Hz
        return lo + ms / 2, hi - ms / 2

    def span_limits(self) -> tuple[float, float]:
        """The widest span that still fits around the CURRENT centre."""
        lo, hi = self.freq_range()
        c = self.cfg.sweep.center_Hz
        ms = self.cfg.limits.min_span_Hz
        return ms, max(ms, 2 * min(c - lo, hi - c))

    # ---- sweep settings (each clamps, stores in cfg, reports) --------------------

    def set_center(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "centre"), *self.center_limits())
        self.cfg.sweep.center_Hz = v
        msg = f"centre {v / 1e9:.9g} GHz"
        sp, sc = _clamp(self.cfg.sweep.span_Hz, *self.span_limits())
        if sc:
            self.cfg.sweep.span_Hz = sp
            msg += f"; span narrowed to {sp / 1e6:.6g} MHz to stay in range"
        self._changed(msg, clamped or sc)

    def set_span(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "span"), *self.span_limits())
        self.cfg.sweep.span_Hz = v
        self._changed(f"span {v / 1e6:.6g} MHz", clamped)

    def set_start_stop(self, start_Hz: float, stop_Hz: float) -> None:
        """Start/stop, for those who think that way: stored as centre + span."""
        a, b = _finite(start_Hz, "start"), _finite(stop_Hz, "stop")
        if b <= a:
            raise ValueError(f"stop ({b:g} Hz) must be above start ({a:g} Hz)")
        lo, hi = self.freq_range()
        a2, b2 = max(a, lo), min(b, hi)
        clamped = (a2, b2) != (a, b)
        ms = self.cfg.limits.min_span_Hz
        if b2 - a2 < ms:
            a2, b2, clamped = (a2 + b2) / 2 - ms / 2, (a2 + b2) / 2 + ms / 2, True
        self.cfg.sweep.center_Hz = _clamp((a2 + b2) / 2, *self.center_limits())[0]
        self.cfg.sweep.span_Hz = _clamp(b2 - a2, *self.span_limits())[0]
        self._changed(f"{a2 / 1e9:.9g} - {b2 / 1e9:.9g} GHz", clamped)

    def set_ref_level(self, dbm: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(dbm, "reference level"), lim.ref_min_dBm, lim.ref_max_dBm)
        self.cfg.sweep.ref_level_dBm = v
        self._changed(f"reference level {v:g} dBm", clamped)

    def set_rbw(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "RBW"), self.cfg.limits.rbw_min_Hz, self.rbw_max())
        snapped = snap_rbw(v, self.rbw_max())
        clamped = clamped or not math.isclose(snapped, v, rel_tol=1e-9)
        self.cfg.sweep.rbw_Hz = snapped
        msg = f"RBW {snapped:g} Hz"
        if self.cfg.sweep.vbw_Hz > snapped:
            # VBW <= RBW is a rule of the API; a narrower RBW drags the VBW down
            self.cfg.sweep.vbw_Hz = snapped
            msg += f" (VBW follows to {snapped:g} Hz)"
        self._changed(msg, clamped)

    def set_vbw(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "VBW"), self.cfg.limits.rbw_min_Hz,
                            self.cfg.sweep.rbw_Hz)
        self.cfg.sweep.vbw_Hz = v
        self._changed(f"VBW {v:g} Hz" + (" (VBW cannot exceed RBW)" if v < hz else ""), clamped)

    def set_reject(self, on: bool) -> None:
        self.cfg.sweep.reject = bool(on)
        self._changed("image rejection " + ("on" if on else "off"), False)

    def set_detector(self, detector: str) -> None:
        detector = str(detector).lower()
        if detector not in DETECTORS:
            raise ValueError(f"detector must be one of {DETECTORS}, got {detector!r}")
        self.cfg.sweep.detector = detector
        self._changed(f"{detector} detector", False)

    def set_averages(self, n: int) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(int(round(_finite(n, "averages"))), lim.averages_min, lim.averages_max)
        self.cfg.sweep.averages = int(v)
        self._changed(f"{int(v)} averages per acquisition", clamped)

    def set_continuous(self, on: bool) -> None:
        self.cfg.acquisition.continuous = bool(on)
        self._emit("info", "continuous sweep on" if on else "sweep on trigger only")

    # ---- the tracking generator -------------------------------------------------

    def set_tg(self, on: bool) -> None:
        """Tracking generator on (TG sweep mode) or off (plain spectrum).

        Turning it on narrows the frequency envelope to the TG44A's range, so
        the window is pulled inside it -- announced, never silent."""
        on = bool(on)
        if on and not self._tg:
            raise ValueError("no tracking generator attached to this analyser")
        self.cfg.tracking.on = on
        msg = "tracking generator " + ("ON (transmission sweep)" if on else "off")
        c0, s0 = self.cfg.sweep.center_Hz, self.cfg.sweep.span_Hz
        self._fit_window()
        moved = (c0, s0) != (self.cfg.sweep.center_Hz, self.cfg.sweep.span_Hz)
        if moved:
            msg += (f"; window moved into the TG range: {self.cfg.sweep.center_Hz / 1e9:.6g} GHz "
                    f"+- {self.cfg.sweep.span_Hz / 2e6:.6g} MHz")
        self._changed(msg, moved)

    def set_tg_level(self, dbm: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(dbm, "TG level"), lim.tg_level_min_dBm, lim.tg_level_max_dBm)
        self.cfg.tracking.level_dBm = v
        self._changed(f"TG level {v:g} dBm", clamped)

    def set_tg_points(self, n: int) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(int(round(_finite(n, "TG points"))), lim.tg_points_min,
                            lim.tg_points_max)
        self.cfg.tracking.points = int(v)
        self._changed(f"{int(v)} TG sweep points requested", clamped)

    # ---- the simulated scene --------------------------------------------------------

    def set_scene(self, name: str, value) -> None:
        """Change the simulated world. Not a trace-altering SETTING -- like
        swapping a cable in the lab, it does not restart an acquisition."""
        if not self.simulated:
            raise ValueError("the scene exists only in the simulator")
        if name in SCENE_BOOLS:
            v = value if isinstance(value, bool) else str(value).strip().lower() in (
                "1", "1.0", "true", "yes", "on")
            setattr(self.cfg.scene, name, bool(v))
            if name == "tg_attached":
                self._tg = bool(v) and bool(self.backend.tg_attached())
                if not self._tg and self.cfg.tracking.on:
                    self.set_tg(False)
            self._emit("info", f"scene {name} = {bool(v)}")
            return
        if name not in SCENE_LIMITS:
            raise ValueError(f"unknown scene parameter {name!r}; one of "
                             f"{', '.join(list(SCENE_LIMITS) + list(SCENE_BOOLS))}")
        v, clamped = _clamp(_finite(value, name), *SCENE_LIMITS[name])
        if name == "dut_order":
            v = int(round(v))
        setattr(self.cfg.scene, name, v)
        self._emit("warn" if clamped else "info",
                   f"scene {name} = {v:g}" + (" (clamped)" if clamped else ""))

    # ---- the scan-safe read ------------------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its id immediately. The clock starts
        NOW: call it after everything the measurement depends on has been set."""
        return self._trigger(reference=False)

    def take_reference(self) -> int:
        """Start an acquisition that becomes THE thru reference when it
        completes. Only in tracking mode: a reference of a spectrum is not a thru."""
        if not self.cfg.tracking.on:
            raise ValueError("a thru reference needs the tracking generator on (set_tg)")
        return self._trigger(reference=True)

    def clear_reference(self) -> None:
        with self._lock:
            had = self._reference is not None
            self._reference = None
        if had:
            self._emit("info", "reference cleared")

    def abort(self) -> None:
        """Cancel a running acquisition. It is latched as ABORTED rather than
        dropped: a caller waiting for "acq_id == n and not acquiring" would
        otherwise see exactly that and read the PREVIOUS trace. Aborting a
        take_reference also clears the old reference (a routine that asked
        for a new one must not carry on with an old one)."""
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

    def get_trace(self, which: str = "sample", quantity: str = "trace") -> dict:
        """A trace and what it was measured under.

        which    "sample" (the latched acquisition, what a scan records), "last"
                 (the newest sweep of any kind) or "reference".
        quantity "trace"         the power per bin in dBm (key "trace")
                 "transmission"  trace - thru reference, in dB (key "transmission")

        Raises ValueError when there is nothing honest to return."""
        if quantity not in ("trace", "transmission"):
            raise ValueError(f"quantity must be 'trace' or 'transmission', got {quantity!r}")
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
        if quantity == "transmission":
            if ref is None:
                raise ValueError("transmission needs a thru reference and there is none: "
                                 "take one first (take_reference, tracking generator on)")
            diffs = _reference_mismatch(t, ref)
            if diffs:
                raise ValueError("transmission refused: the reference does not match this "
                                 "trace (" + "; ".join(diffs) + "). Take a new reference.")
            # The arrays are never modified after they are published, so the
            # subtraction can run outside the lock.
            t["transmission"] = t.pop("trace") - ref["trace"]
            t["reference_acq_id"] = ref["acq_id"]
            t["reference_age_s"] = now - ref["taken_at"]
        t["freqs_Hz"] = Grid(t["start_Hz"], t["bin_Hz"], int(t["points"])).freqs()
        return t

    def frequencies(self) -> np.ndarray:
        """The bins the NEXT sweep will use (a scan reads it once, before
        starting). Configures the analyser first if a setting changed: the grid
        is the analyser's answer to the settings, not something we can compute."""
        with self._hw:
            self._ensure_configured()
        with self._lock:
            g = self._grid
        if g is None:
            raise ValueError("the analyser has not reported a frequency grid")
        return g.freqs()

    # ---- status ------------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        c = self.cfg
        sw, tg = c.sweep, c.tracking
        lo, hi = self.freq_range()
        settings = SweepSettings.from_config(c)
        with self._lock:
            a, g = self._acq, self._grid
            last = self._last or {}
            progress = 0.0
            if self._sweeping and self._sweep_dt > 0:
                progress = min(1.0, (self._clock() - self._sweep_t0) / self._sweep_dt)
            acq_progress = 0.0
            if a is not None:
                acq_progress = min(1.0, (a["n"] + progress * self._sweeping) / a["want"])
            points = g.points if g is not None else 0
            return Status(
                connected=self._connected, idn=self._idn, hw_error=self._hw_error,
                simulated=self.simulated, device_model=self._model, tg_attached=self._tg,
                freq_min_Hz=lo, freq_max_Hz=hi,
                center_Hz=sw.center_Hz, span_Hz=sw.span_Hz,
                start_Hz=sw.center_Hz - sw.span_Hz / 2, stop_Hz=sw.center_Hz + sw.span_Hz / 2,
                ref_level_dBm=sw.ref_level_dBm, rbw_Hz=sw.rbw_Hz, vbw_Hz=sw.vbw_Hz,
                reject=bool(sw.reject), detector=sw.detector, averages=int(sw.averages),
                tg_on=bool(tg.on), tg_level_dBm=tg.level_dBm, tg_points=int(tg.points),
                continuous=bool(c.acquisition.continuous),
                points=int(points), bin_Hz=g.bin_Hz if g else _NAN,
                grid_start_Hz=g.start_Hz if g else _NAN,
                sweep_time_s=self.backend.sweep_time_s(settings, max(points, 2)),
                sweeping=self._sweeping, sweep_progress=progress,
                sweeps=self._sweeps, trace_id=self._trace_id,
                peak_Hz=last.get("peak_Hz", _NAN), peak_dBm=last.get("peak_dBm", _NAN),
                floor_dBm=last.get("floor_dBm", _NAN), overload=bool(last.get("overload", False)),
                acq_id=self._acq_id, acquiring=a is not None, acq_progress=acq_progress,
                acq_is_reference=bool(a is not None and a["reference"]),
                sample=dict(self._sample),
                reference=self._reference_status_locked(),
                scene=({f.name: getattr(c.scene, f.name) for f in fields(Scene)}
                       if self.simulated else {}),
            )

    def _reference_status_locked(self) -> dict:
        r = self._reference
        if r is None:
            return _no_reference()
        return {"present": True, "acq_id": r["acq_id"], "start_Hz": r["start_Hz"],
                "stop_Hz": r["stop_Hz"], "points": r["points"],
                "tg_level_dBm": r["tg_level_dBm"], "age_s": self._clock() - r["taken_at"]}

    # ---- config (Settings dialog / wire) ------------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp cfg (possibly edited in place over the wire)."""
        if self.cfg.tracking.on and not self._tg and self._connected:
            self.cfg.tracking.on = False
            self._emit("warn", "tracking generator: none attached, left off")
        self._sanitise_config()
        self._changed("settings applied", False)

    # ---- the sweep thread -------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()

    def step(self) -> bool:
        """One pass of the sweep thread: one sweep if there is a reason to
        sweep. Returns True if a sweep finished. Public so tests can drive the
        analyser without the thread."""
        with self._lock:
            wanted = self._acq is not None or self.cfg.acquisition.continuous
        try:
            if not wanted:
                # idle, but keep the reported grid honest after a setter
                with self._hw:
                    self._ensure_configured()
                self._stop.wait(0.1)
                return False
            return self._sweep_once()
        except Exception as exc:              # never let the sweep thread die
            with self._lock:
                self._sweeping = False
                self._configured = None       # reconfigure from scratch next time
            self._report_hw_error(exc)
            self._stop.wait(0.5)
            return False

    def _ensure_configured(self) -> None:
        """Configure the backend if the settings differ from what it runs.
        Called with _hw held."""
        s = SweepSettings.from_config(self.cfg)
        with self._lock:
            if s == self._configured:
                return
        grid = self.backend.configure(s)
        with self._lock:
            self._configured, self._grid = s, grid

    def _sweep_once(self) -> bool:
        with self._lock:
            rev0, acq0 = self._rev, self._acq_id
        t0 = self._clock()
        with self._hw:
            self._ensure_configured()
            with self._lock:
                settings, grid = self._configured, self._grid
            self.backend.start_sweep()
            dt = self.backend.sweep_time_s(settings, grid.points)
        with self._lock:
            self._sweeping, self._sweep_t0, self._sweep_dt = True, t0, dt
        # A backend that TAKES the sweep inside finish_sweep (the real Signal
        # Hound: saGetSweep sweeps on request) must not be kept waiting here:
        # idling for dt first would double every sweep. dt still drives the
        # progress bar, since `_sweeping` stays True through finish_sweep.
        wait_dt = 0.0 if getattr(self.backend, "sweeps_in_finish", False) else dt

        # Wait out the sweep WITHOUT the hardware lock. Abandon it the moment
        # it can no longer count: settings changed, or a new trigger arrived.
        while True:
            left = t0 + wait_dt - self._clock()
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
            db, meta = self.backend.finish_sweep()
        db = np.asarray(db, dtype=float)
        if db.size != grid.points:
            raise RuntimeError(f"sweep returned {db.size} bins, the grid has {grid.points}")
        pk_f, pk_db, floor = _peak_and_floor(grid.freqs(), db)
        trace = {"start_Hz": grid.start_Hz, "bin_Hz": grid.bin_Hz, "points": grid.points,
                 "stop_Hz": grid.stop_Hz, "center_Hz": settings.center_Hz,
                 "span_Hz": settings.span_Hz, "rbw_Hz": settings.rbw_Hz,
                 "vbw_Hz": settings.vbw_Hz, "ref_level_dBm": settings.ref_level_dBm,
                 "detector": settings.detector, "tg_on": settings.tg_on,
                 "tg_level_dBm": settings.tg_level_dBm if settings.tg_on else _NAN,
                 "trace": db, "peak_Hz": pk_f, "peak_dBm": pk_db, "floor_dBm": floor,
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
                p = physics.dbm_to_mw(db)
                a["sum"] = p if a["sum"] is None else a["sum"] + p
                a["n"] += 1
                a["overload"] = a["overload"] or trace["overload"]
                if a["n"] >= a["want"]:
                    # Clear "acquiring", publish the sample AND (for
                    # take_reference) the reference in the SAME critical section
                    # (gotcha #28): split, a status frame in between would say
                    # "#n finished" while `sample` is still the old one.
                    self._latch_locked(a, trace)
                    self._acq = None
                    latched = a
        if latched is not None:
            if latched["overload"]:
                self._emit("warn", f"acquisition #{latched['id']}: the input OVERLOADED "
                                   "(raise the reference level)")
            if latched["reference"]:
                self._emit("info", f"thru reference taken (#{latched['id']})")
        if trace["overload"] and not latched:
            self._report_overload()
        return True

    def _latch_locked(self, a: dict, last: dict) -> None:
        """Average an acquisition's sweeps (in power) and publish them as THE
        sample (and as the reference, if that is what it was for). Called with
        _lock held."""
        mean = physics.mw_to_dbm(a["sum"] / a["n"])
        freqs = Grid(last["start_Hz"], last["bin_Hz"], last["points"]).freqs()
        pk_f, pk_db, floor = _peak_and_floor(freqs, mean)
        meta = {k: last[k] for k in ("start_Hz", "bin_Hz", "points", "stop_Hz", "center_Hz",
                                     "span_Hz", "rbw_Hz", "vbw_Hz", "ref_level_dBm",
                                     "detector", "tg_on", "tg_level_dBm")}
        self._sample = {"acq_id": a["id"], "time": time.time(), "averages": a["n"],
                        "reference": bool(a["reference"]), **meta,
                        "peak_Hz": pk_f, "peak_dBm": pk_db, "floor_dBm": floor,
                        "overload": bool(a["overload"]), "tx_center_dB": _NAN}
        self._sample_trace = {**self._sample, "trace": mean}
        if a["reference"]:
            self._reference = {**self._sample_trace, "taken_at": self._clock()}
        ref = self._reference
        if ref is not None and not _reference_mismatch(self._sample_trace, ref):
            # transmission at the bin nearest the centre: a scalar a scan can plot
            i = int(np.argmin(np.abs(freqs - last["center_Hz"])))
            tx = float(mean[i] - ref["trace"][i])
            self._sample["tx_center_dB"] = tx
            self._sample_trace["tx_center_dB"] = tx

    # ---- internals ----------------------------------------------------------------------------

    def _trigger(self, reference: bool) -> int:
        if not self._connected:
            raise ValueError("not connected")
        cleared = False
        with self._lock:
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
        acquisition from scratch under the new settings."""
        restarted = None
        with self._lock:
            self._rev += 1
            if self._acq is not None:
                restarted = self._acq["id"]
                self._acq = self._new_acq(restarted, self._acq["reference"])
        self._emit("warn" if clamped else "info", msg + (" (clamped)" if clamped else ""))
        if restarted is not None:
            self._emit("warn", f"acquisition #{restarted} restarted: settings changed")

    def _fit_window(self) -> None:
        """Pull centre and span inside the envelope in force now."""
        sw = self.cfg.sweep
        sw.center_Hz = _clamp(float(sw.center_Hz), *self.center_limits())[0]
        sw.span_Hz = _clamp(float(sw.span_Hz), *self.span_limits())[0]

    def _discard_pending(self) -> None:
        try:
            self.backend.abort_sweep()
        except Exception:
            pass

    def _sanitise_config(self) -> None:
        sw, lim, tg = self.cfg.sweep, self.cfg.limits, self.cfg.tracking
        self._fit_window()
        sw.ref_level_dBm = _clamp(float(sw.ref_level_dBm), lim.ref_min_dBm, lim.ref_max_dBm)[0]
        sw.rbw_Hz = snap_rbw(_clamp(float(sw.rbw_Hz), lim.rbw_min_Hz, self.rbw_max())[0],
                             self.rbw_max())
        sw.vbw_Hz = _clamp(float(sw.vbw_Hz), lim.rbw_min_Hz, sw.rbw_Hz)[0]
        sw.averages = int(_clamp(int(sw.averages), lim.averages_min, lim.averages_max)[0])
        sw.detector = str(sw.detector).lower()
        if sw.detector not in DETECTORS:
            sw.detector = "average"
        tg.level_dBm = _clamp(float(tg.level_dBm), lim.tg_level_min_dBm, lim.tg_level_max_dBm)[0]
        tg.points = int(_clamp(int(tg.points), lim.tg_points_min, lim.tg_points_max)[0])
        for name, (lo, hi) in SCENE_LIMITS.items():
            v = _clamp(float(getattr(self.cfg.scene, name)), lo, hi)[0]
            setattr(self.cfg.scene, name, int(round(v)) if name == "dut_order" else v)

    def _report_overload(self) -> None:
        now = self._clock()
        if now - getattr(self, "_last_over_emit", -1e9) >= 5.0:   # one per 5 s
            self._last_over_emit = now
            self._emit("warn", "input OVERLOAD: signal above the reference level "
                               "(raise the reference level)")

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

    Only what changes the MEANING of the subtraction counts: a spectrum is not
    a transmission sweep, another grid subtracts bin i at a different
    frequency, and another TG level shifts every dB by the difference. RBW and
    the reference level change the noise, not the meaning."""
    out = []
    if not trace.get("tg_on"):
        out.append("this trace is a spectrum, not a tracking-generator sweep")
    if not ref.get("tg_on"):
        out.append("the reference is not a tracking-generator sweep")
    for key, label in (("start_Hz", "first bin"), ("bin_Hz", "bin width")):
        a, b = float(trace.get(key, _NAN)), float(ref.get(key, _NAN))
        if not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-3):
            out.append(f"{label} {a:.9g} Hz vs reference {b:.9g} Hz")
    if int(trace.get("points", -1)) != int(ref.get("points", -2)):
        out.append(f"{trace.get('points')} points vs reference {ref.get('points')}")
    a, b = float(trace.get("tg_level_dBm", _NAN)), float(ref.get("tg_level_dBm", _NAN))
    if trace.get("tg_on") and ref.get("tg_on") and not math.isclose(a, b, abs_tol=1e-6):
        out.append(f"TG level {a:g} dBm vs reference {b:g} dBm")
    return out
