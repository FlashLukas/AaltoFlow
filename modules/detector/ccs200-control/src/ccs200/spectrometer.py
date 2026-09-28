"""The Spectrometer: the brain between the wire and the backend (simulated or real).

Its SETTINGS (integration time, averages, dark subtraction, the analysis
window) are set-and-forget: clamp, store, report. Its MEASUREMENT is a spectrum,
and a scan must never record a spectrum exposed before the scan step it is filed
under -- the fire-and-forget contract makes that easy to get wrong, and nothing
raises when it happens.

Two ways to get a spectrum:

  continuous  the CCD scans on its own, like ThorSpectra's live view; the
              latest scan is `get_trace("last")`. Right for looking at it.

  acquire     the scan-safe read. `acquire()` returns an id at once. The scan
              thread then averages the next `scan.averages` scans that STARTED
              after the trigger (an exposure already running is thrown away:
              it began before the thing the scan just changed) and latches the
              mean as the sample: `get_trace("sample")` + `status().sample`.
              Callers wait until status shows `acq_id` == their id AND
              `acquiring` False.

A change of integration time or averages in the middle of an acquisition
RESTARTS it: averaging a 10 ms scan with a 100 ms one is not a measurement.

THE DARK. A CCD reads something with no light at all: an electronic offset
plus a dark current that grows with integration time and differs from pixel to
pixel. `take_dark` is an acquisition exactly like `acquire` whose mean is ALSO
kept as the dark spectrum -- latched in the same critical section as the sample
(gotcha #28), so no status frame can say "done" before the dark exists. With
`dark_subtract` on, every processed spectrum is scan - dark.
  * A dark only fits the integration time it was taken at (the dark current
    scales with it). So with subtraction on, `acquire` is REFUSED -- with a
    message saying why -- when there is no dark or it was taken at another
    integration time. A scan then stops with that message instead of
    recording spectra with the wrong background.
  * The live view subtracts when it can and says when it cannot.
  * Nobody but the user can block the light: take the dark with the fibre
    capped (the simulator has a "light on input" switch for this).

THE ANALYSIS. From the processed mean the brain derives scalar detectors
inside the analysis window: peak wavelength (sub-pixel, a parabola through the
three highest points), peak intensity, integrated intensity (trapezoid over
nm), and whether any pixel saturated in any averaged scan.

Threads and locks, the pm16/hf2 rules:
  * ONE scan thread talks to the backend. `status()` only copies what it
    stored and never touches the hardware.
  * Every backend call runs under `_hw`, but the WAIT during an exposure does
    not -- a 60 s integration must not make a setter hang for a minute.
  * The thread sleeps with time.sleep, not Event.wait (gotcha #34: on Windows a
    timed Event.wait rounds up to 15.6 ms, which is longer than a 10 us scan).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .config import Config

_NAN = float("nan")

#: A pixel at or above this fraction of full scale counts as saturated. Just
#: under 1.0 because noise on a clipped pixel reads 0.999..., never above 1.
SATURATION = 0.99

#: Sanity envelope for the simulated light and CCD, name -> (min, max). Loose:
#: it stops nonsense (negative dark current), not experiments.
SIM_LIMITS = {
    "lamp_level_per_s": (0.0, 1e6),
    "lamp_temperature_K": (500.0, 10000.0),
    "line_level_per_s": (0.0, 1e6),
    "line_fwhm_nm": (0.1, 50.0),
    "dark_rate_per_s": (0.0, 10.0),
    "offset": (0.0, 0.5),
    "read_noise": (0.0, 0.1),
}


def _no_dark() -> dict:
    """The `dark` status block when there is none. Same keys as a present one
    (NaN -> null on the wire), so a client never has to test for a key."""
    return {"present": False, "acq_id": 0, "integration_time_s": _NAN, "averages": 0,
            "age_s": _NAN, "matches": False}


@dataclass
class Status:
    """One snapshot of the spectrometer, for status() and the wire. No arrays:
    the status goes out 10 times a second; spectra are fetched with get_trace."""

    connected: bool
    idn: str = ""
    hw_error: str = ""
    simulated: bool = True
    pixels: int = 0
    wl_min_nm: float = _NAN
    wl_max_nm: float = _NAN
    # settings
    integration_time_s: float = _NAN
    averages: int = 1
    dark_subtract: bool = False
    continuous: bool = True
    window_min_nm: float = _NAN
    window_max_nm: float = _NAN
    scan_time_s: float = _NAN
    # what the scan thread is doing
    scanning: bool = False
    scan_progress: float = 0.0
    scans: int = 0                     # completed scans since start
    trace_id: int = 0                  # id of the latest scan
    # the latest scan (live, NOT scan-safe)
    peak_nm: float = _NAN
    peak_intensity: float = _NAN
    integrated: float = _NAN
    exposure: float = _NAN             # highest RAW pixel, fraction of full scale
    saturated: bool = False
    live_dark_applied: bool = False
    # the simulated light (NaN / False on the real instrument)
    light_on: bool = False
    lamp_level_per_s: float = _NAN
    lamp_temperature_K: float = _NAN
    line_level_per_s: float = _NAN
    line_fwhm_nm: float = _NAN
    dark_rate_per_s: float = _NAN
    offset: float = _NAN
    read_noise: float = _NAN
    # acquisition
    acq_id: int = 0
    acquiring: bool = False
    acq_progress: float = 0.0
    acq_is_dark: bool = False
    sample: dict = field(default_factory=dict)
    dark: dict = field(default_factory=_no_dark)


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


def _same_time(a: float, b: float) -> bool:
    return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)


def analyse(wl: np.ndarray, spectrum: np.ndarray, lo_nm: float, hi_nm: float) -> dict:
    """Peak (sub-pixel), peak intensity and integrated intensity of `spectrum`
    inside [lo_nm, hi_nm]. NaN when the window holds fewer than 2 pixels."""
    sel = np.flatnonzero((wl >= lo_nm) & (wl <= hi_nm))
    if sel.size < 2:
        return {"peak_nm": _NAN, "peak_intensity": _NAN, "integrated": _NAN}
    y = spectrum[sel]
    i = int(np.argmax(y))
    peak_nm, peak_val = float(wl[sel[i]]), float(y[i])
    if 0 < i < sel.size - 1:
        # A parabola through the three highest points: the line centre to a
        # fraction of a pixel (~0.2 nm/pixel here), as long as the line is a
        # few pixels wide -- which the CCS200's ~1.5 nm resolution guarantees.
        y0, y1, y2 = float(y[i - 1]), float(y[i]), float(y[i + 1])
        den = y0 - 2.0 * y1 + y2
        if den < 0:
            d = 0.5 * (y0 - y2) / den
            if abs(d) <= 1.0:
                x0, x1, x2 = (float(wl[sel[i + k]]) for k in (-1, 0, 1))
                # map the fractional pixel offset onto the (non-uniform) nm grid
                peak_nm = x1 + (d * (x2 - x1) if d > 0 else -d * (x0 - x1))
                peak_val = y1 - 0.25 * (y0 - y2) * d
    trapz = getattr(np, "trapezoid", None) or np.trapz
    return {"peak_nm": peak_nm, "peak_intensity": peak_val,
            "integrated": float(trapz(y, wl[sel]))}


class Spectrometer:
    def __init__(self, backend, cfg: Config | None = None, clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards the snapshot, spectra, acquisition

        self._connected = False
        self._idn = ""
        self._hw_error = ""
        self._last_err_emit = -1e9
        self._wl = np.zeros(0)

        # everything below is written under _lock
        self._rev = 0                       # bumped by any change that alters a scan
        self._scanning = False
        self._scan_t0 = 0.0
        self._scan_dt = 0.0
        self._scans = 0
        self._trace_id = 0
        self._last: dict | None = None      # latest scan (raw + processed)
        self._acq_id = 0
        self._acq: dict | None = None
        self._sample: dict = {}
        self._sample_trace: dict | None = None
        self._dark: dict | None = None      # a latched raw mean + "taken_at"

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    @property
    def simulated(self) -> bool:
        return bool(getattr(self.backend, "simulated", True))

    # ---- lifecycle ---------------------------------------------------------------

    def start(self, run: bool = True) -> None:
        """Open the spectrometer and start the scan thread.

        `run=False` skips the thread, so a test can drive `step()` by hand."""
        self._sanitise_config()
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            self._wl = np.asarray(self.backend.wavelengths(), dtype=float)
            t_dev = float(getattr(self.backend, "integration_time", lambda: _NAN)())
        self._connected = True
        self._emit("info", f"connected: {self._idn}")
        self._adopt_integration_time(t_dev)
        if run:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="ccs200-scan", daemon=True)
            self._thread.start()

    def _adopt_integration_time(self, t_dev: float) -> None:
        """Take over the integration time the instrument is ALREADY set to
        (Lukas's rule 2026-09-27: read the state at start, change nothing).
        The value in the .ini is then only a default for when the user sets
        one explicitly -- it is no longer pushed at start. The first scan runs
        at the adopted time, so the backend has nothing to write.

        Only if the instrument's time lies OUTSIDE the Limits envelope is it
        clamped (and the first scan writes the clamped value): the envelope is
        the one thing config is allowed to impose. A warn event says so."""
        if not (math.isfinite(t_dev) and t_dev > 0):
            return                          # unknown: keep the config value
        lim = self.cfg.limits
        v, clamped = _clamp(t_dev, lim.integration_min_s, lim.integration_max_s)
        with self._lock:
            old = float(self.cfg.scan.integration_time_s)
            self.cfg.scan.integration_time_s = v
            if not _same_time(old, v):
                self._rev += 1
        if clamped:
            self._emit("warn", f"the instrument is at {t_dev * 1e3:.6g} ms, outside the limits: "
                               f"using {v * 1e3:.6g} ms")
        else:
            self._emit("info", f"adopted the instrument's integration time {v * 1e3:.6g} ms")

    def shutdown(self) -> None:
        """Stop scanning and disconnect. Safe to call more than once. A
        spectrometer has no output to make safe: stopping is enough."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        was = self._connected
        try:
            with self._hw:
                self._discard_pending()
                self.backend.close()
        finally:
            self._connected = False
            with self._lock:
                self._acq = None
                self._scanning = False
            if was:
                self._emit("info", "disconnected")

    # ---- settings (each clamps, stores in cfg, reports) --------------------------

    def set_integration_time(self, seconds: float) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(_finite(seconds, "integration time"),
                            lim.integration_min_s, lim.integration_max_s)
        self.cfg.scan.integration_time_s = v
        self._changed(f"integration time {v * 1e3:.6g} ms", clamped)
        with self._lock:
            d = self._dark
        if self.cfg.scan.dark_subtract and d is not None and not _same_time(d["integration_time_s"], v):
            self._emit("warn", f"the dark was taken at {d['integration_time_s'] * 1e3:.6g} ms: "
                               "take a new dark before acquiring with subtraction on")

    def set_averages(self, n: int) -> None:
        lim = self.cfg.limits
        v, clamped = _clamp(int(round(_finite(n, "averages"))), lim.averages_min, lim.averages_max)
        self.cfg.scan.averages = int(v)
        self._changed(f"{int(v)} scans averaged per acquisition", clamped)

    def set_dark_subtract(self, on: bool) -> None:
        """Processing only: raw scans do not change, so nothing restarts; the
        check that a matching dark exists happens at the trigger."""
        self.cfg.scan.dark_subtract = bool(on)
        self._emit("info", "dark subtraction on" if on else "dark subtraction off")

    def set_continuous(self, on: bool) -> None:
        self.cfg.scan.continuous = bool(on)
        self._emit("info", "continuous scanning on" if on else "scan on trigger only")

    def window_min_limits(self) -> tuple[float, float]:
        return self.wl_range()[0], self.cfg.analysis.window_max_nm - self.cfg.limits.min_window_nm

    def window_max_limits(self) -> tuple[float, float]:
        return self.cfg.analysis.window_min_nm + self.cfg.limits.min_window_nm, self.wl_range()[1]

    def set_window_min(self, nm: float) -> None:
        v, clamped = _clamp(_finite(nm, "window start"), *self.window_min_limits())
        self.cfg.analysis.window_min_nm = v
        self._emit("warn" if clamped else "info",
                   f"analysis window from {v:.2f} nm" + (" (clamped)" if clamped else ""))

    def set_window_max(self, nm: float) -> None:
        v, clamped = _clamp(_finite(nm, "window end"), *self.window_max_limits())
        self.cfg.analysis.window_max_nm = v
        self._emit("warn" if clamped else "info",
                   f"analysis window to {v:.2f} nm" + (" (clamped)" if clamped else ""))

    def set_window(self, lo_nm: float, hi_nm: float) -> None:
        """Both ends at once, in an order that cannot be refused by the other end."""
        lo, hi = sorted((_finite(lo_nm, "window start"), _finite(hi_nm, "window end")))
        if lo >= self.cfg.analysis.window_max_nm:
            self.set_window_max(hi)
            self.set_window_min(lo)
        else:
            self.set_window_min(lo)
            self.set_window_max(hi)

    def wl_range(self) -> tuple[float, float]:
        """The instrument's wavelength range (from its calibration); 200-1000 nm
        before it is known."""
        wl = self._wl
        if wl.size and np.isfinite(wl).any():
            return float(np.nanmin(wl)), float(np.nanmax(wl))
        return 200.0, 1000.0

    # ---- the simulated world -----------------------------------------------------

    def set_light(self, on: bool) -> None:
        """SIMULATOR: light on the input fibre. Deliberately does NOT restart an
        acquisition: light changing is the world changing, as a shutter would."""
        self._need_sim()
        self.cfg.sim.light_on = bool(on)
        self._emit("info", "light on the input" if on else "input dark (light off)")

    def set_sim(self, name: str, value: float) -> None:
        self._need_sim()
        if name not in SIM_LIMITS:
            raise ValueError(f"unknown sim parameter {name!r}; one of {', '.join(SIM_LIMITS)}")
        v, clamped = _clamp(_finite(value, name), *SIM_LIMITS[name])
        setattr(self.cfg.sim, name, v)
        self._emit("warn" if clamped else "info",
                   f"sim {name} = {v:g}" + (" (clamped)" if clamped else ""))

    def _need_sim(self):
        if not self.simulated:
            raise ValueError("only the simulator has a pretend light source")

    # ---- the scan-safe read ------------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its id immediately. The clock starts
        NOW: call it after everything the measurement depends on has been set.

        Refused (ValueError) when dark subtraction is on and there is no dark
        for the current integration time: better a scan that stops and says
        why than one that records the wrong background."""
        self._check_dark_ready()
        return self._trigger(dark=False)

    def take_dark(self) -> int:
        """Start an acquisition that becomes THE dark spectrum when it completes.
        Block the light first. Wait for it exactly as for `acquire`."""
        return self._trigger(dark=True)

    def clear_dark(self) -> None:
        with self._lock:
            had = self._dark is not None
            self._dark = None
        if had:
            self._emit("info", "dark spectrum cleared")

    def abort(self) -> None:
        """Cancel a running acquisition. It is latched as ABORTED rather than
        simply dropped: a caller waiting for "acq_id == n and not acquiring"
        would otherwise see exactly that and read the PREVIOUS spectrum.

        Aborting a take_dark also CLEARS the old dark: whoever asked for a new
        one must not carry on subtracting an older one without knowing."""
        cleared = False
        with self._lock:
            a = self._acq
            if a is None:
                return
            self._acq = None
            self._sample = {"acq_id": a["id"], "aborted": True, "time": time.time(),
                            "dark": bool(a["dark"])}
            self._sample_trace = None
            if a["dark"] and self._dark is not None:
                self._dark, cleared = None, True
        self._emit("warn", f"acquisition #{a['id']} aborted")
        if cleared:
            self._emit("warn", "dark cleared: taking the new one was aborted")

    def get_sample(self) -> dict:
        with self._lock:
            return dict(self._sample)

    def get_trace(self, which: str = "sample") -> dict:
        """A spectrum and what it was measured under.

        which  "sample" (the latched acquisition, what a scan records), "last"
               (the newest scan) or "dark" (the latched dark).
        The array is under key "spectrum" (full-scale units, dark-subtracted
        when `dark_applied`), plus "wavelengths_nm".

        Raises ValueError when there is nothing honest to return."""
        with self._lock:
            if which == "last":
                t = self._last
                if t is None:
                    raise ValueError("no scan finished yet")
            elif which == "sample":
                if self._sample.get("aborted"):
                    raise ValueError(f"acquisition #{self._sample['acq_id']} was aborted")
                if self._sample.get("error"):
                    raise ValueError(self._sample["error"])
                t = self._sample_trace
                if t is None:
                    raise ValueError("no acquisition latched yet")
            elif which == "dark":
                t = self._dark
                if t is None:
                    raise ValueError("no dark spectrum: take one first (take_dark)")
            else:
                raise ValueError(f"which must be 'sample', 'last' or 'dark', got {which!r}")
            t = dict(t)
            now = self._clock()
        taken = t.pop("taken_at", None)
        t.pop("raw", None)
        if taken is not None:
            t["age_s"] = now - taken
        t["wavelengths_nm"] = self._wl.copy()
        return t

    def wavelengths(self) -> np.ndarray:
        """nm of every pixel (a scan reads it once, before starting)."""
        return self._wl.copy()

    # ---- status ---------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        c = self.cfg
        sc, sim = c.scan, c.sim
        wl_lo, wl_hi = self.wl_range()
        with self._lock:
            a = self._acq
            last = self._last or {}
            progress = 0.0
            if self._scanning and self._scan_dt > 0:
                progress = min(1.0, (self._clock() - self._scan_t0) / self._scan_dt)
            acq_progress = 0.0
            if a is not None:
                acq_progress = min(1.0, (a["n"] + progress * self._scanning) / a["want"])
            s = self.simulated
            return Status(
                connected=self._connected, idn=self._idn, hw_error=self._hw_error,
                simulated=s, pixels=int(self._wl.size), wl_min_nm=wl_lo, wl_max_nm=wl_hi,
                integration_time_s=sc.integration_time_s, averages=int(sc.averages),
                dark_subtract=bool(sc.dark_subtract), continuous=bool(sc.continuous),
                window_min_nm=c.analysis.window_min_nm, window_max_nm=c.analysis.window_max_nm,
                scan_time_s=sc.integration_time_s + 0.004,
                scanning=self._scanning, scan_progress=progress,
                scans=self._scans, trace_id=self._trace_id,
                peak_nm=last.get("peak_nm", _NAN),
                peak_intensity=last.get("peak_intensity", _NAN),
                integrated=last.get("integrated", _NAN),
                exposure=last.get("exposure", _NAN),
                saturated=bool(last.get("saturated", False)),
                live_dark_applied=bool(last.get("dark_applied", False)),
                light_on=bool(sim.light_on) if s else False,
                lamp_level_per_s=sim.lamp_level_per_s if s else _NAN,
                lamp_temperature_K=sim.lamp_temperature_K if s else _NAN,
                line_level_per_s=sim.line_level_per_s if s else _NAN,
                line_fwhm_nm=sim.line_fwhm_nm if s else _NAN,
                dark_rate_per_s=sim.dark_rate_per_s if s else _NAN,
                offset=sim.offset if s else _NAN,
                read_noise=sim.read_noise if s else _NAN,
                acq_id=self._acq_id, acquiring=a is not None, acq_progress=acq_progress,
                acq_is_dark=bool(a is not None and a["dark"]),
                sample=dict(self._sample),
                dark=self._dark_status_locked(),
            )

    def _dark_status_locked(self) -> dict:
        d = self._dark
        if d is None:
            return _no_dark()
        return {"present": True, "acq_id": d["acq_id"],
                "integration_time_s": d["integration_time_s"], "averages": d["averages"],
                "age_s": self._clock() - d["taken_at"],
                "matches": _same_time(d["integration_time_s"], self.cfg.scan.integration_time_s)}

    # ---- config (Settings dialog / wire) ------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp cfg (possibly edited in place over the wire)."""
        self._sanitise_config()
        self._changed("settings applied", False)

    # ---- the scan thread ------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()

    def step(self) -> bool:
        """One pass of the scan thread: one scan if there is a reason to scan.
        Returns True if a scan finished. Public so tests can drive it by hand."""
        with self._lock:
            wanted = self._acq is not None or self.cfg.scan.continuous
        if not wanted:
            time.sleep(0.05)
            return False
        try:
            return self._scan_once()
        except Exception as exc:              # never let the scan thread die
            with self._lock:
                self._scanning = False
            with self._hw:
                self._discard_pending()
            self._report_hw_error(exc)
            time.sleep(0.5)
            return False

    def _scan_once(self) -> bool:
        # An abandoned exposure may still be running on the CCD: wait until the
        # backend has thrown it away, or its data would answer THIS start.
        # Polled without the hardware lock, like the exposure wait below.
        while True:
            with self._hw:
                busy = bool(getattr(self.backend, "busy", lambda: False)())
            if not busy:
                break
            if self._stop.is_set():
                return False
            time.sleep(0.005)
        # Integration time and revision are read TOGETHER. A setter writes the
        # config first and bumps the revision second; read apart, the thread
        # could take the old time and the new revision and file a scan exposed
        # at the old time as valid. Read together, the worst case is a scan at
        # the new time with the old revision -- thrown away, never mis-filed.
        with self._lock:
            t_int = float(self.cfg.scan.integration_time_s)
            rev0, acq0 = self._rev, self._acq_id
        t0 = self._clock()
        with self._hw:
            self.backend.start_scan(t_int)
        dt = t_int + 0.004
        with self._lock:
            self._scanning, self._scan_t0, self._scan_dt = True, t0, dt
        give_up = t0 + dt + float(self.cfg.hardware.scan_timeout_s)

        # Wait out the exposure WITHOUT the hardware lock. Abandon it the moment
        # it can no longer count: a setting changed, or a new trigger arrived
        # (the exposure began before the thing the trigger is about).
        while True:
            with self._lock:
                abandoned = self._rev != rev0 or self._acq_id != acq0
            if self._stop.is_set() or abandoned:
                with self._hw:
                    self._discard_pending()
                with self._lock:
                    self._scanning = False
                return False
            with self._hw:
                ready = self.backend.scan_ready()
            if ready:
                break
            now = self._clock()
            if now > give_up:
                raise TimeoutError(f"no scan data {now - t0:.1f} s after starting a "
                                   f"{t_int:g} s exposure")
            # poll fast for short exposures, gently for long ones
            time.sleep(min(0.02, max(0.001, (t0 + dt - now) / 4)))

        with self._hw:
            raw = np.asarray(self.backend.read_scan(), dtype=float)
        exposure = float(np.max(raw)) if raw.size else _NAN
        saturated = bool(raw.size and exposure >= SATURATION)

        latched = None
        with self._lock:
            self._scanning = False
            if self._rev != rev0:
                return False                  # changed during the read-out
            self._hw_error = ""
            self._scans += 1
            self._trace_id += 1
            dark = self._dark
            use_dark = (self.cfg.scan.dark_subtract and dark is not None
                        and _same_time(dark["integration_time_s"], t_int))
            spectrum = raw - dark["raw"] if use_dark else raw
            info = analyse(self._wl, spectrum, self.cfg.analysis.window_min_nm,
                           self.cfg.analysis.window_max_nm)
            self._last = {"trace_id": self._trace_id, "time": time.time(),
                          "integration_time_s": t_int, "averages": 1,
                          "dark_applied": bool(use_dark), "exposure": exposure,
                          "saturated": saturated, "spectrum": spectrum, **info}
            a = self._acq
            if a is not None and t0 >= a["t0"]:
                a["sum"] = raw.copy() if a["sum"] is None else a["sum"] + raw
                a["n"] += 1
                a["saturated"] = a["saturated"] or saturated
                a["exposure"] = max(a["exposure"], exposure)
                if a["n"] >= a["want"]:
                    # Clear "acquiring", publish the sample AND (for take_dark)
                    # the dark in the SAME critical section (gotcha #28). Split,
                    # a status frame could land in between saying "#n finished"
                    # while `sample` or `dark` is still the old one.
                    self._latch_locked(a, t_int)
                    self._acq = None
                    latched = dict(self._sample)
        if latched is not None:
            if latched.get("error"):
                self._emit("error", f"acquisition #{latched['acq_id']}: {latched['error']}")
            elif latched.get("saturated"):
                self._emit("warn", f"acquisition #{latched['acq_id']}: SATURATED (a pixel hit "
                                   "full scale): shorten the integration time")
            if latched.get("dark") and not latched.get("error"):
                self._emit("info", f"dark taken (#{latched['acq_id']}) at "
                                   f"{t_int * 1e3:.6g} ms, {latched['averages']} scans")
        return True

    def _latch_locked(self, a: dict, t_int: float) -> None:
        """Average an acquisition's scans and publish them as THE sample (and as
        the dark, if that is what it was for). Called with _lock held."""
        mean = a["sum"] / a["n"]
        base = {"acq_id": a["id"], "time": time.time(), "averages": a["n"],
                "dark": bool(a["dark"]), "integration_time_s": t_int,
                "saturated": bool(a["saturated"]), "exposure": a["exposure"]}
        if a["dark"]:
            spectrum, applied = mean, False
            self._dark = {**base, "raw": mean, "spectrum": mean, "taken_at": self._clock()}
        elif self.cfg.scan.dark_subtract:
            d = self._dark
            if d is None or not _same_time(d["integration_time_s"], t_int):
                # The dark vanished or stopped fitting while this acquisition ran
                # (cleared, or the integration time changed and restarted it).
                self._sample = {**base, "error": "dark subtraction is on but there is no dark "
                                                 f"for {t_int * 1e3:.6g} ms: take_dark first"}
                self._sample_trace = None
                return
            spectrum, applied = mean - d["raw"], True
        else:
            spectrum, applied = mean, False
        info = analyse(self._wl, spectrum, self.cfg.analysis.window_min_nm,
                       self.cfg.analysis.window_max_nm)
        self._sample = {**base, "dark_applied": applied,
                        "window_min_nm": self.cfg.analysis.window_min_nm,
                        "window_max_nm": self.cfg.analysis.window_max_nm, **info}
        self._sample_trace = {**self._sample, "spectrum": spectrum}

    # ---- internals ----------------------------------------------------------------------

    def _check_dark_ready(self) -> None:
        if not self.cfg.scan.dark_subtract:
            return
        t = self.cfg.scan.integration_time_s
        with self._lock:
            d = self._dark
        if d is None:
            raise ValueError("dark subtraction is on but there is no dark spectrum: "
                             "block the light and take_dark first (or turn subtraction off)")
        if not _same_time(d["integration_time_s"], t):
            raise ValueError(f"dark subtraction is on but the dark was taken at "
                             f"{d['integration_time_s'] * 1e3:.6g} ms and the integration time "
                             f"is {t * 1e3:.6g} ms: take a new dark")

    def _trigger(self, dark: bool) -> int:
        if not self._connected:
            raise ValueError("not connected")
        cleared = False
        with self._lock:
            # A new trigger abandons a running acquisition. If that one was a
            # take_dark, the dark it was meant to replace is stale by intent.
            old = self._acq
            if old is not None and old["dark"] and self._dark is not None:
                self._dark, cleared = None, True
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can ever show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = self._new_acq(self._acq_id, dark)
            n = self._acq_id
        if cleared:
            self._emit("warn", "dark cleared: taking it was interrupted by a new trigger")
        return n

    def _new_acq(self, acq_id: int, dark: bool) -> dict:
        return {"id": acq_id, "t0": self._clock(), "want": max(1, int(self.cfg.scan.averages)),
                "n": 0, "sum": None, "saturated": False, "exposure": 0.0, "dark": bool(dark)}

    def _changed(self, msg: str, clamped: bool) -> None:
        """A scan-altering change: bump the revision and restart a running
        acquisition from scratch under the new settings (a take_dark stays one)."""
        restarted = None
        with self._lock:
            self._rev += 1
            if self._acq is not None:
                restarted = self._acq["id"]
                self._acq = self._new_acq(restarted, self._acq["dark"])
        self._emit("warn" if clamped else "info", msg + (" (clamped)" if clamped else ""))
        if restarted is not None:
            self._emit("warn", f"acquisition #{restarted} restarted: settings changed")

    def _discard_pending(self) -> None:
        try:
            self.backend.abort_scan()
        except Exception:
            pass

    def _sanitise_config(self) -> None:
        sc, lim, an = self.cfg.scan, self.cfg.limits, self.cfg.analysis
        sc.integration_time_s = _clamp(float(sc.integration_time_s),
                                       lim.integration_min_s, lim.integration_max_s)[0]
        sc.averages = int(_clamp(int(sc.averages), lim.averages_min, lim.averages_max)[0])
        lo, hi = self.wl_range()
        an.window_min_nm = _clamp(float(an.window_min_nm), lo, hi - lim.min_window_nm)[0]
        an.window_max_nm = _clamp(float(an.window_max_nm),
                                  an.window_min_nm + lim.min_window_nm, hi)[0]
        for name, (a, b) in SIM_LIMITS.items():
            setattr(self.cfg.sim, name, _clamp(float(getattr(self.cfg.sim, name)), a, b)[0])
        if self.cfg.hardware.calibration not in ("factory", "user"):
            self.cfg.hardware.calibration = "factory"

    def _report_hw_error(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._hw_error = msg
        now = self._clock()
        if now - self._last_err_emit >= 5.0:      # rate-limit: one event per 5 s
            self._last_err_emit = now
            self._emit("error", f"scan failed: {msg}")

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
