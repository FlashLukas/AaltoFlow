"""The SourceMeter: the brain between the wire and the backend.

An SMU is two instruments in one box, and the brain treats them that way:

  SOURCE    set-and-forget, like an RF generator: a function (voltage or
            current), a level, a compliance limit, a range, the output relay.
            Every request is CLAMPED to the safety envelope (and a clamp is
            announced as a warn event), then pushed to the backend.

  MEASURE   a detector, like the pm16 power meter: a polling thread takes
            readings while the output is on, and `acquire` averages N readings
            that were all taken AFTER the trigger and AFTER the source settled,
            so a scan never files a reading from the previous step.

Safety rules, all enforced here (not in the backends):
  * the output is OFF at start, on shutdown, and before a source-function change;
  * the COMPLIANCE limit is always written before the level and before OUTP ON,
    so the instrument never sources for an instant with a stale limit;
  * a level/limit combination outside the 2450's output boxes (21 V x 1.05 A,
    210 V x 105 mA) is clamped: the limit bounds the level and vice versa.

Threads and locks (the suite's two rules, gotcha #1 and #28):
  * ONE polling thread owns the readings. `status()` only copies what that
    thread stored and never touches the hardware.
  * EVERY backend call runs under `_hw` (an RLock): the command thread and the
    polling thread would otherwise talk to the instrument at once.
  * `_lock` guards the snapshot state. "Not settled" is written BEFORE a new
    setpoint becomes visible, and an acquisition's result and its "finished"
    flag change in the same critical section -- so no status frame can pair a
    new setpoint with a stale "settled", or a new id with an old sample.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .backends.base import (BOX_I, BOX_V, FUNCS, I_MAX, NPLC_MAX, NPLC_MIN,
                            OVERRANGE, V_MAX, SourceMeterBackend, other,
                            range_table, snap_range)
from .config import Config

_NAN = float("nan")


@dataclass
class Status:
    """One snapshot of the SMU, for status() and the wire."""

    connected: bool
    idn: str = ""
    hw_error: str = ""
    output: bool = False
    # True once the source has held its newest level for source.settle_s with
    # the output on. With the output OFF it is True too: there is nothing to
    # wait for (and `acquire` refuses, which a scan notices).
    settled: bool = True
    source_function: str = "voltage"
    measure_function: str = "current"
    # the setpoints (as accepted after clamping)
    source_voltage_set_V: float = 0.0
    source_current_set_A: float = 0.0
    # The same current setpoint in uA. scan-core's adopt_then_flag compares with
    # an absolute tolerance of 1e-6 in WIRE units; in amperes that would call
    # 0.5 uA "the same" as 0 and let a scan move on before the source has
    # adopted the new point. In uA the tolerance is 1 pA. See net/describe.py.
    source_current_set_uA: float = 0.0
    current_limit_A: float = 0.0
    voltage_limit_V: float = 0.0
    # the envelope of the ACTIVE function, as it stands now (it moves with the
    # compliance limit and the source range -- see level_limits())
    level_max: float = _NAN
    limit_min: float = _NAN
    limit_max: float = _NAN
    source_auto_range: bool = True
    source_range: float = _NAN
    measure_auto_range: bool = True
    measure_range: float = _NAN
    nplc: float = 1.0
    four_wire: bool = False
    # the live reading (NaN while the output is off)
    voltage_V: float = _NAN
    current_A: float = _NAN
    resistance_ohm: float = _NAN
    tripped: bool = False              # in compliance
    flag: str = ""                     # "", "compliance", "overflow"
    read_ms: float = _NAN
    readings: int = 0
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


def _mean_std(vals: list[float]) -> tuple[float, float]:
    """Mean and sample standard deviation of the FINITE values (an overflowed
    reading is NaN and must not poison the other readings of an acquisition)."""
    v = [x for x in vals if math.isfinite(x)]
    if not v:
        return _NAN, _NAN
    m = sum(v) / len(v)
    s = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1)) if len(v) > 1 else 0.0
    return m, s


def fmt_si(value: float, unit: str) -> str:
    """0.00123, 'A' -> '1.23 mA' (ASCII only: 'u' for micro, gotcha #14)."""
    v = float(value)
    if not math.isfinite(v):
        return "--"
    if v == 0:
        return f"0 {unit}"
    for scale, prefix in ((1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"),
                          (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= scale:
            return f"{v / scale:.4g} {prefix}{unit}"
    return f"{v / 1e-15:.4g} f{unit}"


class SourceMeter:
    def __init__(self, backend: SourceMeterBackend, cfg: Config | None = None,
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

        # output + settling (under _lock)
        self._output = False
        self._settle_at = 0.0               # clock() time the newest level counts as settled
        # the source function the INSTRUMENT is in (set by _push_all); cfg can
        # run ahead of it when set_config edits cfg in place
        self._pushed_fn = self.cfg.source.function

        # the ranges the instrument reports (written under _hw / _lock)
        self._src_range = _NAN
        self._meas_range = _NAN

        # written only by the polling thread (under _lock)
        self._v = _NAN
        self._i = _NAN
        self._r = _NAN
        self._tripped = False
        self._flag = ""
        self._read_ms = _NAN
        self._n_read = 0

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
        """Open the instrument (output OFF), push every setting, start polling.

        `poll=False` skips the thread, so a test can drive `poll_once()` by hand.
        """
        with self._hw:
            self.backend.open()                 # leaves the output OFF
            self._idn = self.backend.idn()
            self._connected = True
            self._sanitise_config()
            self._push_all()
        with self._lock:
            self._output = False
        src = self.cfg.source
        self._emit("info", f"connected: {self._idn or 'Keithley 2450'}")
        self._emit("info", f"output OFF, sourcing {src.function}, "
                           f"limit {self._limit_text()}")
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="k2450-poll", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Output OFF, stop polling, disconnect. Safe to call more than once and
        on a crash: the output-off attempt comes first and its failure does not
        stop the close."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
        self._thread = None
        was = self._connected
        try:
            with self._hw:
                if was:
                    try:
                        self.backend.set_output(False)
                    except Exception as exc:     # still close the connection
                        self._emit("error", f"output off on shutdown failed: {exc}")
                self.backend.close()
        finally:
            self._connected = False
            with self._lock:
                self._output = False
                self._abort_acquisition("shutdown")
                self._clear_live()
            if was:
                self._emit("info", "output OFF, disconnected")

    # ---- the envelope ----------------------------------------------------------

    def level_limits(self, fn: str | None = None) -> tuple[float, float]:
        """(lo, hi) for the source LEVEL of function fn, symmetric.

        Three things narrow it: your envelope (limits.voltage_max_V /
        current_max_A); the 2450's output boxes, via the compliance limit of
        that function (a current limit above 105 mA confines the voltage to
        21 V, and the other way round); and a FIXED source range (105 % of it).
        """
        fn = fn or self.cfg.source.function
        src, lim = self.cfg.source, self.cfg.limits
        if fn == "voltage":
            hi = lim.voltage_max_V
            if src.current_limit_A > lim.box_current_A:
                hi = min(hi, lim.box_voltage_V)
            if not src.auto_range:
                hi = min(hi, src.range_V * OVERRANGE)
        else:
            hi = lim.current_max_A
            if src.voltage_limit_V > lim.box_voltage_V:
                hi = min(hi, lim.box_current_A)
            if not src.auto_range:
                hi = min(hi, src.range_A * OVERRANGE)
        return -hi, hi

    def limit_limits(self, fn: str | None = None) -> tuple[float, float]:
        """(lo, hi) for the COMPLIANCE limit of source function fn (always > 0).

        The mirror image of level_limits: sourcing more than 21 V confines the
        current limit to 105 mA, sourcing more than 105 mA confines the voltage
        limit to 21 V."""
        fn = fn or self.cfg.source.function
        src, lim = self.cfg.source, self.cfg.limits
        if fn == "voltage":
            lo, hi = lim.current_limit_min_A, lim.current_max_A
            if abs(src.voltage_V) > lim.box_voltage_V:
                hi = min(hi, lim.box_current_A)
        else:
            lo, hi = lim.voltage_limit_min_V, lim.voltage_max_V
            if abs(src.current_A) > lim.box_current_A:
                hi = min(hi, lim.box_voltage_V)
        return lo, max(lo, hi)

    # ---- source setters --------------------------------------------------------

    def set_source_function(self, fn: str) -> None:
        """Switch between sourcing voltage and sourcing current. The output is
        switched OFF first: a live change of function would, for an instant,
        apply the other function's level into the sample."""
        fn = str(fn).lower()
        if fn in ("v", "volt", "volts"):
            fn = "voltage"
        if fn in ("i", "curr", "amps"):
            fn = "current"
        if fn not in FUNCS:
            raise ValueError(f"source function must be 'voltage' or 'current', got {fn!r}")
        if fn == self.cfg.source.function:
            self._emit("info", f"already sourcing {fn}")
            return
        if self._output:
            self.set_output(False)
            self._emit("warn", "output switched OFF before changing the source function")
        self.cfg.source.function = fn
        if self._connected:
            with self._hw:
                self._sanitise_config()
                self._push_all()
        self._emit("info", f"sourcing {fn}, measuring {other(fn)}, "
                           f"limit {self._limit_text()} (output OFF)")

    def set_voltage(self, volts: float) -> None:
        self._set_level("voltage", volts)

    def set_current(self, amps: float) -> None:
        self._set_level("current", amps)

    def _set_level(self, fn: str, value) -> None:
        unit = "V" if fn == "voltage" else "A"
        lo, hi = self.level_limits(fn)
        value, clamped = _clamp(_finite(value, f"{fn} level"), lo, hi)
        src = self.cfg.source
        active = src.function == fn
        # "Not settled" FIRST, then the new setpoint, in one critical section:
        # a status frame must never show the new setpoint as already settled.
        with self._lock:
            if active:
                self._settle_at = self._clock() + src.settle_s
            if fn == "voltage":
                src.voltage_V = value
            else:
                src.current_A = value
        if active and self._connected:
            with self._hw:
                self.backend.set_level(fn, value)
            with self._lock:
                # the settling time counts from the moment the instrument has it
                self._settle_at = self._clock() + src.settle_s
        where = "" if active else f" (stored; applies when sourcing {fn})"
        if clamped:
            self._emit("warn", f"{fn} level clamped to {fmt_si(value, unit)} "
                               f"(allowed {fmt_si(lo, unit)} .. {fmt_si(hi, unit)}){where}")
        else:
            self._emit("info", f"{fn} level = {fmt_si(value, unit)}{where}")

    def set_current_limit(self, amps: float) -> None:
        """Compliance while sourcing VOLTAGE (SCPI ILIM)."""
        self._set_limit("voltage", amps)

    def set_voltage_limit(self, volts: float) -> None:
        """Compliance while sourcing CURRENT (SCPI VLIM)."""
        self._set_limit("current", volts)

    def _set_limit(self, fn: str, value) -> None:
        unit = "A" if fn == "voltage" else "V"
        lo, hi = self.limit_limits(fn)
        value, clamped = _clamp(abs(_finite(value, "limit")), lo, hi)
        src = self.cfg.source
        if fn == "voltage":
            src.current_limit_A = value
        else:
            src.voltage_limit_V = value
        if src.function == fn and self._connected:
            # in compliance, the limit IS the operating point: restart settling
            self.mark_unsettled()
            with self._hw:
                self.backend.set_limit(fn, value)
            self.mark_unsettled()
        name = "current limit" if fn == "voltage" else "voltage limit"
        if clamped:
            self._emit("warn", f"{name} clamped to {fmt_si(value, unit)} "
                               f"(allowed {fmt_si(lo, unit)} .. {fmt_si(hi, unit)})")
        else:
            self._emit("info", f"{name} = {fmt_si(value, unit)}")

    def set_source_auto_range(self, on: bool) -> None:
        src = self.cfg.source
        fn = src.function
        if not on and self._connected:
            # Hand over from auto to fixed WITHOUT a jump: keep the range auto chose.
            with self._hw:
                r = self.backend.get_source_range(fn)
            if math.isfinite(r):
                self._store_source_range(fn, r)
        src.auto_range = bool(on)
        self._push_source_range()
        self._emit("info", "source autorange ON" if on else
                   f"source range fixed at {fmt_si(self._fixed_source_range(), self._unit(fn))}")

    def set_source_range(self, value: float) -> None:
        """Fixed source range (switches autorange off; snaps UP to a real range).
        A level above 105 % of the new range is clamped down first."""
        fn = self.cfg.source.function
        r = snap_range(fn, _finite(value, "range"))
        self._store_source_range(fn, r)
        self.cfg.source.auto_range = False
        self._push_source_range()
        self._emit("info", f"source range {fmt_si(r, self._unit(fn))} "
                           f"(asked {fmt_si(float(value), self._unit(fn))})")

    def _push_source_range(self) -> None:
        """Re-clamp the level to the (new) range, then send level + range."""
        src = self.cfg.source
        fn = src.function
        level = src.voltage_V if fn == "voltage" else src.current_A
        lo, hi = self.level_limits(fn)
        new, clamped = _clamp(level, lo, hi)
        if clamped:
            self._set_level(fn, new)          # announces the clamp itself
        if self._connected:
            self.mark_unsettled()             # a range change glitches the output
            with self._hw:
                self.backend.set_source_range(fn, src.auto_range, self._fixed_source_range())
                r = self.backend.get_source_range(fn)
            with self._lock:
                self._src_range = r
                self._settle_at = self._clock() + src.settle_s

    # ---- measure setters -------------------------------------------------------

    def set_measure_auto_range(self, on: bool) -> None:
        m = self.cfg.measure
        mfn = other(self.cfg.source.function)
        if not on and self._connected:
            with self._hw:
                r = self.backend.get_measure_range(mfn)
            if math.isfinite(r):
                self._store_measure_range(mfn, r)
        m.auto_range = bool(on)
        self._push_measure()
        self._emit("info", "measure autorange ON" if on else
                   f"measure range fixed at {fmt_si(self._fixed_measure_range(), self._unit(mfn))}")

    def set_measure_range(self, value: float) -> None:
        mfn = other(self.cfg.source.function)
        r = snap_range(mfn, _finite(value, "range"))
        self._store_measure_range(mfn, r)
        self.cfg.measure.auto_range = False
        self._push_measure()
        self._emit("info", f"measure range {fmt_si(r, self._unit(mfn))} "
                           f"(asked {fmt_si(float(value), self._unit(mfn))}); "
                           f"readings above 105 % of it overflow")

    def set_nplc(self, nplc: float) -> None:
        lim = self.cfg.limits
        value, clamped = _clamp(_finite(nplc, "NPLC"), lim.nplc_min, lim.nplc_max)
        self.cfg.measure.nplc = value
        self._push_measure()
        ms = value / self.cfg.hardware.line_freq_Hz * 1e3
        self._emit("warn" if clamped else "info",
                   f"NPLC = {value:g} ({ms:.4g} ms per reading)"
                   + (f", clamped to {lim.nplc_min:g}..{lim.nplc_max:g}" if clamped else ""))

    def set_four_wire(self, on: bool) -> None:
        # Sourcing voltage, the sense point is where the SMU regulates: moving
        # it from the terminals to the sample changes the operating point.
        self.mark_unsettled()
        self.cfg.measure.four_wire = bool(on)
        self._push_measure()
        self.mark_unsettled()
        self._emit("info", "4-wire (remote sense)" if on else "2-wire (local sense)")

    def _push_measure(self) -> None:
        if not self._connected:
            return
        m = self.cfg.measure
        mfn = other(self.cfg.source.function)
        with self._hw:
            self.backend.set_measure_range(mfn, m.auto_range, self._fixed_measure_range())
            self.backend.set_nplc(mfn, m.nplc)
            self.backend.set_four_wire(m.four_wire)
            r = self.backend.get_measure_range(mfn)
        with self._lock:
            self._meas_range = r

    # ---- output ------------------------------------------------------------------

    def set_output(self, on: bool) -> None:
        """Output relay. ON re-sends the compliance limit and the level first,
        so what the sample sees is exactly what status reports."""
        on = bool(on)
        if on and not self._connected:
            raise ValueError("not connected")
        src = self.cfg.source
        fn = src.function
        if on:
            level = src.voltage_V if fn == "voltage" else src.current_A
            limit = src.current_limit_A if fn == "voltage" else src.voltage_limit_V
            with self._lock:
                self._settle_at = self._clock() + src.settle_s
            with self._hw:
                self.backend.set_limit(fn, limit)       # compliance FIRST
                self.backend.set_level(fn, level)
                self.backend.set_output(True)
            with self._lock:
                self._settle_at = self._clock() + src.settle_s
                self._output = True
            u, lu = self._unit(fn), self._unit(other(fn))
            self._emit("warn", f"OUTPUT ON: {fn} {fmt_si(level, u)}, "
                               f"limit {fmt_si(limit, lu)}")
        else:
            if self._connected:
                with self._hw:
                    self.backend.set_output(False)
            with self._lock:
                self._output = False
                self._abort_acquisition("output switched off")
                self._clear_live()
            self._emit("info", "output OFF")

    def mark_unsettled(self) -> None:
        """Restart the settle clock. Called BEFORE anything that can move the
        operating point of a live output (a new compliance limit when the
        sample is in compliance, a source-range change, 2-wire <-> 4-wire, a
        set_config), so that no status frame -- and no acquisition reading --
        treats the transient as settled."""
        with self._lock:
            self._settle_at = self._clock() + self.cfg.source.settle_s

    def output_off(self) -> None:
        """The one-click safe state (also a scan-routine action)."""
        self.set_output(False)

    # ---- acquisition ----------------------------------------------------------------

    def set_acquisition(self, readings: int) -> None:
        lim = self.cfg.limits
        n = int(round(_finite(readings, "readings")))
        value, clamped = _clamp(n, lim.readings_min, lim.readings_max)
        self.cfg.acquisition.readings = int(value)
        self._emit("warn" if clamped else "info",
                   f"acquire averages {int(value)} readings" + (" (clamped)" if clamped else ""))

    def acquire(self) -> int:
        """Start an acquisition; returns its id immediately. Readings count only
        if they STARTED after this call and after the source settled."""
        if not self._connected:
            raise ValueError("not connected")
        n = max(1, int(self.cfg.acquisition.readings))
        with self._lock:
            if not self._output:
                raise ValueError("output is OFF: switch it on before acquiring")
            # id and "acquiring" change TOGETHER, under the lock (gotcha #17)
            self._acq_id += 1
            self._acq = {"id": self._acq_id, "t0": self._clock(), "want": n,
                         "v": [], "i": [], "r": [], "tripped": False, "overflow": False}
            return self._acq_id

    def get_sample(self) -> dict:
        with self._lock:
            return dict(self._sample)

    # ---- status ------------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        src, m = self.cfg.source, self.cfg.measure
        fn = src.function
        _, lmax = self.level_limits(fn)
        llo, lhi = self.limit_limits(fn)
        with self._lock:
            a = self._acq
            settled = (not self._output) or self._clock() >= self._settle_at
            return Status(
                connected=self._connected, idn=self._idn, hw_error=self._hw_error,
                output=self._output, settled=settled,
                source_function=fn, measure_function=other(fn),
                source_voltage_set_V=src.voltage_V, source_current_set_A=src.current_A,
                source_current_set_uA=src.current_A * 1e6,
                current_limit_A=src.current_limit_A, voltage_limit_V=src.voltage_limit_V,
                level_max=lmax, limit_min=llo, limit_max=lhi,
                source_auto_range=src.auto_range, source_range=self._src_range,
                measure_auto_range=m.auto_range, measure_range=self._meas_range,
                nplc=m.nplc, four_wire=m.four_wire,
                voltage_V=self._v, current_A=self._i, resistance_ohm=self._r,
                tripped=self._tripped, flag=self._flag, read_ms=self._read_ms,
                readings=self._n_read,
                acq_readings=int(self.cfg.acquisition.readings),
                acq_id=self._acq_id, acquiring=a is not None,
                acq_progress=0.0 if a is None else len(a["v"]) / a["want"],
                sample=dict(self._sample),
            )

    # ---- config (Settings dialog / wire) ---------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp cfg (possibly edited in place over the wire) and push it.
        The limit and level are re-sent. The output stays as it is -- EXCEPT
        when the new config changes the source function: that goes through the
        same rule as set_source_function (output OFF first), because a
        set_config or a loaded .ini must not be a back door around it."""
        if self._output and self.cfg.source.function != self._pushed_fn:
            self.set_output(False)
            self._emit("warn", "output switched OFF: the new settings change "
                               "the source function")
        self._sanitise_config()
        if self._connected:
            with self._hw:
                self._push_all()
            with self._lock:
                self._settle_at = self._clock() + self.cfg.source.settle_s
        self._emit("info", "settings applied")

    # ---- polling ------------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            period = 1.0 / max(1.0, float(self.cfg.hardware.poll_hz))
            t = self._clock()
            self.poll_once()
            # A reading blocks for NPLC / line frequency by itself; only sleep
            # what is left of the period, so the loop runs as fast as allowed.
            self._stop.wait(max(0.005, period - (self._clock() - t)))

    def poll_once(self) -> None:
        """One reading while the output is on, then advance any acquisition.
        Public so tests and single-threaded scripts can drive it."""
        with self._lock:
            if not self._output:
                return
        try:
            with self._hw:
                # read the function under _hw: a function switch also needs
                # _hw, so it cannot slip in between this and the reading
                fn = self.cfg.source.function
                t_start = self._clock()
                rd = self.backend.measure()
                t_end = self._clock()
        except Exception as exc:          # never let the polling thread die
            self._report_hw_error(exc)
            return

        if fn == "voltage":
            v, i = rd.source, rd.measured
        else:
            v, i = rd.measured, rd.source
        r = v / i if (math.isfinite(v) and math.isfinite(i) and i != 0.0) else _NAN
        flag = "overflow" if rd.overflow else ("compliance" if rd.tripped else "")

        recovered = False
        with self._lock:
            if not self._output:          # switched off while we were reading
                return
            recovered = bool(self._hw_error)
            self._hw_error = ""
            self._v, self._i, self._r = v, i, r
            self._tripped = bool(rd.tripped)
            self._flag = flag
            self._read_ms = (t_end - t_start) * 1e3
            self._n_read += 1
            if math.isfinite(rd.measure_range):
                self._meas_range = rd.measure_range
            if math.isfinite(rd.source_range):
                self._src_range = rd.source_range
            self._advance_acquisition(t_start, v, i, r, rd)
        if recovered:
            self._emit("info", "hardware reads recovered")

    def _advance_acquisition(self, t_start, v, i, r, rd) -> None:
        """Called with _lock held. Only readings that STARTED after the trigger
        and after the source settled count."""
        a = self._acq
        if a is None or t_start < a["t0"] or t_start < self._settle_at:
            return
        a["v"].append(v)
        a["i"].append(i)
        a["r"].append(r)
        a["tripped"] |= bool(rd.tripped)
        a["overflow"] |= bool(rd.overflow)
        if len(a["v"]) < a["want"]:
            return
        mv, sv = _mean_std(a["v"])
        mi, si = _mean_std(a["i"])
        _, sr = _mean_std(a["r"])
        # R from the MEANS (not the mean of ratios): a noisy near-zero current
        # reading would otherwise dominate the average.
        mr = mv / mi if (math.isfinite(mv) and math.isfinite(mi) and mi != 0.0) else _NAN
        flags = [f for f, on in (("compliance", a["tripped"]), ("overflow", a["overflow"])) if on]
        src, m = self.cfg.source, self.cfg.measure
        # result and "finished" in ONE critical section (gotcha #28)
        self._sample = {"acq_id": a["id"], "voltage_V": mv, "voltage_std_V": sv,
                        "current_A": mi, "current_std_A": si,
                        "resistance_ohm": mr, "resistance_std_ohm": sr,
                        "n": len(a["v"]), "tripped": a["tripped"],
                        "flag": ",".join(flags), "aborted": False,
                        "source_function": src.function, "nplc": m.nplc,
                        "four_wire": m.four_wire, "time": time.time()}
        self._acq = None

    def _abort_acquisition(self, why: str) -> None:
        """Called with _lock held. LATCH an aborted sample (NaN values) rather
        than just dropping the acquisition: a scan waiting for this id must not
        go on to read the PREVIOUS sample (gotcha #28)."""
        a = self._acq
        if a is None:
            return
        self._sample = {"acq_id": a["id"], "voltage_V": _NAN, "voltage_std_V": _NAN,
                        "current_A": _NAN, "current_std_A": _NAN,
                        "resistance_ohm": _NAN, "resistance_std_ohm": _NAN,
                        "n": len(a["v"]), "tripped": a["tripped"], "flag": "aborted",
                        "aborted": True, "why": why, "time": time.time()}
        self._acq = None

    def _clear_live(self) -> None:
        """Called with _lock held: no output, no reading."""
        self._v = self._i = self._r = _NAN
        self._tripped = False
        self._flag = ""

    def _report_hw_error(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._hw_error = msg
        now = self._clock()
        if now - self._last_err_emit >= 5.0:      # rate-limit: one event per 5 s
            self._last_err_emit = now
            self._emit("error", f"hardware read failed: {msg}")

    # ---- internals --------------------------------------------------------------------------

    @staticmethod
    def _unit(fn: str) -> str:
        return "V" if fn == "voltage" else "A"

    def _limit_text(self) -> str:
        src = self.cfg.source
        if src.function == "voltage":
            return fmt_si(src.current_limit_A, "A")
        return fmt_si(src.voltage_limit_V, "V")

    def _fixed_source_range(self) -> float:
        src = self.cfg.source
        return src.range_V if src.function == "voltage" else src.range_A

    def _fixed_measure_range(self) -> float:
        m = self.cfg.measure
        return m.range_V if other(self.cfg.source.function) == "voltage" else m.range_A

    def _store_source_range(self, fn: str, r: float) -> None:
        if fn == "voltage":
            self.cfg.source.range_V = r
        else:
            self.cfg.source.range_A = r

    def _store_measure_range(self, mfn: str, r: float) -> None:
        if mfn == "voltage":
            self.cfg.measure.range_V = r
        else:
            self.cfg.measure.range_A = r

    def _sanitise_config(self) -> None:
        """Make cfg self-consistent: valid function and ranges, limits first
        (bounded by the present levels), then levels (bounded by the limits)."""
        src, m, lim = self.cfg.source, self.cfg.measure, self.cfg.limits
        if src.function not in FUNCS:
            src.function = "voltage"
        # The user envelope may be narrower than the instrument, never wider
        # (a hand-edited .ini or a set_config could ask for 500 V; the 2450
        # would refuse it and status would report a level that is not applied).
        lim.voltage_max_V = min(abs(float(lim.voltage_max_V)), V_MAX)
        lim.current_max_A = min(abs(float(lim.current_max_A)), I_MAX)
        lim.box_voltage_V = min(abs(float(lim.box_voltage_V)), BOX_V)
        lim.box_current_A = min(abs(float(lim.box_current_A)), BOX_I)
        lim.nplc_min = max(float(lim.nplc_min), NPLC_MIN)
        lim.nplc_max = max(lim.nplc_min, min(float(lim.nplc_max), NPLC_MAX))
        src.range_V = snap_range("voltage", src.range_V)
        src.range_A = snap_range("current", src.range_A)
        m.range_V = snap_range("voltage", m.range_V)
        m.range_A = snap_range("current", m.range_A)
        src.current_limit_A = _clamp(abs(float(src.current_limit_A)),
                                     *self.limit_limits("voltage"))[0]
        src.voltage_limit_V = _clamp(abs(float(src.voltage_limit_V)),
                                     *self.limit_limits("current"))[0]
        src.voltage_V = _clamp(float(src.voltage_V), *self.level_limits("voltage"))[0]
        src.current_A = _clamp(float(src.current_A), *self.level_limits("current"))[0]
        m.nplc = _clamp(float(m.nplc), lim.nplc_min, lim.nplc_max)[0]
        a = self.cfg.acquisition
        a.readings = int(_clamp(int(a.readings), lim.readings_min, lim.readings_max)[0])

    def _push_all(self) -> None:
        """Send every setting of the active function (with _hw held). The order
        is the safe one: function, COMPLIANCE, range, level, then measurement."""
        src, m = self.cfg.source, self.cfg.measure
        fn, mfn = src.function, other(src.function)
        b = self.backend
        b.set_source_function(fn)
        self._pushed_fn = fn
        b.set_limit(fn, src.current_limit_A if fn == "voltage" else src.voltage_limit_V)
        b.set_source_range(fn, src.auto_range, self._fixed_source_range())
        b.set_level(fn, src.voltage_V if fn == "voltage" else src.current_A)
        b.set_measure_range(mfn, m.auto_range, self._fixed_measure_range())
        b.set_nplc(mfn, m.nplc)
        b.set_four_wire(m.four_wire)
        src_r = b.get_source_range(fn)
        meas_r = b.get_measure_range(mfn)
        with self._lock:
            self._src_range, self._meas_range = src_r, meas_r

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)


__all__ = ["SourceMeter", "Status", "fmt_si", "range_table"]
