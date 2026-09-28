"""The Analyzer: the brain between the wire and the backend (simulated or the
signalhound service).

Its SETTINGS (start, stop, points, RBW, averages) are set-and-forget: clamp,
store, report. Its MEASUREMENT is a TG sweep -- the power that came through the
device under test at each frequency, in dB RELATIVE TO THE TG OUTPUT (the
unit the TG44A reports; there is no dBm and no settable level in TG sweep
mode) -- and a scan must never record a sweep
that was made before the scan step it is filed under. The fire-and-forget
contract makes that easy to get wrong, and nothing raises when it happens.

Two ways to get a trace:

  continuous  sweep on its own, like a front panel; the latest trace is
              `get_trace(..., source="last")`. Off by default: a TG sweep is
              exclusive on the analyser (the owner pauses its spectrum display
              and any signal-generator CW output while it runs).

  acquire     the scan-safe read. `acquire()` returns an id at once. The sweep
              thread then runs ONE acquisition that STARTED after the trigger
              (a sweep already running is thrown away) and latches it as the
              sample. Callers wait until status shows `acq_id` == their id AND
              `acquiring` False -- and then look at `acq_error`: "" means the
              sample is a measurement, anything else is why it is not.

A settings change in the middle of an acquisition RESTARTS it: a trace swept
from 1 to 2 GHz is not a measurement of the 1-3 GHz band that was asked for.

A FAILED acquisition (owner refused, TG lost, owner gone, aborted) is LATCHED
as failed, never silently dropped: a caller waiting for "acq_id == n and not
acquiring" would otherwise see exactly that and read the PREVIOUS trace. The
failed sample carries the reason, `acq_error` says it in status, and
get_trace / get_result of that sample raise it.

THE THRU REFERENCE. Transmission is

        T(f) [dB] = P_dut(f) - P_thru(f)          (|S21| in dB)

with P_thru a sweep taken with the DUT replaced by a thru. The TG's output
flatness, the cables and the fixed 20 dB pad cancel; the DUT remains. The
brain owns the reference, so every client (GUI, console, scan) uses the SAME
one:
  take_reference  an acquisition exactly like `acquire` whose sample is ALSO
                  stored as the reference, in the same critical section that
                  latches the sample (gotcha #28).
  transmission    refused, with a message saying what differs, when there is
                  no reference or it was taken on another frequency grid
                  (never subtract a stale reference).

THE GRID is the analyser's, not ours: every trace is filed on the start + bin
* i the backend reports, and the reference comparison is on that grid.

Threads and locks (the pm16/hf2/vna rules):
  * ONE sweep thread talks to the backend. `status()` only copies what it
    stored and never touches the hardware (gotcha #1).
  * Every backend call runs under `_hw`, but the WAIT during a sweep does not.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import physics
from .backends.base import SweepFailed
from .config import Config

_NAN = float("nan")

#: The simulated chain's knobs, name -> (min, max). Loose on purpose: the point
#: is to stop nonsense (a negative bandwidth), not to police the chain.
SIM_LIMITS = {
    "pad_dB": (0.0, 80.0),
    "cable_loss_dB_at_1GHz": (0.0, 20.0),
    "tg_ripple_dB": (0.0, 5.0),
    "floor_dB": (-200.0, -40.0),
    "dut_center_Hz": (10.0, 4.4e9),
    "dut_bandwidth_Hz": (1e3, 4.4e9),
    "dut_order": (1, 10),
    "dut_loss_dB": (0.0, 60.0),
}
SIM_SWITCHES = ("dut_inserted", "tg_attached")
QUANTITIES = ("raw", "transmission", "reference")
SOURCES = ("sample", "last")


def _no_reference() -> dict:
    """The `reference` status block when there is none. Same keys as a present
    one (NaN -> null on the wire), so a client never has to test for a key."""
    return {"present": False, "acq_id": 0, "start_Hz": _NAN, "stop_Hz": _NAN,
            "bin_Hz": _NAN, "points": 0, "age_s": _NAN}


def _no_owner() -> dict:
    return {"address": "", "reachable": False, "tg_attached": False, "tg_mode": "",
            "hw_error": ""}


@dataclass
class Status:
    """One snapshot, for status() and the wire. No arrays: the status goes out
    10 times a second; traces are fetched with get_trace."""

    connected: bool
    idn: str = ""
    hw_error: str = ""
    simulated: bool = True
    owner: dict = field(default_factory=_no_owner)
    # sweep settings
    start_Hz: float = _NAN
    stop_Hz: float = _NAN
    points: int = 0
    rbw_Hz: float = 0.0
    averages: int = 1
    sweep_time_s: float = _NAN
    continuous: bool = False
    # what the sweep thread is doing
    sweeping: bool = False
    sweep_progress: float = 0.0
    sweeps: int = 0                    # completed sweeps since start
    trace_id: int = 0                  # id of the latest trace (any kind)
    last_peak_db: float = _NAN         # the latest trace, raw (dB rel. TG output)
    last_peak_Hz: float = _NAN
    last_peak_transmission_db: float = _NAN   # ... against the reference, if it matches
    # acquisition
    acq_id: int = 0
    acquiring: bool = False
    acq_progress: float = 0.0
    acq_is_reference: bool = False
    acq_error: str = ""                # "" = the last acquisition is a measurement
    sample: dict = field(default_factory=dict)
    reference: dict = field(default_factory=_no_reference)
    # the simulated chain (NaN / False on the real backend)
    sim_dut_inserted: bool = False
    sim_tg_attached: bool = False
    sim_pad_dB: float = _NAN


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


def _freqs(t: dict) -> np.ndarray:
    return t["start_Hz"] + t["bin_Hz"] * np.arange(int(t["points"]))


def _same(a, b, rel=1e-9) -> bool:
    a, b = float(a), float(b)
    return math.isclose(a, b, rel_tol=rel, abs_tol=1e-6)


class Analyzer:
    def __init__(self, backend, cfg: Config | None = None, clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards the snapshot, traces, acquisition

        self._connected = False
        self._idn = ""
        self._last_err_emit = -1e9

        # everything below is written under _lock
        self._health = ""                   # backend.health(), refreshed by the thread
        self._owner: dict = _no_owner()
        self._sweep_error = ""              # why the last sweep attempt failed ("" = it did not)
        self._rev = 0                       # bumped by any change that alters a trace
        self._sweeping = False
        self._sweep_t0 = 0.0
        self._sweep_dt = 0.0
        self._sweeps = 0
        self._trace_id = 0
        self._last: dict | None = None      # latest trace, any kind
        self._last_summary: dict = {}
        self._acq_id = 0
        self._acq: dict | None = None
        self._acq_error = ""
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
        """Connect and start the sweep thread. Writes NOTHING to the analyser
        and starts no sweep (Lukas's rule, 2026-09-27).

        On the real backend `continuous` is forced off: a TG sweep takes the
        analyser away from its spectrum display (and pauses a signal
        generator's CW), so it must be asked for, not implied by starting.

        `run=False` skips the thread, so a test can drive `step()` by hand."""
        self._sanitise_config()
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
        self._connected = True
        if not self.simulated and self.cfg.acquisition.continuous:
            self.cfg.acquisition.continuous = False
            self._emit("info", "continuous sweep left OFF at start: a TG sweep interrupts "
                               "the analyser's other work, switch it on when wanted")
        self._refresh_health()
        self._emit("info", f"connected: {self._idn}")
        if run:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="shsna-sweep", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop sweeping and disconnect. A TG acquisition this module started
        on the owner is aborted (only ours: see RemoteSa.abort), so the
        analyser goes back to its spectrum display at once. Safe to call more
        than once."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
        self._thread = None
        was = self._connected
        try:
            with self._hw:
                try:
                    self.backend.abort()
                finally:
                    self.backend.close()
        finally:
            self._connected = False
            with self._lock:
                self._acq = None
                self._sweeping = False
            if was:
                self._emit("info", "disconnected")

    # ---- sweep settings (each clamps, stores in cfg, reports) ------------------

    def start_limits(self) -> tuple[float, float]:
        lim = self.cfg.limits
        return lim.freq_min_Hz, self.cfg.sweep.stop_Hz - lim.min_span_Hz

    def stop_limits(self) -> tuple[float, float]:
        lim = self.cfg.limits
        return self.cfg.sweep.start_Hz + lim.min_span_Hz, lim.freq_max_Hz

    def set_start(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "start"), *self.start_limits())
        self.cfg.sweep.start_Hz = v
        self._changed(f"start {v / 1e6:.6g} MHz", clamped)

    def set_stop(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "stop"), *self.stop_limits())
        self.cfg.sweep.stop_Hz = v
        self._changed(f"stop {v / 1e6:.6g} MHz", clamped)

    def set_points(self, n: int) -> None:
        """How many points to ASK for; the analyser has the last word (the SA
        API clamps TG sweeps to 1001, silently -- so we clamp first, loudly)."""
        lim = self.cfg.limits
        v, clamped = _clamp(int(round(_finite(n, "points"))), lim.points_min, lim.points_max)
        self.cfg.sweep.points = int(v)
        self._changed(f"{int(v)} points", clamped)

    def set_rbw(self, hz: float) -> None:
        """0 (or less) = leave the RBW to the analyser; otherwise clamped."""
        lim = self.cfg.limits
        v = _finite(hz, "RBW")
        clamped = False
        if v <= 0:
            v = 0.0
        else:
            v, clamped = _clamp(v, lim.rbw_min_Hz, lim.rbw_max_Hz)
        self.cfg.sweep.rbw_Hz = v
        self._changed("RBW " + (f"{v:g} Hz" if v else "auto (the analyser's default)"), clamped)

    def set_averages(self, n: int) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(int(round(_finite(n, "averages"))), lim.averages_min, lim.averages_max)
        self.cfg.sweep.averages = int(v)
        self._changed(f"{int(v)} sweeps averaged (in power) per acquisition", clamped)

    def set_continuous(self, on: bool) -> None:
        self.cfg.acquisition.continuous = bool(on)
        self._emit("info", "continuous TG sweep on" if on else "sweep on trigger only")

    # ---- the simulated chain -----------------------------------------------------------

    def set_sim(self, name: str, value) -> None:
        """Change the SIMULATED chain (simulator only). Deliberately does NOT
        restart an acquisition: inserting the DUT is the physical world
        changing, like a cable being swapped -- and the simulator latches the
        chain when a sweep starts, so no trace is ever half one and half the
        other."""
        if not self.simulated:
            raise ValueError("set_sim changes the SIMULATED chain; this is the real analyser")
        if name in SIM_SWITCHES:
            v = value if isinstance(value, bool) else str(value).strip().lower() in (
                "1", "true", "yes", "on")
            setattr(self.cfg.sim, name, bool(v))
            self._emit("info", f"sim {name} = {bool(v)}")
            return
        if name not in SIM_LIMITS:
            raise ValueError(f"unknown sim parameter {name!r}; one of "
                             f"{', '.join(list(SIM_LIMITS) + list(SIM_SWITCHES))}")
        v, clamped = _clamp(_finite(value, name), *SIM_LIMITS[name])
        if name == "dut_order":
            v = int(round(v))
        setattr(self.cfg.sim, name, v)
        self._emit("warn" if clamped else "info",
                   f"sim {name} = {v:g}" + (" (clamped)" if clamped else ""))

    # ---- the scan-safe read ------------------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its id immediately. The clock starts
        NOW: call it after everything the measurement depends on has been set."""
        return self._trigger(reference=False)

    def take_reference(self) -> int:
        """Start an acquisition that becomes THE thru reference when it
        completes. Returns its id; wait for it exactly as for `acquire`."""
        return self._trigger(reference=True)

    def clear_reference(self) -> None:
        with self._lock:
            had = self._reference is not None
            self._reference = None
        if had:
            self._emit("info", "reference cleared")

    def abort(self) -> None:
        """Cancel a running acquisition; it is latched as failed ("aborted").
        The sweep thread notices and aborts the TG sweep on the owner."""
        with self._lock:
            a = self._acq
            if a is None:
                return
            cleared = self._fail_locked(a, "aborted")
        self._emit("warn", f"acquisition #{a['id']} aborted")
        if cleared:
            self._emit("warn", "reference cleared: taking the new one was aborted")

    def get_sample(self) -> dict:
        with self._lock:
            return dict(self._sample)

    def _pick(self, source: str) -> dict:
        """The trace `source` names, as a copy. Called with _lock held."""
        if source == "last":
            if self._last is None:
                raise ValueError("no sweep finished yet")
            return dict(self._last)
        if source == "sample":
            if self._sample.get("failed"):
                raise ValueError(f"acquisition #{self._sample['acq_id']} failed: "
                                 f"{self._sample.get('error', '')}")
            if self._sample_trace is None:
                raise ValueError("no acquisition latched yet")
            return dict(self._sample_trace)
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")

    def get_trace(self, which: str = "transmission", source: str = "sample") -> dict:
        """A trace and what it was measured under.

        which   "raw"           what the analyser measured, dB rel. TG output (key "raw")
                "transmission"  raw - reference, dB = |S21| in dB (key "transmission")
                "reference"     the thru reference itself, dB (key "reference");
                                `source` does not apply
        source  "sample" (the latched acquisition, what a scan records) or
                "last" (the newest sweep of any kind)

        Raises ValueError when there is nothing honest to return."""
        if which not in QUANTITIES:
            raise ValueError(f"which must be one of {QUANTITIES}, got {which!r}")
        with self._lock:
            ref = self._reference
            if which == "reference":
                if ref is None:
                    raise ValueError("no reference: take one first (take_reference, "
                                     "with the DUT replaced by a thru)")
                t = dict(ref)
                t["reference"] = t.pop("db")
            else:
                t = self._pick(source)
            now = self._clock()
        t.pop("taken_at", None)
        if which == "raw":
            t["raw"] = t.pop("db")
        elif which == "transmission":
            t["transmission"] = self._transmission(t, ref)
            t.pop("db")
            t["reference_acq_id"] = ref["acq_id"]
            t["reference_age_s"] = now - ref["taken_at"]
        t["freqs_Hz"] = _freqs(t)
        return t

    def get_result(self, quantity: str = "transmission", source: str = "sample") -> dict:
        """The scalar detectors of a trace, WITHOUT shipping the trace.

        transmission  peak_transmission_db, peak_freq_hz, mean_transmission_db,
                      bw3_hz (see physics.summarise_transmission); needs a
                      matching reference, like the trace
        raw           peak_db, peak_freq_hz, mean_db (band-averaged power),
                      dB relative to the TG output; needs no reference"""
        if quantity not in ("raw", "transmission"):
            raise ValueError(f"quantity must be 'raw' or 'transmission', got {quantity!r}")
        with self._lock:
            ref = self._reference
            t = self._pick(source)
        f = _freqs(t)
        out = {"acq_id": t.get("acq_id", 0), "trace_id": t.get("trace_id", 0)}
        if quantity == "transmission":
            out.update(physics.summarise_transmission(f, self._transmission(t, ref)))
            out["reference_acq_id"] = ref["acq_id"]
        else:
            s = physics.summarise_transmission(f, t["db"])
            out.update({"peak_db": s["peak_transmission_db"], "peak_freq_hz": s["peak_freq_hz"],
                        "mean_db": s["mean_transmission_db"]})
        return out

    @staticmethod
    def _transmission(t: dict, ref: dict | None) -> np.ndarray:
        if ref is None:
            raise ValueError("transmission needs a thru reference and there is none: "
                             "replace the DUT by a thru and take one (take_reference)")
        diffs = _reference_mismatch(t, ref)
        if diffs:
            raise ValueError("transmission refused: the reference does not match this "
                             "trace (" + "; ".join(diffs) + "). Take a new reference.")
        # dB - dB = the ratio of the two POWERS, i.e. |S21|^2 in dB = 20 log|S21|.
        # The TG output cancels too, which is why its unknown level does not matter.
        return np.asarray(t["db"], dtype=float) - np.asarray(ref["db"], dtype=float)

    def frequencies(self) -> np.ndarray:
        """The grid a scan should file the transmission trace on, read ONCE
        before the scan starts. The ANALYSER chooses the bins, so in order:
          1. the reference's grid, if it was taken for the band set now (every
             transmission trace must match it anyway, or it is refused);
          2. the last trace's grid, if it was swept with the band and point
             count set now;
          3. the backend's prediction (the simulator knows its grid; the
             owner only if it publishes its TG point count).
        Else refuse: the scan would otherwise file N points on a guess."""
        sw = self.cfg.sweep
        with self._lock:
            candidates = [self._reference, self._sample_trace, self._last]
        for t in candidates:
            if (t is not None and _same(t.get("req_start_Hz", _NAN), sw.start_Hz)
                    and _same(t.get("req_stop_Hz", _NAN), sw.stop_Hz)
                    and int(t.get("req_points", -1)) == int(sw.points)):
                return _freqs(t)
        g = self.backend.predicted_grid(sw.start_Hz, sw.stop_Hz, sw.points)
        if g is None:
            raise ValueError("the analyser's frequency grid for this band is not known yet: "
                             "take a reference (or acquire once) before the scan")
        start, bin_Hz, n = g
        return start + bin_Hz * np.arange(int(n))

    def grid_points(self) -> int | None:
        """How many bins the transmission trace will have, or None if unknown."""
        try:
            return int(self.frequencies().size)
        except ValueError:
            return None

    # ---- status ---------------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        c = self.cfg
        sw = c.sweep
        with self._lock:
            a = self._acq
            progress = 0.0
            if self._sweeping and self._sweep_dt > 0:
                progress = min(1.0, (self._clock() - self._sweep_t0) / self._sweep_dt)
            elif self._sweeping:
                progress = 1.0
            ls = self._last_summary
            return Status(
                connected=self._connected, idn=self._idn,
                hw_error=self._health or self._sweep_error,
                simulated=self.simulated, owner=dict(self._owner),
                start_Hz=sw.start_Hz, stop_Hz=sw.stop_Hz, points=int(sw.points),
                rbw_Hz=sw.rbw_Hz, averages=int(sw.averages),
                sweep_time_s=self.backend.estimate_time_s(sw.points, sw.averages),
                continuous=c.acquisition.continuous,
                sweeping=self._sweeping, sweep_progress=progress,
                sweeps=self._sweeps, trace_id=self._trace_id,
                last_peak_db=ls.get("peak_db", _NAN), last_peak_Hz=ls.get("peak_Hz", _NAN),
                last_peak_transmission_db=ls.get("peak_transmission_db", _NAN),
                acq_id=self._acq_id, acquiring=a is not None,
                acq_progress=progress if (a is not None and self._sweeping) else 0.0,
                acq_is_reference=bool(a is not None and a["reference"]),
                acq_error=self._acq_error,
                sample=dict(self._sample),
                reference=self._reference_status_locked(),
                sim_dut_inserted=bool(c.sim.dut_inserted) if self.simulated else False,
                sim_tg_attached=bool(c.sim.tg_attached) if self.simulated else False,
                sim_pad_dB=float(c.sim.pad_dB) if self.simulated else _NAN,
            )

    def _reference_status_locked(self) -> dict:
        r = self._reference
        if r is None:
            return _no_reference()
        return {"present": True, "acq_id": r["acq_id"], "start_Hz": r["start_Hz"],
                "stop_Hz": r["stop_Hz"], "bin_Hz": r["bin_Hz"], "points": r["points"],
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
        """One pass of the sweep thread: refresh the owner's health, then one
        acquisition if there is a reason to sweep. Returns True if a sweep
        finished. Public so tests can drive the analyser without the thread."""
        self._refresh_health()
        with self._lock:
            wanted = self._acq is not None or self.cfg.acquisition.continuous
        if not wanted:
            self._stop.wait(0.1)
            return False
        ok = self._sweep_once()
        if not ok:
            # after a failure, do not hammer an owner that is down
            with self._lock:
                failed = bool(self._sweep_error)
            if failed:
                self._stop.wait(0.5)
        return ok

    def _sweep_once(self) -> bool:
        sw = self.cfg.sweep
        start, stop = float(sw.start_Hz), float(sw.stop_Hz)
        points, rbw, avg = int(sw.points), float(sw.rbw_Hz), int(sw.averages)
        with self._lock:
            rev0, a0 = self._rev, self._acq
        t0 = self._clock()
        try:
            with self._hw:
                self.backend.start_sweep(start, stop, points, rbw, avg)
                dt = self.backend.estimate_time_s(points, avg)
        except Exception as exc:
            self._sweep_failed(a0, rev0, exc)
            return False
        with self._lock:
            self._sweeping, self._sweep_t0, self._sweep_dt = True, t0, dt

        # Wait WITHOUT the hardware lock. Abandon the sweep the moment it can
        # no longer count: settings changed (it would be the wrong band), a new
        # trigger arrived or the acquisition was aborted (`_acq` is no longer
        # the one this sweep was started for), or shutdown.
        try:
            while True:
                with self._lock:
                    abandoned = self._rev != rev0 or self._acq is not a0
                if self._stop.is_set() or abandoned:
                    with self._hw:
                        self._discard_pending()
                    with self._lock:
                        self._sweeping = False
                    return False
                with self._hw:
                    done = self.backend.poll()
                if done:
                    break
                self._stop.wait(0.02)
            with self._hw:
                res = self.backend.fetch()
        except Exception as exc:
            with self._hw:
                self._discard_pending()
            self._sweep_failed(a0, rev0, exc)
            return False

        trace = {"start_Hz": float(res["start_Hz"]), "bin_Hz": float(res["bin_Hz"]),
                 "points": int(res["points"]),
                 "stop_Hz": float(res["start_Hz"]) + float(res["bin_Hz"]) * (int(res["points"]) - 1),
                 "rbw_Hz": float(res.get("rbw_Hz", rbw) or rbw),
                 "averages": int(res.get("averages") or avg),
                 "overload": bool(res.get("overload", False)),
                 "req_start_Hz": start, "req_stop_Hz": stop, "req_points": points,
                 "time": time.time(), "db": np.asarray(res["db"], dtype=float)}
        pk = physics.summarise_transmission(_freqs(trace), trace["db"])
        summary = {"peak_db": pk["peak_transmission_db"], "peak_Hz": pk["peak_freq_hz"]}

        latched = None
        with self._lock:
            self._sweeping = False
            if self._rev != rev0:
                return False                  # changed during the final read-out
            self._sweep_error = ""
            self._sweeps += 1
            self._trace_id += 1
            trace["trace_id"] = self._trace_id
            self._last = trace
            if a0 is not None and self._acq is a0:
                # Clear "acquiring", publish the sample AND (for take_reference)
                # the reference in the SAME critical section. Split, a status
                # snapshot could land in between saying "#n finished" while
                # `sample` or `reference` is still the old one (gotcha #28).
                self._latch_locked(a0, trace)
                latched = a0
            # after the latch, so a fresh reference already counts
            ref = self._reference
            if ref is not None and not _reference_mismatch(trace, ref):
                t = trace["db"] - ref["db"]
                summary["peak_transmission_db"] = float(np.nanmax(t)) if np.isfinite(t).any() else _NAN
            self._last_summary = summary
        if trace["overload"]:
            self._emit("warn", "analyser input OVERLOAD during the TG sweep")
        if latched is not None and latched["reference"]:
            self._emit("info", f"thru reference taken (#{latched['id']}): "
                               f"{trace['points']} points")
        return True

    def _latch_locked(self, a: dict, trace: dict) -> None:
        """Publish a finished trace as THE sample (and as the reference, if that
        is what it was for). Called with _lock held."""
        f = _freqs(trace)
        raw = physics.summarise_transmission(f, trace["db"])
        sample = {k: v for k, v in trace.items() if k != "db"}
        sample.update({"acq_id": a["id"], "is_reference": bool(a["reference"]), "failed": False,
                       "error": "", "peak_db": raw["peak_transmission_db"],
                       "peak_Hz": raw["peak_freq_hz"],
                       "peak_transmission_db": _NAN, "peak_freq_hz": _NAN,
                       "mean_transmission_db": _NAN, "bw3_hz": _NAN})
        trace_s = {**sample, "db": trace["db"]}
        if a["reference"]:
            self._reference = {**trace_s, "taken_at": self._clock()}
        ref = self._reference
        if ref is not None and not _reference_mismatch(trace_s, ref):
            # the scalars in status too, for a GUI or a quick look; a scan reads
            # them with get_result, which refuses honestly when they are missing
            sample.update(physics.summarise_transmission(f, trace["db"] - ref["db"]))
            trace_s.update(sample)
        self._sample = sample
        self._sample_trace = trace_s
        self._acq_error = ""
        self._acq = None

    def _fail_locked(self, a: dict, reason: str) -> bool:
        """Latch acquisition `a` as FAILED with `reason`. Returns True if it
        was a take_reference and the old reference was cleared by it: a scan
        routine that asked for a new reference and did not get one must not
        carry on dividing by an older one taken somewhere else."""
        self._acq = None
        self._acq_error = reason
        self._sample = {"acq_id": a["id"], "time": time.time(), "is_reference": bool(a["reference"]),
                        "failed": True, "error": reason}
        self._sample_trace = None
        if a["reference"] and self._reference is not None:
            self._reference = None
            return True
        return False

    def _sweep_failed(self, a0, rev0, exc: Exception) -> None:
        """A sweep attempt failed. If it was the one an acquisition was waiting
        for, that acquisition is latched as failed; either way the reason is
        this module's hw_error until the next good sweep or a new trigger."""
        msg = str(exc) if isinstance(exc, SweepFailed) else f"{type(exc).__name__}: {exc}"
        cleared, failed_id = False, None
        with self._lock:
            self._sweeping = False
            self._sweep_error = msg
            if a0 is not None and self._acq is a0 and self._rev == rev0:
                failed_id = a0["id"]
                cleared = self._fail_locked(a0, msg)
        now = self._clock()
        if failed_id is not None:
            self._emit("error", f"acquisition #{failed_id} failed: {msg}")
        elif now - self._last_err_emit >= 5.0:       # continuous: one event per 5 s
            self._last_err_emit = now
            self._emit("error", f"TG sweep failed: {msg}")
        if cleared:
            self._emit("warn", "reference cleared: taking the new one failed")

    # ---- internals ----------------------------------------------------------------------------

    def _trigger(self, reference: bool) -> int:
        if not self._connected:
            raise ValueError("not connected")
        cleared = False
        with self._lock:
            # A new trigger abandons a running acquisition. If that one was a
            # take_reference, the reference it was meant to replace is stale
            # by intent: clear it (see _fail_locked).
            old = self._acq
            if old is not None and old["reference"] and self._reference is not None:
                self._reference, cleared = None, True
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can ever show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = {"id": self._acq_id, "reference": bool(reference)}
            # a fresh attempt: the last failure no longer describes the present
            self._sweep_error = ""
            n = self._acq_id
        if cleared:
            self._emit("warn", "reference cleared: taking it was interrupted by a new trigger")
        return n

    def _changed(self, msg: str, clamped: bool) -> None:
        """A trace-altering change: bump the revision and restart a running
        acquisition from scratch under the new settings (a take_reference stays
        a take_reference)."""
        restarted = None
        with self._lock:
            self._rev += 1
            self._sweep_error = ""
            if self._acq is not None:
                restarted = self._acq["id"]
                # a NEW dict with the same id: the sweep thread compares by
                # identity, so the sweep in flight is abandoned
                self._acq = dict(self._acq)
        self._emit("warn" if clamped else "info", msg + (" (clamped)" if clamped else ""))
        if restarted is not None:
            self._emit("warn", f"acquisition #{restarted} restarted: settings changed")

    def _refresh_health(self) -> None:
        try:
            health = self.backend.health() if self._connected else ""
            owner = self.backend.owner_status()
        except Exception as exc:              # never let the sweep thread die
            health, owner = f"{type(exc).__name__}: {exc}", _no_owner()
        with self._lock:
            self._health, self._owner = health, owner

    def _discard_pending(self) -> None:
        try:
            self.backend.abort()
        except Exception:
            pass

    def _sanitise_config(self) -> None:
        sw, lim = self.cfg.sweep, self.cfg.limits
        sw.stop_Hz = _clamp(float(sw.stop_Hz), lim.freq_min_Hz + lim.min_span_Hz, lim.freq_max_Hz)[0]
        sw.start_Hz = _clamp(float(sw.start_Hz), lim.freq_min_Hz, sw.stop_Hz - lim.min_span_Hz)[0]
        sw.points = int(_clamp(int(sw.points), lim.points_min, lim.points_max)[0])
        sw.rbw_Hz = 0.0 if float(sw.rbw_Hz) <= 0 else _clamp(float(sw.rbw_Hz), lim.rbw_min_Hz,
                                                             lim.rbw_max_Hz)[0]
        sw.averages = int(_clamp(int(sw.averages), lim.averages_min, lim.averages_max)[0])
        for name, (lo, hi) in SIM_LIMITS.items():
            v = _clamp(float(getattr(self.cfg.sim, name)), lo, hi)[0]
            setattr(self.cfg.sim, name, int(round(v)) if name == "dut_order" else v)

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)


def _reference_mismatch(trace: dict, ref: dict) -> list[str]:
    """What makes `ref` unusable for `trace`, in words; [] if it matches.

    Only what changes the MEANING of the subtraction counts: another grid
    subtracts bin i at a different frequency. RBW and averaging change the
    noise, not the meaning. (No TG level: the TG44A has none in sweep mode.)"""
    out = []
    for key, label in (("start_Hz", "first bin"), ("bin_Hz", "bin width")):
        a, b = float(trace.get(key, _NAN)), float(ref.get(key, _NAN))
        if not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-3):
            out.append(f"{label} {a:.9g} Hz vs reference {b:.9g} Hz")
    if int(trace.get("points", -1)) != int(ref.get("points", -2)):
        out.append(f"{trace.get('points')} points vs reference {ref.get('points')}")
    return out
