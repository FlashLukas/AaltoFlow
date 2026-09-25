"""The Analyzer: the brain between the wire and the backend (simulated or real).

Its SETTINGS (start, stop, points, IFBW, power, averages, S-parameter) are
set-and-forget: clamp, store, report. Its MEASUREMENT is a trace, and a scan
must never record a trace that was swept before the scan step it is filed
under -- the fire-and-forget contract makes that easy to get wrong, and nothing
raises when it happens.

Two ways to get a trace:

  continuous  the analyser sweeps on its own, like a VNA's front panel; the
              latest trace is `get_trace("last")`. Right for looking at it.

  acquire     the scan-safe read. `acquire()` returns an id at once. The sweep
              thread then averages the next `sweep.averages` sweeps that STARTED
              after the trigger (a sweep already running is thrown away, like
              a real VNA restarting on a trigger) and latches the mean as the
              sample: `get_trace("sample")` + `status().sample`. Callers wait
              until status shows `acq_id` == their id AND `acquiring` False.

A settings change in the middle of an acquisition RESTARTS it: averaging a
trace swept at 10 kHz IFBW with one swept at 1 kHz, or with a different point
count, is not a measurement of anything.

THE REFERENCE. VNA-FMR data is read relative to a trace taken where the sample
does not resonate in the band (e.g. 150 mT at 45 deg): the loss slope, ripple
and phase winding of the cables cancel, and
        u = (S - S_ref) / S_ref
is what the sample does (complex; a permeability-like quantity). The brain owns
the reference, so every client (GUI, console, scan) sees the SAME one:
  take_reference   an acquisition exactly like `acquire` (same averaging, same
                   abort / restart rules) whose sample is ALSO stored as the
                   reference -- in the same critical section that latches the
                   sample, so no status frame can say "done" before it exists.
  get_trace(quantity="u")  refused, with a message saying what differs, when
                   there is no reference or it was taken with another
                   S-parameter / start / stop / point count.

THE FIELD. The brain -- not the simulator -- reads the magnet service (mag2d by
default), so the field, its angle and whether it was live are latched into
every sample in real mode too.

Threads and locks, the pm16/hf2 rules:
  * ONE sweep thread talks to the backend. `status()` only copies what it
    stored and never touches the hardware.
  * Every backend call runs under `_hw`, but the WAIT during a sweep does not --
    a narrow-IFBW sweep can take 20 s, and a setter must not hang that long.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import model
from .config import Config
from .field import FIELD_SOURCES, FieldReading, make_field_source

_NAN = float("nan")

SPARAMS = model.SPARAMS

#: Sanity envelope for the simulated sample, name -> (min, max). Loose on
#: purpose: the point is to stop nonsense (negative damping, a zero gyromagnetic
#: ratio) rather than to police which materials are allowed.
SAMPLE_LIMITS = {
    "ms_mT": (0.0, 2500.0),
    "gamma_GHz_per_T": (1.0, 100.0),
    "alpha": (1e-6, 0.5),
    "dh0_mT": (0.0, 200.0),
    "h_anis_mT": (-1000.0, 1000.0),
    "hk_mT": (-1000.0, 1000.0),
    "easy_axis_deg": (-360.0, 360.0),
    "dip_dB": (0.0, 40.0),
}
GEOMETRIES = ("in_plane", "out_of_plane")
ANGLE_LIMIT_DEG = 360.0


def _no_reference() -> dict:
    """The `reference` status block when there is none. Same keys as a present
    one (NaN -> null on the wire), so a client never has to test for a key."""
    return {"present": False, "acq_id": 0, "field_mT": _NAN, "angle_deg": _NAN,
            "sparam": "", "start_Hz": _NAN, "stop_Hz": _NAN, "points": 0, "age_s": _NAN}


@dataclass
class Status:
    """One snapshot of the analyser, for status() and the wire. No arrays: the
    status goes out 10 times a second; traces are fetched with get_trace."""

    connected: bool
    idn: str = ""
    hw_error: str = ""
    simulated: bool = True
    # sweep settings
    start_Hz: float = _NAN
    stop_Hz: float = _NAN
    points: int = 0
    ifbw_Hz: float = _NAN
    power_dBm: float = _NAN
    averages: int = 1
    sparam: str = "S21"
    sweep_time_s: float = _NAN
    continuous: bool = True
    # what the sweep thread is doing
    sweeping: bool = False
    sweep_progress: float = 0.0
    sweeps: int = 0                    # completed sweeps since start
    trace_id: int = 0                  # id of the latest trace (any kind)
    dip_Hz: float = _NAN               # dip in the latest trace
    dip_dB: float = _NAN
    # the field the sample sits in
    field_source_set: str = ""         # what was asked for: "mag2d" | "clMag" | "manual"
    field_source: str = ""             # what is actually in use, with its health
    field_mT: float = _NAN
    angle_deg: float = _NAN
    field_ok: bool = False
    field_age_s: float = _NAN
    manual_field_mT: float = _NAN
    manual_angle_deg: float = _NAN
    # the simulated world (the model's numbers; NaN on a real analyser)
    f_res_model_Hz: float = _NAN       # Kittel at the current field
    geometry: str = "in_plane"
    ms_mT: float = _NAN
    gamma_GHz_per_T: float = _NAN
    alpha: float = _NAN
    dh0_mT: float = _NAN
    h_anis_mT: float = _NAN
    hk_mT: float = _NAN
    easy_axis_deg: float = _NAN
    dip_set_dB: float = _NAN
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


def _circular_mean_deg(angles) -> float:
    """Mean of angles in degrees, done on the unit circle: the mean of 179 and
    -179 deg is 180, not 0."""
    a = np.radians(np.asarray([x for x in angles if math.isfinite(x)], dtype=float))
    if a.size == 0:
        return _NAN
    return float(np.degrees(np.arctan2(np.sin(a).mean(), np.cos(a).mean())))


class Analyzer:
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
        self.field = None                   # the field source (field.py); built in start()

        # everything below is written under _lock
        self._rev = 0                       # bumped by any change that alters a trace
        self._live: dict = {}
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
        """Open the analyser and start the sweep thread.

        `run=False` skips the thread, so a test can drive `step()` by hand."""
        self._sanitise_config()
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
        self._build_field()
        self._connected = True
        self._refresh_live()
        self._emit("info", f"connected: {self._idn}")
        self._emit("info", "field from " + self._field_description())
        if run:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="vna-sweep", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop sweeping and disconnect. Safe to call more than once."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        was = self._connected
        try:
            with self._hw:
                self.backend.close()
        finally:
            self._connected = False
            if self.field is not None:
                self.field.close()
                self.field = None
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
        self._changed(f"start {v / 1e9:.6g} GHz", clamped)

    def set_stop(self, hz: float) -> None:
        v, clamped = _clamp(_finite(hz, "stop"), *self.stop_limits())
        self.cfg.sweep.stop_Hz = v
        self._changed(f"stop {v / 1e9:.6g} GHz", clamped)

    def set_points(self, n: int) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(int(round(_finite(n, "points"))), lim.points_min, lim.points_max)
        self.cfg.sweep.points = int(v)
        self._changed(f"{int(v)} points", clamped)

    def set_ifbw(self, hz: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(hz, "IFBW"), lim.ifbw_min_Hz, lim.ifbw_max_Hz)
        self.cfg.sweep.ifbw_Hz = v
        self._changed(f"IFBW {v:g} Hz", clamped)

    def set_power(self, dbm: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(dbm, "power"), lim.power_min_dBm, lim.power_max_dBm)
        self.cfg.sweep.power_dBm = v
        self._changed(f"power {v:g} dBm", clamped)

    def set_averages(self, n: int) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(int(round(_finite(n, "averages"))), lim.averages_min, lim.averages_max)
        self.cfg.sweep.averages = int(v)
        self._changed(f"{int(v)} averages per acquisition", clamped)

    def set_sparam(self, sparam: str) -> None:
        """Which S-parameter is measured. A change makes every later trace a
        different quantity, so it restarts an acquisition like any setting --
        and a reference taken with the old one no longer matches (u refuses)."""
        sparam = str(sparam).upper()
        if sparam not in SPARAMS:
            raise ValueError(f"sparam must be one of {SPARAMS}, got {sparam!r}")
        self.cfg.sweep.sparam = sparam
        self._changed(f"measuring {sparam}", False)

    def set_continuous(self, on: bool) -> None:
        self.cfg.acquisition.continuous = bool(on)
        self._emit("info", "continuous sweep on" if on else "sweep on trigger only")

    # ---- the field ---------------------------------------------------------------------

    def set_field_source(self, source: str) -> None:
        source = str(source)
        if source not in FIELD_SOURCES:
            raise ValueError(f"field source must be one of {FIELD_SOURCES}, got {source!r}")
        self.cfg.field.source = source
        if self._connected:
            self._build_field()
        self._refresh_live()
        self._changed(f"field source: {source}", False)

    def set_manual_field(self, mT: float, angle_deg: float | None = None) -> None:
        """Deliberately does NOT restart an acquisition: a changing field is the
        physical world changing, the same as the magnet moving."""
        m = self.cfg.limits.manual_field_max_mT
        v, clamped = _clamp(_finite(mT, "field"), -m, m)
        self.cfg.field.manual_mT = v
        msg = f"manual field {v:g} mT" + (" (clamped)" if clamped else "")
        if angle_deg is not None:
            a, ac = _clamp(_finite(angle_deg, "angle"), -ANGLE_LIMIT_DEG, ANGLE_LIMIT_DEG)
            self.cfg.field.manual_angle_deg = a
            clamped = clamped or ac
            msg += f" at {a:g} deg" + (" (clamped)" if ac else "")
        self._refresh_live()
        self._emit("warn" if clamped else "info", msg)

    def set_manual_angle(self, angle_deg: float) -> None:
        a, clamped = _clamp(_finite(angle_deg, "angle"), -ANGLE_LIMIT_DEG, ANGLE_LIMIT_DEG)
        self.cfg.field.manual_angle_deg = a
        self._refresh_live()
        self._emit("warn" if clamped else "info",
                   f"manual angle {a:g} deg" + (" (clamped)" if clamped else ""))

    # ---- the simulated world -----------------------------------------------------------

    def set_sample(self, name: str, value: float) -> None:
        if name not in SAMPLE_LIMITS:
            raise ValueError(f"unknown sample parameter {name!r}; "
                             f"one of {', '.join(SAMPLE_LIMITS)}")
        v, clamped = _clamp(_finite(value, name), *SAMPLE_LIMITS[name])
        setattr(self.cfg.sample, name, v)
        self._refresh_live()
        self._changed(f"sample {name} = {v:g}", clamped)

    def set_geometry(self, geometry: str) -> None:
        if geometry not in GEOMETRIES:
            raise ValueError(f"geometry must be one of {GEOMETRIES}, got {geometry!r}")
        self.cfg.sample.geometry = geometry
        self._refresh_live()
        self._changed(f"geometry {geometry}", False)

    # ---- the scan-safe read ------------------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its id immediately. The clock starts
        NOW: call it after everything the measurement depends on has been set."""
        return self._trigger(reference=False)

    def take_reference(self) -> int:
        """Start an acquisition that becomes THE reference when it completes.
        Returns its id; wait for it exactly as for `acquire`."""
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

        Aborting a take_reference also CLEARS the old reference. A scan routine
        that asked for a new reference and got an abort must not carry on
        dividing by an older one taken somewhere else -- u refuses instead."""
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

    def get_trace(self, which: str = "sample", quantity: str = "s") -> dict:
        """A trace and what it was measured under.

        which    "sample" (the latched acquisition, what a scan records), "last"
                 (the newest sweep of any kind) or "reference".
        quantity "s"  the S-parameter as measured (key "s")
                 "u"  (S - S_ref)/S_ref against the brain's reference (key "u")
                 "ln" ln(S / S_ref), the complex logarithm (key "ln")

        Why both u and ln: the old LabVIEW program plotted u, and for a shallow
        line the two agree, since u ~ ln(1 + u). But a transmission line with a
        film on it gives S = A exp(i eta chi), so it is the LOGARITHM that is
        proportional to the susceptibility -- exactly, at any depth. No
        prefactor is applied here: eta depends on the waveguide and the film,
        and inventing one would hand back numbers in no units at all.

        Raises ValueError when there is nothing honest to return."""
        if quantity not in ("s", "u", "ln"):
            raise ValueError(f"quantity must be 's', 'u' or 'ln', got {quantity!r}")
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
        # the division can run outside the lock.
        if quantity in ("u", "ln"):
            if ref is None:
                raise ValueError(f"{quantity} needs a reference and there is none: "
                                 "take a reference first (take_reference)")
            diffs = _reference_mismatch(t, ref)
            if diffs:
                raise ValueError(f"{quantity} refused: the reference does not match this "
                                 "trace (" + "; ".join(diffs) + "). Take a new reference.")
            with np.errstate(divide="ignore", invalid="ignore"):
                s = t.pop("s")
                if quantity == "u":
                    t["u"] = (s - ref["s"]) / ref["s"]
                else:
                    # numpy's complex log is the PRINCIPAL branch, so the
                    # imaginary part is wrapped into (-pi, pi]. Fine here: S/S_ref
                    # is a small perturbation around 1, and unwrapping a trace
                    # that never approaches the cut would only invent structure.
                    t["ln"] = np.log(s / ref["s"])
            t["reference_acq_id"] = ref["acq_id"]
            t["reference_field_mT"] = ref.get("field_mT", _NAN)
            t["reference_angle_deg"] = ref.get("angle_deg", _NAN)
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
        sw, smp = c.sweep, c.sample
        with self._lock:
            live, a = self._live, self._acq
            last = self._last or {}
            progress = 0.0
            if self._sweeping and self._sweep_dt > 0:
                progress = min(1.0, (self._clock() - self._sweep_t0) / self._sweep_dt)
            acq_progress = 0.0
            if a is not None:
                acq_progress = min(1.0, (a["n"] + progress * self._sweeping) / a["want"])
            return Status(
                connected=self._connected, idn=self._idn, hw_error=self._hw_error,
                simulated=self.simulated,
                start_Hz=sw.start_Hz, stop_Hz=sw.stop_Hz, points=int(sw.points),
                ifbw_Hz=sw.ifbw_Hz, power_dBm=sw.power_dBm, averages=int(sw.averages),
                sparam=sw.sparam,
                sweep_time_s=self.backend.sweep_time_s(sw.points, sw.ifbw_Hz),
                continuous=c.acquisition.continuous,
                sweeping=self._sweeping, sweep_progress=progress,
                sweeps=self._sweeps, trace_id=self._trace_id,
                dip_Hz=last.get("dip_Hz", _NAN), dip_dB=last.get("dip_dB", _NAN),
                field_source_set=c.field.source,
                field_source=live.get("field_source", ""),
                field_mT=live.get("field_mT", _NAN), angle_deg=live.get("angle_deg", _NAN),
                field_ok=live.get("field_ok", False),
                field_age_s=live.get("field_age_s", _NAN),
                manual_field_mT=c.field.manual_mT, manual_angle_deg=c.field.manual_angle_deg,
                f_res_model_Hz=live.get("f_res_model_Hz", _NAN),
                geometry=smp.geometry, ms_mT=smp.ms_mT,
                gamma_GHz_per_T=smp.gamma_GHz_per_T, alpha=smp.alpha,
                dh0_mT=smp.dh0_mT, h_anis_mT=smp.h_anis_mT, hk_mT=smp.hk_mT,
                easy_axis_deg=smp.easy_axis_deg, dip_set_dB=smp.dip_dB,
                acq_id=self._acq_id, acquiring=a is not None, acq_progress=acq_progress,
                acq_is_reference=bool(a is not None and a["reference"]),
                sample=dict(self._sample),
                reference=self._reference_status_locked(),
            )

    def _reference_status_locked(self) -> dict:
        r = self._reference
        if r is None:
            return _no_reference()
        return {"present": True, "acq_id": r["acq_id"], "field_mT": r.get("field_mT", _NAN),
                "angle_deg": r.get("angle_deg", _NAN), "sparam": r["sparam"],
                "start_Hz": r["start_Hz"], "stop_Hz": r["stop_Hz"], "points": r["points"],
                "age_s": self._clock() - r["taken_at"]}

    # ---- config (Settings dialog / wire) ------------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp cfg (possibly edited in place over the wire) and rebuild the
        field source, which may have a new host, port or source."""
        self._sanitise_config()
        if self._connected:
            self._build_field()
            self._refresh_live()
        self._changed("settings applied", False)

    # ---- the sweep thread -------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()

    def step(self) -> bool:
        """One pass of the sweep thread: refresh the live readouts, then one
        sweep if there is a reason to sweep. Returns True if a sweep finished.
        Public so tests can drive the analyser without the thread."""
        self._refresh_live()
        with self._lock:
            wanted = self._acq is not None or self.cfg.acquisition.continuous
        if not wanted:
            self._stop.wait(0.1)
            return False
        try:
            return self._sweep_once()
        except Exception as exc:              # never let the sweep thread die
            with self._lock:
                self._sweeping = False
            self._report_hw_error(exc)
            self._stop.wait(0.5)
            return False

    def _sweep_once(self) -> bool:
        sw = self.cfg.sweep
        freqs = np.linspace(sw.start_Hz, sw.stop_Hz, int(sw.points))
        sparam = sw.sparam
        with self._lock:
            rev0, acq0 = self._rev, self._acq_id
        # The field is read ONCE, as the sweep starts: in a scan the magnet has
        # settled before the trigger, so this is the field of the sweep.
        fr = self._read_field()
        t0 = self._clock()
        with self._hw:
            self.backend.start_sweep(freqs, sw.ifbw_Hz, sw.power_dBm, sparam, fr)
            dt = self.backend.sweep_time_s(freqs.size, sw.ifbw_Hz)
        with self._lock:
            self._sweeping, self._sweep_t0, self._sweep_dt = True, t0, dt

        # Wait out the sweep WITHOUT the hardware lock. Abandon it the moment it
        # can no longer count: settings changed (the trace would be neither), or
        # a new trigger arrived (a real VNA restarts the sweep on a trigger).
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
            z, meta = self.backend.finish_sweep()
        dip_Hz, dip_dB = model.find_dip(freqs, z)
        trace = {"start_Hz": float(freqs[0]), "stop_Hz": float(freqs[-1]),
                 "points": int(freqs.size), "ifbw_Hz": float(sw.ifbw_Hz),
                 "power_dBm": float(sw.power_dBm), "sparam": sparam, "s": z,
                 "dip_Hz": dip_Hz, "dip_dB": dip_dB, "time": time.time(),
                 "field_mT": fr.field_mT, "angle_deg": fr.angle_deg, "field_ok": fr.ok,
                 "field_source": fr.source, **meta}

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
                a["sum"] = z.copy() if a["sum"] is None else a["sum"] + z
                a["n"] += 1
                a["fields"].append(fr.field_mT)
                a["angles"].append(fr.angle_deg)
                a["field_ok"] = a["field_ok"] and bool(fr.ok)
                if a["n"] >= a["want"]:
                    # Clear "acquiring", publish the sample AND (for
                    # take_reference) the reference in the SAME critical
                    # section. Split, a status snapshot could land in between
                    # saying "#n finished" while `sample` or `reference` is
                    # still the old one -- and a scan waiting for #n would use it.
                    self._latch_locked(a, trace, meta)
                    self._acq = None
                    latched = (a, self._sample.get("field_mT", _NAN),
                               self._sample.get("angle_deg", _NAN))
        if latched is not None:
            a, f_mT, ang = latched
            if not a["field_ok"]:
                self._emit("warn", f"acquisition #{a['id']}: the field was not live "
                                   f"(magnet not publishing?); field_ok = False")
            if a["reference"]:
                self._emit("info", f"reference taken (#{a['id']}) at {f_mT:.3f} mT, "
                                   f"{ang:.1f} deg")
        return True

    def _latch_locked(self, a: dict, last: dict, meta: dict) -> None:
        """Average an acquisition's sweeps and publish them as THE sample (and
        as the reference, if that is what it was for). Called with _lock held
        (the dip fit is a few ms; see the caller for why)."""
        mean = a["sum"] / a["n"]
        dip_Hz, dip_dB = model.find_dip(
            np.linspace(last["start_Hz"], last["stop_Hz"], last["points"]), mean)
        fields = np.asarray(a["fields"], dtype=float)
        field_mT = float(np.nanmean(fields)) if np.isfinite(fields).any() else _NAN
        angle = _circular_mean_deg(a["angles"])
        f_model = _NAN
        if self.simulated and math.isfinite(field_mT):
            f_model = model.kittel_Hz(field_mT, self.cfg.sample,
                                      angle if math.isfinite(angle) else 0.0)
        extra = {k: v for k, v in meta.items() if k != "f_res_model_Hz"}
        self._sample = {
            "acq_id": a["id"], "time": time.time(), "averages": a["n"],
            "reference": bool(a["reference"]),
            "sparam": last["sparam"],
            "start_Hz": last["start_Hz"], "stop_Hz": last["stop_Hz"],
            "points": last["points"], "ifbw_Hz": last["ifbw_Hz"],
            "power_dBm": last["power_dBm"],
            "field_mT": field_mT, "angle_deg": angle, "field_ok": a["field_ok"],
            "field_spread_mT": float(np.ptp(fields)) if fields.size else _NAN,
            "f_res_model_Hz": f_model,
            "dip_Hz": dip_Hz, "dip_dB": dip_dB, **extra,
        }
        self._sample_trace = {**self._sample, "s": mean}
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
                "n": 0, "sum": None, "fields": [], "angles": [], "field_ok": True,
                "reference": bool(reference)}

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

    def _build_field(self) -> None:
        """(Re)build the field subscription from cfg.field. Closing the old one
        after swapping means the sweep thread always has a source to read."""
        new = make_field_source(self.cfg.field)
        old, self.field = self.field, new
        if old is not None:
            old.close()

    def _field_description(self) -> str:
        f = self.cfg.field
        if f.source == "mag2d":
            return f"mag2d at {f.mag2d_host}:{f.mag2d_pub_port}"
        if f.source == "clMag":
            return f"clMag at {f.clMag_host}:{f.clMag_pub_port}"
        return f"manual value {f.manual_mT:g} mT at {f.manual_angle_deg:g} deg"

    def _read_field(self) -> FieldReading:
        src = self.field
        if src is None:
            return FieldReading(_NAN, False, "not connected", _NAN, _NAN)
        return src.read()

    def _refresh_live(self) -> None:
        live: dict = {}
        if self._connected:
            try:
                fr = self._read_field()
                live = {"field_mT": fr.field_mT, "angle_deg": fr.angle_deg, "field_ok": fr.ok,
                        "field_source": fr.source, "field_age_s": fr.age_s}
                if self.simulated and math.isfinite(fr.field_mT):
                    live["f_res_model_Hz"] = model.kittel_Hz(
                        fr.field_mT, self.cfg.sample,
                        fr.angle_deg if math.isfinite(fr.angle_deg) else 0.0)
            except Exception:
                live = {}
        with self._lock:
            self._live = live

    def _discard_pending(self) -> None:
        try:
            self.backend.abort_sweep()
        except Exception:
            pass

    def _sanitise_config(self) -> None:
        sw, lim = self.cfg.sweep, self.cfg.limits
        sw.stop_Hz = _clamp(float(sw.stop_Hz), lim.freq_min_Hz + lim.min_span_Hz, lim.freq_max_Hz)[0]
        sw.start_Hz = _clamp(float(sw.start_Hz), lim.freq_min_Hz, sw.stop_Hz - lim.min_span_Hz)[0]
        sw.points = int(_clamp(int(sw.points), lim.points_min, lim.points_max)[0])
        sw.ifbw_Hz = _clamp(float(sw.ifbw_Hz), lim.ifbw_min_Hz, lim.ifbw_max_Hz)[0]
        sw.power_dBm = _clamp(float(sw.power_dBm), lim.power_min_dBm, lim.power_max_dBm)[0]
        sw.averages = int(_clamp(int(sw.averages), lim.averages_min, lim.averages_max)[0])
        sw.sparam = str(sw.sparam).upper()
        if sw.sparam not in SPARAMS:
            sw.sparam = "S21"
        for name, (lo, hi) in SAMPLE_LIMITS.items():
            setattr(self.cfg.sample, name, _clamp(float(getattr(self.cfg.sample, name)), lo, hi)[0])
        if self.cfg.sample.geometry not in GEOMETRIES:
            self.cfg.sample.geometry = "in_plane"
        if self.cfg.field.source not in FIELD_SOURCES:
            self.cfg.field.source = "mag2d"

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

    Only what changes the MEANING of the division counts: another S-parameter
    divides two different quantities, another grid divides point i by a
    different frequency. IFBW and power change the noise, not the meaning."""
    out = []
    if trace.get("sparam") != ref.get("sparam"):
        out.append(f"S-parameter {trace.get('sparam')} vs reference {ref.get('sparam')}")
    for key, label, unit, scale in (("start_Hz", "start", "GHz", 1e9),
                                    ("stop_Hz", "stop", "GHz", 1e9)):
        a, b = float(trace.get(key, _NAN)), float(ref.get(key, _NAN))
        if not math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-3):
            out.append(f"{label} {a / scale:.9g} {unit} vs reference {b / scale:.9g} {unit}")
    if int(trace.get("points", -1)) != int(ref.get("points", -2)):
        out.append(f"{trace.get('points')} points vs reference {ref.get('points')}")
    return out
