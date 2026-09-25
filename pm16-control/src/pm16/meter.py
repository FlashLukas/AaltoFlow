"""The PowerMeter: the brain between the wire and the backend.

Its SETTINGS are set-and-forget like the SMB100A's (wavelength, auto/manual
range): clamp, push, read back, report. Its READING needs more care, for the
same reason as the hf2 lock-in: a scan must never record a value that was
measured before the scan step it is filed under.

Two ways to read it:

  live      the latest reading from the polling thread. Right for a front
            panel. Not guaranteed fresh for a scan point.

  acquire   the scan-safe read. `acquire()` returns an id at once (the suite's
            fire-and-forget contract). The polling thread then averages the
            next `acquisition.readings` readings whose measurement STARTED
            after the trigger, and latches the mean as `sample`. On the PM16
            every reading is a new 60 ms average (measured), so "started after
            the trigger" really does mean "light that arrived after the
            trigger". Callers wait until status shows `acq_id` == their id
            AND `acquiring` False -- the id check stops a stale "not
            acquiring" frame from the previous point fooling them.

Threads and locks (the same two rules as hf2, both learned the hard way):

  * ONE polling thread owns the readings. `status()` only copies what that
    thread stored and never touches the hardware, so a slow USB call cannot
    stall the status publisher, and a dead link shows as `hw_error` instead of
    a healthy-looking panel full of old numbers.
  * EVERY backend call runs under `_hw` (an RLock), because the command thread
    and the polling thread would otherwise talk to the meter at once. A setter
    therefore waits for at most one reading in progress (~60 ms).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .backends.base import FLAG_OK, PowerMeterBackend
from .config import Config

_NAN = float("nan")


@dataclass
class Status:
    """One snapshot of the power meter, for status() and the wire."""

    connected: bool
    idn: str = ""
    sensor: str = ""
    hw_error: str = ""
    # the live reading
    power_W: float = _NAN
    flag: str = ""                     # "", "overrange", "underrun", "nan"
    read_ms: float = _NAN              # how long the last reading took
    readings: int = 0                  # live readings since start
    # settings: what we asked for (_set) and what the meter applied
    wavelength_set_nm: float = _NAN
    wavelength_nm: float = _NAN
    wavelength_min_nm: float = _NAN
    wavelength_max_nm: float = _NAN
    auto_range: bool = True
    range_set_W: float = _NAN
    range_W: float = _NAN
    range_min_W: float = _NAN
    range_max_W: float = _NAN
    average_time_s: float = _NAN
    # zero adjustment
    zeroing: bool = False
    dark_offset: float = _NAN
    # acquisition
    acq_readings: int = 1
    acq_id: int = 0
    acquiring: bool = False
    acq_progress: float = 0.0
    sample: dict = field(default_factory=dict)


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def _finite(value, what: str) -> float:
    """float(value), refusing NaN and inf -- NaN passes every `<`/`>` clamp and
    would be sent straight to the instrument."""
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return v


class PowerMeter:
    def __init__(self, backend: PowerMeterBackend, cfg: Config | None = None,
                 clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards the snapshot + acquisition

        self._connected = False
        self._idn = ""
        self._sensor = ""
        self._hw_error = ""
        self._last_err_emit = -1e9

        # what the meter reports (written under _hw by setters / start)
        self._dev_wl = (_NAN, _NAN)
        self._dev_range = (_NAN, _NAN)
        self._wl_actual = _NAN
        self._range_actual = _NAN
        self._avg_time = _NAN

        # written only by the polling thread (under _lock)
        self._power = _NAN
        self._flag = FLAG_OK
        self._read_ms = _NAN
        self._n_read = 0
        self._zeroing = False
        self._dark = _NAN

        # acquisition state (under _lock)
        self._acq_id = 0
        self._acq: dict | None = None
        self._sample: dict = {}

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- lifecycle -----------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Open the meter, adopt (or push) its settings, start polling.

        `poll=False` skips the thread, so a test can drive `poll_once()` by hand.
        """
        s = self.cfg.sensor
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            self._sensor = self.backend.sensor_name()
            self._dev_wl = self._try(self.backend.wavelength_range, self._dev_wl)
            self._dev_range = self._try(self.backend.range_limits, self._dev_range)
            self._avg_time = self._try(self.backend.average_time_s, _NAN)
            self._connected = True
            if self.cfg.hardware.push_on_start:
                self._sanitise_config()
                self._push_sensor()
            else:
                # The meter stores its wavelength; adopting it means connecting
                # never silently changes a measurement someone already set up.
                s.wavelength_nm = self.backend.get_wavelength()
                s.auto_range = self.backend.get_auto_range()
                if not s.auto_range:
                    s.range_W = self.backend.get_range()
                self._read_back()
            self._dark = self._try(self.backend.dark_offset, _NAN)
        self._emit("info", f"connected: {self._idn or 'power meter'}"
                           + (f", sensor {self._sensor}" if self._sensor else ""))
        self._emit("info", f"wavelength {self._wl_actual:g} nm, "
                           + ("auto range" if s.auto_range else
                              f"manual range {_fmt_W(self._range_actual)}"))
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="pm16-poll", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop polling and disconnect. Safe to call more than once. A power
        meter has no output to make safe, so nothing is changed on the way out."""
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
            with self._lock:
                self._acq = None
            if was:
                self._emit("info", "disconnected")

    # ---- settings (each clamps, stores in cfg, pushes, reads back) -----------

    def wavelength_limits(self) -> tuple[float, float]:
        """The config envelope narrowed by what the sensor is calibrated for."""
        lim = self.cfg.limits
        lo, hi = lim.wavelength_min_nm, lim.wavelength_max_nm
        dlo, dhi = self._dev_wl
        if math.isfinite(dlo):
            lo = max(lo, dlo)
        if math.isfinite(dhi):
            hi = min(hi, dhi)
        return lo, hi

    def range_limits(self) -> tuple[float, float]:
        lim = self.cfg.limits
        lo, hi = lim.range_min_W, lim.range_max_W
        dlo, dhi = self._dev_range
        if math.isfinite(dlo):
            lo = max(lo, dlo)
        if math.isfinite(dhi):
            hi = min(hi, dhi)
        return lo, hi

    def set_wavelength(self, nm: float) -> None:
        lo, hi = self.wavelength_limits()
        value, clamped = _clamp(_finite(nm, "wavelength"), lo, hi)
        self.cfg.sensor.wavelength_nm = value
        if self._connected:
            with self._hw:
                self.backend.set_wavelength(value)
                self._read_back()
        if clamped:
            self._emit("warn", f"wavelength clamped to {value:g} nm (limit {lo:g}..{hi:g})")
        else:
            self._emit("info", f"wavelength = {self._wl_actual:g} nm")

    def set_auto_range(self, on: bool) -> None:
        s = self.cfg.sensor
        s.auto_range = bool(on)
        if self._connected:
            with self._hw:
                self.backend.set_auto_range(s.auto_range)
                if not s.auto_range:
                    # Hand over from auto to manual WITHOUT a jump: keep the
                    # range auto had chosen, and remember it as the setpoint.
                    s.range_W = self.backend.get_range()
                self._read_back()
        self._emit("info", "auto range on" if s.auto_range else
                   f"manual range {_fmt_W(self._range_actual)}")

    def set_range(self, watts: float) -> None:
        """Manual range. Switches auto-range off: asking for a range and then
        letting auto overrule it would silently ignore the request."""
        lo, hi = self.range_limits()
        value, clamped = _clamp(_finite(watts, "range"), lo, hi)
        s = self.cfg.sensor
        s.range_W = value
        s.auto_range = False
        if self._connected:
            with self._hw:
                self.backend.set_auto_range(False)
                self.backend.set_range(value)
                self._read_back()
        msg = f"range {_fmt_W(self._range_actual)} (asked {_fmt_W(value)})"
        self._emit("warn" if clamped else "info",
                   msg + (f", clamped to {_fmt_W(lo)}..{_fmt_W(hi)}" if clamped else ""))

    def set_acquisition(self, readings: int) -> None:
        lim = self.cfg.limits
        n = int(round(_finite(readings, "readings")))
        value, clamped = _clamp(n, lim.readings_min, lim.readings_max)
        self.cfg.acquisition.readings = int(value)
        self._emit("warn" if clamped else "info",
                   f"acquire averages {int(value)} readings"
                   + (" (clamped)" if clamped else ""))

    # ---- zero (dark) adjustment ---------------------------------------------

    def zero(self) -> None:
        """Start the dark adjustment. COVER THE SENSOR FIRST -- whatever light
        reaches it now becomes the new zero."""
        if not self._connected:
            raise ValueError("not connected")
        with self._lock:
            if self._acq is not None:
                raise ValueError("an acquisition is running; zero afterwards")
        with self._hw:
            self.backend.start_zero()
        with self._lock:
            self._zeroing = True
        self._emit("warn", "zero adjustment started (sensor must be covered)")

    def cancel_zero(self) -> None:
        if self._connected:
            with self._hw:
                self.backend.cancel_zero()
        self._emit("info", "zero adjustment cancelled")

    # ---- the scan-safe read ---------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its id immediately. The clock starts
        NOW: call it after everything the measurement depends on has been set."""
        if not self._connected:
            raise ValueError("not connected")
        n = max(1, int(self.cfg.acquisition.readings))
        with self._lock:
            if self._zeroing:
                raise ValueError("zero adjustment running; no readings possible")
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can ever show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = {"id": self._acq_id, "t0": self._clock(), "want": n,
                         "vals": [], "flags": set()}
            return self._acq_id

    def get_sample(self) -> dict:
        with self._lock:
            return dict(self._sample)

    # ---- status ------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        s = self.cfg.sensor
        wlo, whi = self.wavelength_limits()
        rlo, rhi = self.range_limits()
        with self._lock:
            a = self._acq
            return Status(
                connected=self._connected, idn=self._idn, sensor=self._sensor,
                hw_error=self._hw_error,
                power_W=self._power, flag=self._flag, read_ms=self._read_ms,
                readings=self._n_read,
                wavelength_set_nm=s.wavelength_nm, wavelength_nm=self._wl_actual,
                wavelength_min_nm=wlo, wavelength_max_nm=whi,
                auto_range=s.auto_range, range_set_W=s.range_W,
                range_W=self._range_actual, range_min_W=rlo, range_max_W=rhi,
                average_time_s=self._avg_time,
                zeroing=self._zeroing, dark_offset=self._dark,
                acq_readings=int(self.cfg.acquisition.readings),
                acq_id=self._acq_id, acquiring=a is not None,
                acq_progress=0.0 if a is None else len(a["vals"]) / a["want"],
                sample=dict(self._sample),
            )

    # ---- config (Settings dialog / wire) ------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp cfg (possibly edited in place over the wire) and push it."""
        self._sanitise_config()
        if self._connected:
            with self._hw:
                self._push_sensor()
        self._emit("info", "settings applied")

    # ---- polling --------------------------------------------------------------------

    def _poll_loop(self) -> None:
        period = 1.0 / max(1.0, float(self.cfg.hardware.poll_hz))
        while not self._stop.is_set():
            t = self._clock()
            self.poll_once()
            # A PM16 reading blocks ~60 ms by itself; only sleep what is left of
            # the period, so the loop runs as fast as the meter allows.
            self._stop.wait(max(0.005, period - (self._clock() - t)))

    def poll_once(self) -> None:
        """One reading (or one zero-state check), then advance any acquisition.
        Public so tests and single-threaded scripts can drive it."""
        with self._lock:
            zeroing = self._zeroing
        try:
            if zeroing:
                with self._hw:
                    running = self.backend.zero_running()
                    dark = self.backend.dark_offset() if not running else _NAN
                if not running:
                    with self._lock:
                        self._zeroing = False
                        self._dark = dark
                    self._emit("info", f"zero adjustment finished, dark offset {dark:.4g}")
                return
            with self._hw:
                t_start = self._clock()
                power, flag = self.backend.measure_power()
                t_end = self._clock()
                auto_range = self.cfg.sensor.auto_range
                rng = self.backend.get_range() if auto_range else None
        except Exception as exc:          # never let the polling thread die
            self._report_hw_error(exc)
            return

        recovered = False
        with self._lock:
            recovered = bool(self._hw_error)
            self._hw_error = ""
            self._power = float(power)
            self._flag = flag
            self._read_ms = (t_end - t_start) * 1e3
            self._n_read += 1
            if rng is not None:
                self._range_actual = float(rng)
            self._advance_acquisition(t_start, float(power), flag)
        if recovered:
            self._emit("info", "hardware reads recovered")

    def _advance_acquisition(self, t_start: float, power: float, flag: str) -> None:
        """Called with _lock held. Only readings that STARTED after the trigger count."""
        a = self._acq
        if a is None or t_start < a["t0"]:
            return
        a["vals"].append(power)
        if flag:
            a["flags"].add(flag)
        if len(a["vals"]) < a["want"]:
            return
        vals = a["vals"]
        n = len(vals)
        mean = sum(vals) / n
        std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
        self._sample = {"acq_id": a["id"], "power_W": mean, "std_W": std, "n": n,
                        "flag": ",".join(sorted(a["flags"])),
                        "wavelength_nm": self._wl_actual, "time": time.time()}
        self._acq = None

    def _report_hw_error(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._hw_error = msg
        now = self._clock()
        if now - self._last_err_emit >= 5.0:      # rate-limit: one event per 5 s
            self._last_err_emit = now
            self._emit("error", f"hardware read failed: {msg}")

    # ---- internals ---------------------------------------------------------------------

    def _sanitise_config(self) -> None:
        s, lim = self.cfg.sensor, self.cfg.limits
        s.wavelength_nm = _clamp(float(s.wavelength_nm), *self.wavelength_limits())[0]
        s.range_W = _clamp(float(s.range_W), *self.range_limits())[0]
        a = self.cfg.acquisition
        a.readings = int(_clamp(int(a.readings), lim.readings_min, lim.readings_max)[0])

    def _push_sensor(self) -> None:
        """Send the sensor settings to the meter (with _hw held)."""
        s = self.cfg.sensor
        self.backend.set_wavelength(s.wavelength_nm)
        self.backend.set_auto_range(s.auto_range)
        if not s.auto_range:
            self.backend.set_range(s.range_W)
        self._read_back()

    def _read_back(self) -> None:
        """What the meter actually applied (with _hw held)."""
        self._wl_actual = float(self.backend.get_wavelength())
        self._range_actual = float(self.backend.get_range())

    @staticmethod
    def _try(fn, default):
        """For optional queries: a meter that refuses one should not stop start-up."""
        try:
            return fn()
        except Exception:
            return default

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)


def _fmt_W(watts: float) -> str:
    """0.00123 -> '1.23 mW', human-scaled for the event log (ASCII: 'uW')."""
    w = float(watts)
    if not math.isfinite(w):
        return "--"
    for scale, unit in ((1.0, "W"), (1e-3, "mW"), (1e-6, "uW"), (1e-9, "nW")):
        if abs(w) >= scale:
            return f"{w / scale:.4g} {unit}"
    return f"{w / 1e-12:.4g} pW"
