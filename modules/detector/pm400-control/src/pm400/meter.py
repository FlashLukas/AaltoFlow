"""Pm400Meter: the brain between the wire and the backend.

The PM400 is a CONSOLE: the head plugged into it decides what is measured.

  photodiode / thermal head -> power (W), with auto/manual range and averaging
  pyroelectric head         -> energy per pulse (J), manual energy range only
  no head                   -> nothing to measure; acquire is refused

The brain asks the console which head is present at start and every
`hardware.head_check_s` after that. When the head changes it re-reads the
head's limits and ADOPTS its settings (a console loads a head's defaults when
it is plugged in), aborts whatever acquisition or zero was running, and the
`describe` manifest changes with it (units, which controls exist, limits), so
every client re-fetches it.

Its SETTINGS are set-and-forget (wavelength, range, averaging time): clamp,
push, read back, report. Its READING needs more care, as on hf2 and pm16: a
scan must never record a value measured before the scan step it is filed under.

  live      the latest reading from the polling thread. Right for a panel.
  acquire   the scan-safe read. `acquire()` returns an id at once (the suite's
            fire-and-forget contract). The polling thread then averages the
            next `acquisition.readings` readings whose measurement STARTED at
            least `acquisition.settle_s` after the trigger, and latches the
            mean as `sample`. settle_s is for SLOW heads: a thermal absorber
            needs ~5 time constants (several seconds) to follow a change of
            the light; a photodiode needs none.

Threads and locks (the same rules as hf2 and pm16):

  * ONE polling thread owns the readings. `status()` only copies what that
    thread stored and never touches the hardware (gotcha #1), so a slow USB
    call cannot stall the status publisher, and a dead link shows as
    `hw_error` instead of a healthy-looking panel full of old numbers.
  * EVERY backend call runs under `_hw` (an RLock). A setter therefore waits
    for at most one reading in progress -- one averaging time, which is why
    limits.avg_time_max_s is kept at 1 s.
  * Finishing an acquisition or a zero clears "busy" and publishes the result
    in ONE critical section (gotcha #28), so no status frame can say "done"
    next to the previous result.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .backends.base import (FLAG_OK, HEAD_NONE, HEAD_PHOTODIODE, HEAD_PYRO,
                            HEAD_THERMAL, ConsoleBackend, empty_sensor_info)
from .config import Config

_NAN = float("nan")
_NAN2 = (_NAN, _NAN)


@dataclass
class Status:
    """One snapshot of the console, for status() and the wire."""

    connected: bool
    idn: str = ""
    hw_error: str = ""
    # the head
    head: str = HEAD_NONE             # photodiode / thermal / pyro / none / other
    sensor: str = ""                  # head name (+ serial) as the console reports it
    quantity: str = "none"            # "power", "energy" or "none"
    unit: str = ""                    # "W" or "J"
    # the live reading (in `unit`)
    value: float = _NAN
    flag: str = ""                     # "", "overrange", "underrun", "nan", "no_sensor"
    read_ms: float = _NAN              # how long the last reading took
    readings: int = 0                  # live readings since start
    rep_rate_Hz: float = _NAN          # pulse rate seen by a pyro head
    # settings: what we asked for (_set) and what the console applied
    wavelength_settable: bool = True
    wavelength_set_nm: float = _NAN
    wavelength_nm: float = _NAN
    wavelength_min_nm: float = _NAN
    wavelength_max_nm: float = _NAN
    auto_range_available: bool = True
    auto_range: bool = True
    range_set: float = _NAN            # in `unit`
    range: float = _NAN
    range_min: float = _NAN
    range_max: float = _NAN
    avg_time_set_s: float = _NAN
    avg_time_s: float = _NAN
    avg_time_min_s: float = _NAN
    avg_time_max_s: float = _NAN
    # zero adjustment
    zero_supported: bool = False
    zeroing: bool = False
    zero_id: int = 0
    zero_error: str = ""               # "OK" when the last zero finished
    dark_offset: float = _NAN
    dark_unit: str = ""                # A (photodiode) or V (thermopile)
    # acquisition
    acq_readings: int = 1
    acq_settle_s: float = 0.0
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


def _narrow(lo: float, hi: float, dev: tuple[float, float]) -> tuple[float, float]:
    """The config envelope narrowed by what the device reports (if it did)."""
    dlo, dhi = dev
    if math.isfinite(dlo):
        lo = max(lo, dlo)
    if math.isfinite(dhi):
        hi = min(hi, dhi)
    return lo, hi


class Pm400Meter:
    def __init__(self, backend: ConsoleBackend, cfg: Config | None = None,
                 clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards the snapshot + acquisition

        self._connected = False
        self._idn = ""
        self._hw_error = ""
        self._last_err_emit = -1e9

        # the head, and what the console reports for it (written under _hw)
        self._head = empty_sensor_info()
        self._dev_wl = _NAN2
        self._dev_range = _NAN2
        self._dev_avg = _NAN2
        self._wl_actual = _NAN
        self._range_actual = _NAN
        self._avg_actual = _NAN
        self._last_head_check = -1e9

        # written only by the polling thread (under _lock)
        self._value = _NAN
        self._flag = FLAG_OK
        self._read_ms = _NAN
        self._n_read = 0
        self._rep_rate = _NAN

        # zero adjustment (under _lock)
        self._zeroing = False
        self._zero_id = 0
        self._zero_error = ""
        self._dark = _NAN

        # acquisition state (under _lock)
        self._acq_id = 0
        self._acq: dict | None = None
        self._sample: dict = {}

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- what the head measures ------------------------------------------------

    @property
    def head_kind(self) -> str:
        return self._head["kind"]

    @property
    def quantity(self) -> str:
        """'power', 'energy' or 'none' -- the head decides."""
        if self._head["kind"] not in (HEAD_PHOTODIODE, HEAD_THERMAL, HEAD_PYRO):
            return "none"
        return "energy" if self._head["energy"] else "power"

    @property
    def unit(self) -> str:
        return {"power": "W", "energy": "J"}.get(self.quantity, "")

    def _is_energy(self) -> bool:
        return self.quantity == "energy"

    # ---- lifecycle -----------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Open the console, find the head, ADOPT its settings, start polling.

        Start-up only READS the console (Lukas's rule, 2026-09-27): wavelength,
        auto range / range, averaging time and dark offset are queried and
        copied into cfg.sensor and the status, so the GUI and describe show what
        the console is actually doing. Nothing is written -- the config values
        reach the console only when the user sets them explicitly.
        `poll=False` skips the thread, so a test can drive `poll_once()` by hand."""
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            self._connected = True
            info = self._try(self.backend.sensor_info, empty_sensor_info())
            self._take_head(info, adopt=True)
            self._last_head_check = self._clock()
        self._emit("info", f"connected: {self._idn or 'power meter console'}")
        self._announce_head()
        self._warn_adopted_outside_limits()
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="pm400-poll", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop polling and disconnect. Safe to call more than once. A power
        meter has no output to make safe, but a zero adjustment in progress is
        cancelled so the console is not left half-zeroed."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
        self._thread = None
        was = self._connected
        try:
            with self._hw:
                if self._zeroing:
                    self._try(self.backend.cancel_zero, None)
                self.backend.close()
        finally:
            self._connected = False
            with self._lock:
                self._abort_acquisition("service stopped")
                self._zeroing = False
            if was:
                self._emit("info", "disconnected")

    # ---- limits -----------------------------------------------------------------

    def wavelength_limits(self) -> tuple[float, float]:
        lim = self.cfg.limits
        return _narrow(lim.wavelength_min_nm, lim.wavelength_max_nm, self._dev_wl)

    def range_limits(self) -> tuple[float, float]:
        """In the head's unit: W for a power head, J for an energy head."""
        lim = self.cfg.limits
        if self._is_energy():
            return _narrow(lim.range_min_J, lim.range_max_J, self._dev_range)
        return _narrow(lim.range_min_W, lim.range_max_W, self._dev_range)

    def avg_time_limits(self) -> tuple[float, float]:
        lim = self.cfg.limits
        return _narrow(lim.avg_time_min_s, lim.avg_time_max_s, self._dev_avg)

    def acquire_timeout_s(self) -> float:
        """How long a scan may honestly wait for ONE acquisition: the settle
        time plus N readings (one averaging time each, or one pulse period on
        an energy head), with a generous margin for USB and a slow poll. Never
        less than the configured acquisition.timeout_s. Derived, so a scan with
        a 30 s settle or 1000 readings does not time out on a healthy meter."""
        a = self.cfg.acquisition
        if self._is_energy():
            # One pulse per reading. Budget 1 s each (pulses at >= 1 Hz) rather
            # than 1/measured rate: the measured rate jitters, and a timeout
            # that followed it would move describe_rev on every head check.
            per = 1.0
        else:
            avg = self._avg_actual if math.isfinite(self._avg_actual) else float(
                self.cfg.sensor.avg_time_s)
            per = max(avg, 1.0 / max(1.0, float(self.cfg.hardware.poll_hz)))
        need = float(a.settle_s) + int(a.readings) * per
        return max(float(a.timeout_s), 2.0 * need + 5.0)

    # ---- settings (each clamps, stores in cfg, pushes, reads back) -----------

    def set_wavelength(self, nm: float) -> None:
        if self._connected and self.quantity == "none":
            raise ValueError("no usable sensor head connected")
        if self._connected and not self._head.get("wavelength_settable", True):
            raise ValueError("this head has a fixed wavelength")
        lo, hi = self.wavelength_limits()
        value, clamped = _clamp(_finite(nm, "wavelength"), lo, hi)
        self.cfg.sensor.wavelength_nm = value
        if self._connected and self.quantity != "none":
            with self._hw:
                self.backend.set_wavelength(value)
                self._read_back()
        if clamped:
            self._emit("warn", f"wavelength clamped to {value:g} nm (limit {lo:g}..{hi:g})")
        else:
            self._emit("info", f"wavelength = {self._wl_actual:g} nm")

    def set_auto_range(self, on: bool) -> None:
        if self._is_energy():
            raise ValueError("an energy head has no auto range; set the energy range")
        s = self.cfg.sensor
        s.auto_range = bool(on)
        if self._connected and self.quantity == "power":
            with self._hw:
                self.backend.set_auto_range(s.auto_range)
                if not s.auto_range:
                    # Hand over from auto to manual WITHOUT a jump: keep the
                    # range auto had chosen, and remember it as the setpoint.
                    s.range_W = self.backend.get_range()
                self._read_back()
        self._emit("info", "auto range on" if s.auto_range else
                   f"manual range {_fmt(self._range_actual, 'W')}")

    def set_range(self, value: float) -> None:
        """Manual range, in the head's unit (W, or J on an energy head). On a
        power head it switches auto-range off: asking for a range and then
        letting auto overrule it would silently ignore the request."""
        lo, hi = self.range_limits()
        v, clamped = _clamp(_finite(value, "range"), lo, hi)
        s = self.cfg.sensor
        energy = self._is_energy()
        if energy:
            s.range_J = v
        else:
            s.range_W = v
            s.auto_range = False
        if self._connected and self.quantity != "none":
            with self._hw:
                if energy:
                    self.backend.set_energy_range(v)
                else:
                    self.backend.set_auto_range(False)
                    self.backend.set_range(v)
                self._read_back()
        u = self.unit or "W"
        msg = f"range {_fmt(self._range_actual, u)} (asked {_fmt(v, u)})"
        self._emit("warn" if clamped else "info",
                   msg + (f", clamped to {_fmt(lo, u)}..{_fmt(hi, u)}" if clamped else ""))

    def set_avg_time(self, seconds: float) -> None:
        """How long the console averages into ONE reading (power heads)."""
        if self._is_energy():
            raise ValueError("an energy head reads single pulses; no averaging time")
        lo, hi = self.avg_time_limits()
        v, clamped = _clamp(_finite(seconds, "averaging time"), lo, hi)
        self.cfg.sensor.avg_time_s = v
        if self._connected and self.quantity == "power":
            with self._hw:
                self.backend.set_avg_time(v)
                self._read_back()
        self._emit("warn" if clamped else "info",
                   f"averaging time {self._avg_actual * 1e3:.4g} ms"
                   + (f" (clamped to {lo * 1e3:g}..{hi * 1e3:g} ms)" if clamped else ""))

    def set_acquisition(self, readings: int) -> None:
        lim = self.cfg.limits
        n = int(round(_finite(readings, "readings")))
        value, clamped = _clamp(n, lim.readings_min, lim.readings_max)
        self.cfg.acquisition.readings = int(value)
        self._emit("warn" if clamped else "info",
                   f"acquire averages {int(value)} readings" + (" (clamped)" if clamped else ""))

    def set_settle(self, seconds: float) -> None:
        v, clamped = _clamp(_finite(seconds, "settle time"), 0.0, self.cfg.limits.settle_max_s)
        self.cfg.acquisition.settle_s = v
        self._emit("warn" if clamped else "info",
                   f"acquire ignores readings in the first {v:g} s" + (" (clamped)" if clamped else ""))

    # ---- zero (dark) adjustment ---------------------------------------------

    def zero(self) -> int:
        """Start the zero adjustment; returns its number at once. COVER THE HEAD
        FIRST -- whatever light reaches it now becomes the new zero."""
        if not self._connected:
            raise ValueError("not connected")
        if not self._head.get("zero_supported"):
            raise ValueError("this head does not support zero adjustment"
                             if self.quantity != "none" else "no sensor head connected")
        with self._lock:
            if self._acq is not None:
                raise ValueError("an acquisition is running; zero afterwards")
            if self._zeroing:
                raise ValueError("a zero adjustment is already running")
        # Mark "zeroing" while STILL holding _hw: otherwise the polling thread
        # can slip a measurement in between start_zero and the flag, on a
        # console that is already busy with its dark adjustment.
        with self._hw:
            try:
                self.backend.start_zero()
            except Exception as exc:
                raise ValueError(f"zero refused by the console: {exc}") from exc
            with self._lock:
                if self._acq is not None:
                    # an acquire slipped in between the check above and now
                    self._abort_acquisition("zero adjustment started")
                self._zero_id += 1
                self._zeroing = True
                self._zero_error = ""
                n = self._zero_id
        self._emit("warn", f"zero adjustment #{n} started (head must be covered)")
        return n

    def cancel_zero(self) -> None:
        if self._connected:
            with self._hw:
                self._try(self.backend.cancel_zero, None)
        with self._lock:
            was = self._zeroing
            self._zeroing = False
            if was:
                self._zero_error = "cancelled"
        if was:
            self._emit("info", "zero adjustment cancelled")

    # ---- the scan-safe read ---------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its id immediately. The clock starts
        NOW: call it after everything the measurement depends on has been set."""
        if not self._connected:
            raise ValueError("not connected")
        if self.quantity == "none":
            raise ValueError("no usable sensor head connected")
        n = max(1, int(self.cfg.acquisition.readings))
        settle = max(0.0, float(self.cfg.acquisition.settle_s))
        with self._lock:
            if self._zeroing:
                raise ValueError("zero adjustment running; no readings possible")
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can ever show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = {"id": self._acq_id, "t0": self._clock() + settle, "want": n,
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
        alo, ahi = self.avg_time_limits()
        head = self._head
        energy = self._is_energy()
        power = self.quantity == "power"
        name = head.get("name", "")
        with self._lock:
            a = self._acq
            return Status(
                connected=self._connected, idn=self._idn, hw_error=self._hw_error,
                head=head["kind"],
                sensor=name + (f" ({head['serial']})" if head.get("serial") and name else ""),
                quantity=self.quantity, unit=self.unit,
                value=self._value, flag=self._flag, read_ms=self._read_ms,
                readings=self._n_read, rep_rate_Hz=self._rep_rate if energy else _NAN,
                wavelength_settable=bool(head.get("wavelength_settable", True)),
                wavelength_set_nm=s.wavelength_nm, wavelength_nm=self._wl_actual,
                wavelength_min_nm=wlo, wavelength_max_nm=whi,
                auto_range_available=not energy,
                auto_range=bool(s.auto_range) and not energy,
                range_set=s.range_J if energy else s.range_W,
                range=self._range_actual, range_min=rlo, range_max=rhi,
                avg_time_set_s=s.avg_time_s if power else _NAN,
                avg_time_s=self._avg_actual if power else _NAN,
                avg_time_min_s=alo, avg_time_max_s=ahi,
                zero_supported=bool(head.get("zero_supported")),
                zeroing=self._zeroing, zero_id=self._zero_id, zero_error=self._zero_error,
                dark_offset=self._dark,
                dark_unit={HEAD_PHOTODIODE: "A", HEAD_THERMAL: "V"}.get(head["kind"], ""),
                acq_readings=int(self.cfg.acquisition.readings),
                acq_settle_s=float(self.cfg.acquisition.settle_s),
                acq_id=self._acq_id, acquiring=a is not None,
                acq_progress=0.0 if a is None else len(a["vals"]) / a["want"],
                sample=dict(self._sample),
            )

    # ---- config (Settings dialog / wire) ------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp cfg (possibly edited in place over the wire) and push it.

        The head is checked FIRST: on the simulator a Settings change of
        `sim.head` is a head swap, and the settings must be clamped to the head
        that is actually plugged in, not the one before."""
        if self._connected:
            self.check_head()
        self._sanitise_config()
        if self._connected and self.quantity != "none":
            with self._hw:
                self._push_sensor()
        self._emit("info", "settings applied")

    # ---- polling --------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            period = 1.0 / max(1.0, float(self.cfg.hardware.poll_hz))
            t = self._clock()
            self.poll_once()
            # A reading blocks for its whole averaging time by itself; only
            # sleep what is left of the period, so the loop runs as fast as the
            # console allows.
            self._stop.wait(max(0.005, period - (self._clock() - t)))

    def poll_once(self) -> None:
        """One head check (when due), then one reading or one zero-state check,
        then advance any acquisition. Public so tests can drive it."""
        if self._clock() - self._last_head_check >= float(self.cfg.hardware.head_check_s):
            self.check_head()
        with self._lock:
            zeroing = self._zeroing
        try:
            if zeroing:
                self._poll_zero()
                return
            q = self.quantity
            if q == "none":
                with self._lock:
                    self._value, self._flag = _NAN, "no_sensor"
                return
            with self._hw:
                t_start = self._clock()
                if q == "energy":
                    value, flag = self.backend.measure_energy()
                    rng = None
                else:
                    value, flag = self.backend.measure_power()
                    rng = self.backend.get_range() if self.cfg.sensor.auto_range else None
                t_end = self._clock()
        except Exception as exc:          # never let the polling thread die
            self._report_hw_error(exc)
            return

        with self._lock:
            recovered = bool(self._hw_error)
            self._hw_error = ""
            self._value = float(value)
            self._flag = flag
            self._read_ms = (t_end - t_start) * 1e3
            self._n_read += 1
            if rng is not None:
                self._range_actual = float(rng)
            self._advance_acquisition(t_start, float(value), flag)
        if recovered:
            self._emit("info", "hardware reads recovered")

    def check_head(self) -> None:
        """Ask the console which head is plugged in; follow a change. Called by
        the polling thread every hardware.head_check_s (public for tests)."""
        self._last_head_check = self._clock()
        try:
            with self._hw:
                info = self.backend.sensor_info()
                changed = _head_key(info) != _head_key(self._head)
                if changed:
                    self._take_head(info, adopt=True)
                rate = (self.backend.measure_frequency()
                        if self._is_energy() and not changed else _NAN)
        except Exception as exc:
            self._report_hw_error(exc)
            return
        if self._is_energy():
            with self._lock:
                self._rep_rate = float(rate)
        if changed:
            with self._lock:
                self._abort_acquisition("sensor head changed")
                if self._zeroing:
                    self._zeroing = False
                    self._zero_error = "sensor head changed"
                self._value, self._flag = _NAN, ""
            self._emit("warn", "sensor head changed")
            self._announce_head()

    def _poll_zero(self) -> None:
        with self._hw:
            running = self.backend.zero_running()
            dark = self._try(self.backend.dark_offset, _NAN) if not running else _NAN
        if not running:
            # busy flag and result in ONE critical section (gotcha #28)
            with self._lock:
                self._zeroing = False
                self._zero_error = "OK"
                self._dark = dark
                n = self._zero_id
            self._emit("info", f"zero adjustment #{n} finished, offset {dark:.4g}")

    def _advance_acquisition(self, t_start: float, value: float, flag: str) -> None:
        """Called with _lock held. Only readings that STARTED after the trigger
        (plus the settle time) count."""
        a = self._acq
        if a is None or t_start < a["t0"]:
            return
        a["vals"].append(value)
        if flag:
            a["flags"].add(flag)
        if len(a["vals"]) < a["want"]:
            return
        vals = a["vals"]
        n = len(vals)
        mean = sum(vals) / n
        std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
        # latch the result AND clear "acquiring" in this one locked section
        self._sample = {"acq_id": a["id"], "value": mean, "std": std, "n": n,
                        "flag": ",".join(sorted(a["flags"])), "unit": self.unit,
                        "quantity": self.quantity, "head": self.head_kind,
                        "wavelength_nm": self._wl_actual, "time": time.time()}
        self._acq = None

    def _abort_acquisition(self, why: str) -> None:
        """Called with _lock held. Latch an EMPTY sample under the running id,
        so a scan waiting for it gets NaN plus a reason, never the previous
        point's value (gotcha #28)."""
        a = self._acq
        if a is None:
            return
        self._sample = {"acq_id": a["id"], "value": _NAN, "std": _NAN, "n": 0,
                        "flag": f"aborted: {why}", "unit": self.unit,
                        "quantity": self.quantity, "head": self.head_kind,
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

    def _take_head(self, info: dict, adopt: bool) -> None:
        """A (new) head: read its limits and, if `adopt`, its settings (with _hw held)."""
        self._head = dict(info)
        b = self.backend
        s = self.cfg.sensor
        q = self.quantity
        if q == "none":
            self._dev_wl = self._dev_range = self._dev_avg = _NAN2
            self._wl_actual = self._range_actual = self._avg_actual = _NAN
            self._dark = _NAN
            return
        self._dev_wl = self._try(b.wavelength_range, _NAN2)
        if q == "energy":
            self._dev_range = self._try(b.energy_range_limits, _NAN2)
            self._dev_avg = _NAN2
        else:
            self._dev_range = self._try(b.range_limits, _NAN2)
            self._dev_avg = self._try(b.avg_time_limits, _NAN2)
        if adopt:
            # The console keeps its own settings; adopting them means that
            # connecting (or plugging in a head) never silently changes a
            # measurement someone already set up.
            s.wavelength_nm = self._try(b.get_wavelength, s.wavelength_nm)
            if q == "energy":
                s.range_J = self._try(b.get_energy_range, s.range_J)
            else:
                s.auto_range = self._try(b.get_auto_range, s.auto_range)
                if not s.auto_range:
                    s.range_W = self._try(b.get_range, s.range_W)
                s.avg_time_s = self._try(b.get_avg_time, s.avg_time_s)
        self._read_back()
        self._dark = self._try(b.dark_offset, _NAN) if info.get("zero_supported") else _NAN

    def _warn_adopted_outside_limits(self) -> None:
        """An adopted setting may lie outside this module's envelope (e.g. the
        console was left averaging 3 s, our limit is 1 s). It is NOT corrected
        -- that would be a write at start -- only reported, so the user knows
        why readings are slow or why the next setter will clamp."""
        if self.quantity == "none":
            return
        checks = [("wavelength", self._wl_actual, self.wavelength_limits(), "nm")]
        if self._is_energy():
            checks.append(("energy range", self._range_actual, self.range_limits(), "J"))
        else:
            checks.append(("averaging time", self._avg_actual, self.avg_time_limits(), "s"))
            if not self.cfg.sensor.auto_range:
                checks.append(("power range", self._range_actual, self.range_limits(), "W"))
        for name, v, (lo, hi), unit in checks:
            if math.isfinite(v) and math.isfinite(lo) and math.isfinite(hi) and not (
                    lo * (1 - 1e-9) <= v <= hi * (1 + 1e-9)):
                self._emit("warn", f"console {name} {v:g} {unit} is outside this "
                                   f"module's limits {lo:g}..{hi:g} {unit}; kept as "
                                   f"found (not changed at start)")

    def _announce_head(self) -> None:
        h = self._head
        if self.quantity == "none":
            self._emit("warn", "no usable sensor head connected"
                       + (f" ({h['name']})" if h.get("name") else ""))
            return
        self._emit("info", f"head {h.get('name') or h['kind']} ({h['kind']}, measures "
                           f"{self.quantity}), wavelength {self._wl_actual:g} nm")
        if h["kind"] == HEAD_THERMAL and self.cfg.acquisition.settle_s < 3.0:
            self._emit("info", "thermal head: it follows a change of light in ~1 s; "
                               "for scans set the acquire settle time to ~5 s")

    def _sanitise_config(self) -> None:
        s, lim, a = self.cfg.sensor, self.cfg.limits, self.cfg.acquisition
        s.wavelength_nm = _clamp(float(s.wavelength_nm), *self.wavelength_limits())[0]
        if self._is_energy():
            s.range_J = _clamp(float(s.range_J), *self.range_limits())[0]
        else:
            s.range_W = _clamp(float(s.range_W), *self.range_limits())[0]
        s.avg_time_s = _clamp(float(s.avg_time_s), *self.avg_time_limits())[0]
        a.readings = int(_clamp(int(a.readings), lim.readings_min, lim.readings_max)[0])
        a.settle_s = _clamp(float(a.settle_s), 0.0, lim.settle_max_s)[0]

    def _push_sensor(self) -> None:
        """Send the sensor settings to the console (with _hw held)."""
        s, b = self.cfg.sensor, self.backend
        if self._head.get("wavelength_settable", True):
            b.set_wavelength(s.wavelength_nm)
        if self._is_energy():
            b.set_energy_range(s.range_J)
        else:
            b.set_avg_time(s.avg_time_s)
            b.set_auto_range(s.auto_range)
            if not s.auto_range:
                b.set_range(s.range_W)
        self._read_back()

    def _read_back(self) -> None:
        """What the console actually applied (with _hw held)."""
        b = self.backend
        self._wl_actual = float(self._try(b.get_wavelength, _NAN))
        if self._is_energy():
            self._range_actual = float(self._try(b.get_energy_range, _NAN))
            self._avg_actual = _NAN
        else:
            self._range_actual = float(self._try(b.get_range, _NAN))
            self._avg_actual = float(self._try(b.get_avg_time, _NAN))

    @staticmethod
    def _try(fn, default):
        """For optional queries: a console that refuses one should not stop start-up."""
        try:
            return fn()
        except Exception:
            return default

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)


def _head_key(info: dict) -> tuple:
    return (info.get("kind"), info.get("name"), info.get("serial"))


def _fmt(value: float, unit: str) -> str:
    """0.00123 -> '1.23 mW', human-scaled for the event log (ASCII: 'uW')."""
    v = float(value)
    if not math.isfinite(v):
        return "--"
    for scale, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n")):
        if abs(v) >= scale:
            return f"{v / scale:.4g} {prefix}{unit}"
    return f"{v / 1e-12:.4g} p{unit}"
