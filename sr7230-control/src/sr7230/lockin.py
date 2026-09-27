"""The LockIn: the brain between the wire and the 7230 backend.

Set-and-forget for its SETTINGS (reference, oscillator, sensitivity, time
constant, slope, input): clamp, snap to what the instrument can do, push,
report. Where a lock-in differs from a generator is that it is a DETECTOR WITH
MEMORY. Its output is a low-pass-filtered average, so after anything changes --
the field, the sample position, the time constant itself -- the output needs
several time constants to settle. A reading taken too early describes the
PREVIOUS state, looks perfectly clean, and is wrong.

So there are two ways to read it:

  live      the latest output, updated by the polling thread. Right for a
            front panel. WRONG for a scan point: nothing guarantees it has
            settled since the last change.

  acquire   the scan-safe read. `acquire()` returns an id at once (the suite's
            fire-and-forget contract); the polling thread then waits the
            settling time -- COMPUTED from the time constant and the slope,
            see filters.py -- optionally averages over a window, and LATCHES
            the result as `sample`. A caller waits until status shows ITS id
            with `acquiring` False; checking the id first is what stops a
            stale "not acquiring" from the previous point fooling it.

The 7230 has a few things the suite's other lock-in did not:

  * DISCRETE SETTINGS. Time constant and sensitivity come from fixed 1-2-5
    tables (tables.py). A requested time constant is snapped to the nearest
    one the instrument has; status shows both what was asked (`tc_set_s`, the
    settle echo) and what is applied (`tc_s`).
  * MODES THAT MOVE THE LIMITS. Fast mode trades the 5 ms minimum time
    constant for a 12 dB/oct maximum slope; current mode turns volts into
    amps; the harmonic divides the highest usable frequency. All of it shows
    up in `describe` with a new revision.
  * OVERLOAD. The instrument reports input and output overload with every
    reply. A sample remembers whether ANY reading in its window was overloaded
    or taken on an unlocked reference, so bad data carries its own warning.
  * AUTO OPERATIONS. Auto-phase / auto-sensitivity / auto-measure take real
    time on the instrument. They are queued, numbered like acquisitions
    (`auto_id`, `auto_busy`) and carried out by the polling thread.
  * AN OUTPUT. OSC OUT can drive a real load, so it starts at 0 V and goes
    back to 0 V on shutdown (Hardware.osc_zero_on_start / osc_off_on_shutdown).

Threads and locks (both rules learned the hard way elsewhere in the suite):

  * ONE polling thread owns the output reads. `status()` only copies what that
    thread stored -- it never touches the hardware -- so a slow network read
    can never stall the status publisher, and a dead link shows up as
    `hw_error` instead of a healthy-looking panel full of zeros.
  * EVERY backend call runs under `_hw` (an RLock): one socket, one command at
    a time.
  * Live control state is the config itself plus a few brain attributes. The
    polling thread builds a NEW snapshot each cycle and never shares an object
    a setter writes to (gotcha #1, the lost-update race).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from . import filters, tables
from .backends.base import LockInBackend
from .config import Config, REF_SOURCES, INPUT_MODES, SLOPES_DB
from .stream import StreamRecorder

#: The channels of the fly-scan stream, named like the scan detectors in the
#: manifest, so a detector id IS its stream channel.
STREAM_CHANNELS = ("x", "y", "r", "theta", "adc1", "adc2")

#: The instrument's own frequency limit: 120 kHz, 250 kHz with option 7230/99.
F_MAX_STANDARD_HZ = 120e3
F_MAX_OPTION_HZ = 250e3

#: What each auto operation is called on the wire, and the backend method.
AUTO_OPS = {"auto_phase": "auto_phase", "auto_sensitivity": "auto_sensitivity",
            "auto_measure": "auto_measure"}

# input mode -> (IMODE, VMODE), manual section 6.6.01
_INPUT_CODES = {"A": (0, 1), "-B": (0, 2), "A-B": (0, 3), "ground": (0, 0),
                "I high-BW": (1, 1), "I low-noise": (2, 1)}
_LINE_FILTER = {"off": 0, "1f": 1, "2f": 2, "both": 3}


@dataclass
class Status:
    """One snapshot of the lock-in, for status() and the wire."""

    connected: bool
    idn: str = ""
    hw_error: str = ""
    ref_source: str = "internal"
    ref_locked: object = None          # bool on an external reference, None on internal
    freq_set_Hz: float = 0.0           # oscillator setpoint
    ref_freq_Hz: float = math.nan      # frequency meter (0 while an external ref is unlocked)
    demod_freq_Hz: float = math.nan    # harmonic x reference: where the detector sits
    freq_max_Hz: float = math.nan      # highest oscillator setting allowed right now
    amplitude_V: float = 0.0
    phase_deg: float = 0.0
    harmonic: int = 1
    input: str = "A"
    unit: str = "V"
    ac_coupled: bool = True
    coupling: str = "AC"               # the same, as the label a panel shows
    sensitivity: str = ""              # label, e.g. "100 mV"
    sensitivity_index: int = 0
    full_scale: float = math.nan       # V or A
    fast_mode: bool = False
    tc_set_s: float = math.nan         # time constant we asked for
    tc_s: float = math.nan             # time constant the instrument applied
    slope: str = ""                    # label, e.g. "12 dB/oct"
    slope_db: int = 12
    settle_s: float = math.nan         # computed settling time
    live: dict = field(default_factory=dict)       # x, y, r, theta_deg, r_fs, adc
    overload: dict = field(default_factory=dict)   # input, output, x, y, byte
    acq_id: int = 0
    acquiring: bool = False
    acq_progress: float = 0.0
    sample: dict = field(default_factory=dict)     # the last LATCHED acquisition
    auto_id: int = 0
    auto_busy: bool = False
    auto_op: str = ""
    auto_error: str = ""


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def _finite(value, what: str) -> float:
    """float(value), refusing NaN and inf.

    Not pedantry: every comparison with NaN is False, so NaN sails straight
    through `_clamp` and would be sent to the instrument.
    """
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return v


def _empty_live() -> dict:
    nan = float("nan")
    return {"x": nan, "y": nan, "r": nan, "theta_deg": nan, "r_fs": nan,
            "adc": [nan, nan]}


def _empty_overload() -> dict:
    return {"input": False, "output": False, "x": False, "y": False, "byte": 0}


class LockIn:
    def __init__(self, backend: LockInBackend, cfg: Config | None = None,
                 clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards the snapshot, acquisition, auto ops

        self._connected = False
        self._idn = ""
        self._hw_error = ""
        self._last_err_emit = -1e9

        # what the instrument reports back after a set
        self._tc_actual = tables.TIME_CONSTANTS_S[
            tables.nearest_tc_index(self.cfg.filter.time_constant_s, tables.TIME_CONSTANTS_S)]

        # written only by the polling thread (under _lock)
        self._live = _empty_live()
        self._ref_freq = float("nan")
        self._locked: object = None
        self._overload = _empty_overload()

        # acquisition state (under _lock)
        self._acq_id = 0
        self._acq: dict | None = None
        self._sample: dict = {}

        # auto operations (under _lock): queued by the command thread, carried
        # out by the polling thread, which owns the instrument's time
        self._auto_id = 0
        self._auto_pending: tuple[int, str] | None = None
        self._auto_busy = False
        self._auto_op = ""
        self._auto_error = ""

        # The fly-scan record: every reading the poll thread takes, time
        # stamped, while a scan has it running (stream.py).
        self.stream = StreamRecorder(STREAM_CHANNELS, delay_fn=self.stream_delays)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- lifecycle -----------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Open the backend, push every setting, start polling.

        `poll=False` skips the thread, so a test can drive `poll_once()` by hand.
        """
        if self.cfg.hardware.osc_zero_on_start and self.cfg.reference.amplitude_V != 0.0:
            self._emit("info", f"OSC OUT starts at 0 V (was {self.cfg.reference.amplitude_V:g} V "
                               f"in the config; raise it deliberately)")
            self.cfg.reference.amplitude_V = 0.0
        self._sanitise_config()
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            self._connected = True
            self._push_all()
        self._emit("info", f"connected: {self._idn or '7230'}")
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="sr7230-poll", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop polling, make OSC OUT safe, disconnect. Safe to call twice."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        self._thread = None
        was = self._connected
        try:
            with self._hw:
                if was and self.cfg.hardware.osc_off_on_shutdown:
                    try:
                        self.backend.set_osc_amplitude(0.0)
                        self.cfg.reference.amplitude_V = 0.0
                    except Exception as exc:        # still disconnect
                        self._emit("error", f"could not zero OSC OUT: {exc}")
                self.backend.close()
        finally:
            self._connected = False
            with self._lock:
                self._acq = None
                self._auto_pending = None
                self._auto_busy = False
            if was:
                self._emit("info", "disconnected")

    # ---- derived limits (the manifest reads these too) --------------------------

    def instrument_f_max(self) -> float:
        """The highest frequency the instrument itself can do."""
        return F_MAX_OPTION_HZ if self.cfg.hardware.option_250kHz else F_MAX_STANDARD_HZ

    def freq_max_Hz(self) -> float:
        """Highest oscillator frequency allowed now. On an internal reference
        the detector sits at harmonic x oscillator, and THAT must stay within
        the instrument's range, so the harmonic divides the limit."""
        f = min(self.cfg.limits.freq_max_Hz, self.instrument_f_max())
        if self.cfg.reference.source == "internal":
            f /= max(1, int(self.cfg.reference.harmonic))
        return f

    def allowed_tcs(self) -> list[float]:
        lim = self.cfg.limits
        return tables.allowed_time_constants(self.cfg.filter.fast_mode, lim.tc_min_s, lim.tc_max_s)

    def sensitivity_table(self) -> dict[int, float]:
        return tables.sensitivity_table(self.cfg.signal.input)

    def order(self) -> int:
        """Filter order = slope / 6 dB per octave."""
        return max(1, int(self.cfg.filter.slope_db) // 6)

    def _refuse_during_auto(self, what: str) -> None:
        """Refuse a change while an auto operation runs on the instrument.

        Why: AS / ASM can keep the 7230 busy for many time constants, and the
        poll thread holds the instrument lock (`_hw`) for all of it. A setter
        arriving meanwhile would sit waiting for that lock inside the service's
        command thread -- the client gives up after 3 s, and the change then
        lands much later, unannounced, possibly undoing what the auto operation
        just chose. A clear refusal is honest; the caller retries once status
        shows `auto_busy` false. (The OSC OUT amplitude is exempt: turning a
        drive DOWN must never be refused.)"""
        with self._lock:
            busy, op = self._auto_busy, self._auto_op
        if busy:
            raise ValueError(f"{what} refused: {op.replace('_', '-')} is still running")

    # ---- reference + oscillator -------------------------------------------------

    def set_reference(self, source: str) -> None:
        self._refuse_during_auto("reference")
        src = _parse_source(source)
        self.cfg.reference.source = src
        if self._connected:
            with self._hw:
                self.backend.set_ref_source(REF_SOURCES.index(src))
                if src == "internal":
                    # the harmonic limit applies again: re-clamp the oscillator
                    self._reclamp_frequency()
        self._emit("info", f"reference = {src}")

    def set_frequency(self, hz: float) -> None:
        """The internal oscillator. On an internal reference that is also the
        detection frequency; on an external one OSC OUT just keeps running."""
        self._refuse_during_auto("frequency")
        lim = self.cfg.limits
        value, clamped = _clamp(_finite(hz, "frequency"), lim.freq_min_Hz, self.freq_max_Hz())
        self.cfg.reference.frequency_Hz = value
        if self._connected:
            with self._hw:
                self.backend.set_osc_frequency(value)
        if clamped:
            self._emit("warn", f"frequency clamped to {value:g} Hz "
                               f"(limit {lim.freq_min_Hz:g}..{self.freq_max_Hz():g})")
        else:
            self._emit("info", f"oscillator = {value:g} Hz")

    def set_amplitude(self, volts: float) -> None:
        """OSC OUT amplitude, V rms, clamped to Limits.amplitude_max_V."""
        value, clamped = _clamp(_finite(volts, "amplitude"), 0.0, self.cfg.limits.amplitude_max_V)
        self.cfg.reference.amplitude_V = value
        if self._connected:
            with self._hw:
                self.backend.set_osc_amplitude(value)
        if clamped:
            self._emit("warn", f"amplitude clamped to {value:g} V "
                               f"(limit 0..{self.cfg.limits.amplitude_max_V:g} V rms)")
        else:
            self._emit("info", f"OSC OUT = {value:g} V rms")

    def set_phase(self, deg: float) -> None:
        self._refuse_during_auto("phase")
        # wrap into -180..180: the same phase, and the range every readout uses.
        # +180 stays +180 (not -180), so a scan that asks for 180 sees it echoed.
        raw = _finite(deg, "phase")
        value = (raw + 180.0) % 360.0 - 180.0
        if value == -180.0 and raw > 0:
            value = 180.0
        self.cfg.reference.phase_deg = value
        if self._connected:
            with self._hw:
                self.backend.set_phase(value)
        self._emit("info", f"reference phase = {value:+.3f} deg")

    def set_harmonic(self, n: int) -> None:
        self._refuse_during_auto("harmonic")
        n = int(round(_finite(n, "harmonic")))
        hi = self._harmonic_max()
        value, clamped = _clamp(n, 1, hi)
        value = int(value)
        self.cfg.reference.harmonic = value
        if self._connected:
            with self._hw:
                self.backend.set_harmonic(value)
                self._reclamp_frequency()
        self._emit("warn" if clamped else "info",
                   f"harmonic = {value}" + (f" (clamped to 1..{hi}: harmonic x frequency "
                                            f"must stay below {self.instrument_f_max():g} Hz)"
                                            if clamped else ""))

    # ---- signal channel ----------------------------------------------------------

    def set_input(self, mode: str) -> None:
        self._refuse_during_auto("input")
        m = _parse_input(mode)
        self.cfg.signal.input = m
        if self._connected:
            with self._hw:
                self.backend.set_input(*_INPUT_CODES[m])
        self._emit("info", f"input = {m} ({tables.unit_for(m)})")
        # the same SEN index means something else now; low-noise current mode
        # starts at index 7, so an index below that must move
        self._reclamp_sensitivity()

    def set_coupling(self, coupling) -> None:
        self._refuse_during_auto("coupling")
        c = str(coupling).strip().upper()
        if c in ("TRUE", "1"):
            c = "AC"
        elif c in ("FALSE", "0"):
            c = "DC"
        if c not in ("AC", "DC"):
            raise ValueError(f"coupling must be AC or DC, got {coupling!r}")
        self.cfg.signal.ac_coupled = c == "AC"
        if self._connected:
            with self._hw:
                self.backend.set_coupling(dc=(c == "DC"))
        self._emit("info", f"coupling = {c}")

    def set_full_scale(self, value: float) -> None:
        """Full scale by VALUE in V (or A in current mode): the smallest range
        that still holds it, so a signal of that size never overloads."""
        v = _finite(value, "full scale")
        table = self.sensitivity_table()
        fitting = [i for i, fs in sorted(table.items()) if fs >= v * (1 - 1e-9)]
        self.set_sensitivity(fitting[0] if fitting else max(table))

    def set_sensitivity(self, value) -> None:
        """Full scale by label ("100 mV") or by table index (24, the SEN n)."""
        self._refuse_during_auto("sensitivity")
        table = self.sensitivity_table()
        idx = _parse_sensitivity(value, table, self.cfg.signal.input)
        lo, hi = min(table), max(table)
        new, clamped = _clamp(idx, lo, hi)
        new = int(new)
        self.cfg.signal.sensitivity_index = new
        if self._connected:
            with self._hw:
                self.backend.set_sensitivity_index(new)
        label = tables.sensitivity_label(new, self.cfg.signal.input)
        self._emit("warn" if clamped else "info",
                   f"sensitivity = {label}" + (" (clamped to the table)" if clamped else ""))

    # ---- output filter -------------------------------------------------------------

    def set_time_constant(self, tc_s: float) -> None:
        """Any value in seconds; the instrument gets the nearest table entry
        (on a log scale) that the current mode allows."""
        self._refuse_during_auto("time constant")
        allowed = self.allowed_tcs()
        lo, hi = min(allowed), max(allowed)
        value, clamped = _clamp(_finite(tc_s, "time constant"), lo, hi)
        self.cfg.filter.time_constant_s = value
        self._tc_actual = self._apply_tc() if self._connected else \
            tables.TIME_CONSTANTS_S[tables.nearest_tc_index(value, allowed)]
        if clamped:
            why = "" if self.cfg.filter.fast_mode or lo > tables.TC_MIN_NORMAL_S else \
                " -- shorter needs fast mode"
            self._emit("warn", f"time constant clamped to {tables.tc_label(value)} "
                               f"(allowed {tables.tc_label(lo)}..{tables.tc_label(hi)}{why})")
        self._emit("info", f"time constant = {tables.tc_label(self._tc_actual)}"
                           + ("" if abs(self._tc_actual - value) <= 1e-9 * value
                              else f" (nearest to the {value:.4g} s asked for)"))

    def set_slope(self, slope) -> None:
        self._refuse_during_auto("slope")
        db = _parse_slope(slope)
        allowed = tables.allowed_slopes(self.cfg.filter.fast_mode)
        clamped = db not in allowed
        if clamped:
            db = max(s for s in allowed if s <= db) if db > min(allowed) else min(allowed)
        self.cfg.filter.slope_db = db
        if self._connected:
            with self._hw:
                self.backend.set_slope_index(SLOPES_DB.index(db))
        self._emit("warn" if clamped else "info",
                   f"slope = {tables.slope_label(db)}"
                   + (" (fast mode allows 6 or 12 dB/oct only)" if clamped else ""))

    def set_fast_mode(self, enabled) -> None:
        self._refuse_during_auto("fast mode")
        on = _parse_bool(enabled)
        self.cfg.filter.fast_mode = on
        if self._connected:
            with self._hw:
                self.backend.set_fast_mode(on)
        self._emit("info", f"fast mode {'on' if on else 'off'}")
        # The two things fast mode trades must follow: a slope it no longer
        # allows, a time constant below its range.
        if self.cfg.filter.slope_db not in tables.allowed_slopes(on):
            self.set_slope(self.cfg.filter.slope_db)
        allowed = self.allowed_tcs()
        if not min(allowed) <= self.cfg.filter.time_constant_s <= max(allowed) or \
                (self._connected and self._tc_actual not in allowed):
            self.set_time_constant(self.cfg.filter.time_constant_s)

    # ---- auto operations ---------------------------------------------------------------

    def auto(self, op: str) -> int:
        """Queue auto_phase / auto_sensitivity / auto_measure. Returns its id at
        once; status shows `auto_id` = that id with `auto_busy` False when done."""
        if op not in AUTO_OPS:
            raise ValueError(f"unknown auto operation {op!r} (use {', '.join(AUTO_OPS)})")
        if not self._connected:
            raise ValueError("not connected")
        with self._lock:
            if self._auto_busy:
                raise ValueError(f"{self._auto_op} still running")
            # id and busy change TOGETHER, so no snapshot shows the new id idle
            self._auto_id += 1
            self._auto_busy = True
            self._auto_op = op
            self._auto_error = ""
            self._auto_pending = (self._auto_id, op)
            n = self._auto_id
        self._emit("info", f"{op.replace('_', '-')} #{n} queued")
        if self._thread is None:
            # no polling thread (a test driving poll_once, or a script): run now
            self._run_pending_auto()
        return n

    # ---- the scan-safe read ---------------------------------------------------

    def acquire(self) -> int:
        """Start a settle-then-latch acquisition. Returns its id immediately.

        The clock starts NOW: call this after everything the measurement
        depends on has been set.
        """
        self._refuse_during_auto("acquire")
        if not self._connected:
            raise ValueError("not connected")
        acq = self.cfg.acquisition
        settle = self.settle_time_s() + max(0.0, float(acq.extra_wait_s))
        avg = max(0.0, float(acq.average_tc)) * self._tc_actual
        now = self._clock()
        with self._lock:
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can ever show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = {"id": self._acq_id, "t0": now, "t_settle": now + settle,
                         "t_end": now + settle + avg, "settle_s": settle,
                         "avg_s": avg, "n": 0, "x": 0.0, "y": 0.0,
                         "adc": [0.0, 0.0], "overload": False, "locked": True}
            return self._acq_id

    def settle_time_s(self) -> float:
        """Settling time with the APPLIED time constant and the slope."""
        return filters.settle_time_s(self._tc_actual, self.order(),
                                     self.cfg.acquisition.settle_percent)

    def acquire_timeout_s(self) -> float:
        """What a coordinator should wait at least: the configured timeout, but
        never less than three settle-plus-average windows (a 10 s time constant
        at 24 dB/oct settles in 100 s -- a fixed 120 s would be too tight)."""
        acq = self.cfg.acquisition
        need = self.settle_time_s() + acq.extra_wait_s + acq.average_tc * self._tc_actual
        return max(float(acq.timeout_s), 3.0 * need + 5.0)

    def stream_delays(self) -> dict:
        """How late each streamed channel is, in seconds: the filter's GROUP
        DELAY, order x tau, with the tau actually applied. A convolution moves
        a feature's centroid by the kernel's mean, and n RC stages have mean
        n*tau. The ADC inputs are sampled unfiltered: no delay. # VERIFY the
        network transport delay on the instrument (a few ms per command)."""
        d = float(self.order()) * float(self._tc_actual)
        out = {k: d for k in ("x", "y", "r", "theta")}
        out["adc1"] = out["adc2"] = 0.0
        return out

    def get_sample(self) -> dict:
        with self._lock:
            return _copy_sample(self._sample)

    # ---- status ------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        ref, sig, flt = self.cfg.reference, self.cfg.signal, self.cfg.filter
        table = self.sensitivity_table()
        settle = self.settle_time_s()
        fmax = self.freq_max_Hz()
        now = self._clock()
        with self._lock:
            a = self._acq
            progress = 0.0
            if a is not None:
                span = a["t_end"] - a["t0"]
                progress = 1.0 if span <= 0 else min(1.0, (now - a["t0"]) / span)
            live = dict(self._live)
            live["adc"] = list(live["adc"])
            return Status(
                connected=self._connected, idn=self._idn, hw_error=self._hw_error,
                ref_source=ref.source,
                ref_locked=self._locked if ref.source != "internal" else None,
                freq_set_Hz=ref.frequency_Hz,
                ref_freq_Hz=self._ref_freq,
                demod_freq_Hz=self._ref_freq * ref.harmonic,
                freq_max_Hz=fmax,
                amplitude_V=ref.amplitude_V, phase_deg=ref.phase_deg,
                harmonic=ref.harmonic,
                input=sig.input, unit=tables.unit_for(sig.input),
                ac_coupled=sig.ac_coupled, coupling="AC" if sig.ac_coupled else "DC",
                sensitivity=tables.sensitivity_label(sig.sensitivity_index, sig.input),
                sensitivity_index=sig.sensitivity_index,
                full_scale=table.get(sig.sensitivity_index, math.nan),
                fast_mode=flt.fast_mode,
                tc_set_s=flt.time_constant_s, tc_s=self._tc_actual,
                slope=tables.slope_label(flt.slope_db), slope_db=flt.slope_db,
                settle_s=settle,
                live=live, overload=dict(self._overload),
                acq_id=self._acq_id, acquiring=a is not None, acq_progress=progress,
                sample=_copy_sample(self._sample),
                auto_id=self._auto_id, auto_busy=self._auto_busy,
                auto_op=self._auto_op, auto_error=self._auto_error,
            )

    # ---- config (Settings dialog / wire) ------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp everything in cfg (possibly edited in place over the wire)
        and push it all again."""
        self._refuse_during_auto("settings")
        self._sanitise_config()
        if self._connected:
            with self._hw:
                self._push_all()
        self._emit("info", "settings applied")

    # ---- polling --------------------------------------------------------------------

    def _poll_loop(self) -> None:
        # Scheduled on deadlines with time.sleep, not `self._stop.wait(period)`:
        # on Windows a timed Event.wait is rounded up to the 15.6 ms system
        # tick, so "50 Hz" would really run at ~32 Hz.
        period = 1.0 / max(1.0, float(self.cfg.hardware.poll_hz))
        next_t = time.monotonic()
        while not self._stop.is_set():
            next_t += period
            wait = next_t - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            else:
                next_t = time.monotonic()        # fell behind: do not burst
            if self._stop.is_set():
                break
            self._run_pending_auto()
            self.poll_once()

    def poll_once(self) -> None:
        """One read of the outputs, then advance any acquisition.
        Public so tests (and a single-threaded script) can drive it."""
        try:
            with self._hw:
                rd = self.backend.read_outputs(read_adc=bool(self.cfg.hardware.read_adc))
        except Exception as exc:          # never let the polling thread die
            self._report_hw_error(exc)
            return

        x, y = float(rd["x"]), float(rd["y"])
        adc = [float(v) for v in rd.get("adc", [math.nan, math.nan])][:2]
        st, ovl = int(rd.get("status", 0)), int(rd.get("overload", 0))
        fs = self.sensitivity_table().get(self.cfg.signal.sensitivity_index, math.nan)
        r = math.hypot(x, y)
        live = {"x": x, "y": y, "r": r, "theta_deg": math.degrees(math.atan2(y, x)),
                "r_fs": r / fs if fs else math.nan, "adc": adc}
        overload = {"input": bool(st & (1 << 6)), "output": bool(st & (1 << 4)) or bool(ovl & 0b11),
                    "x": bool(ovl & 0b01), "y": bool(ovl & 0b10), "byte": ovl}
        locked = not bool(st & (1 << 3))
        # Stamped with the WALL clock: a fly scan lines this stream up with
        # another instrument's, possibly on another PC.
        self.stream.append(time.time(), (x, y, r, live["theta_deg"], adc[0], adc[1]))
        now = self._clock()
        with self._lock:
            recovered = bool(self._hw_error)
            self._hw_error = ""
            self._live = live
            self._ref_freq = float(rd.get("freq_Hz", math.nan))
            self._locked = locked
            self._overload = overload
            self._advance_acquisition(now, x, y, adc,
                                      overload["input"] or overload["output"], locked)
        if recovered:
            self._emit("info", "hardware reads recovered")

    def _advance_acquisition(self, now, x, y, adc, overloaded, locked) -> None:
        """Called with _lock held. Clearing `_acq` and writing `_sample` happen
        in this ONE critical section, so no status frame can say "#n done"
        while `sample` is still #n-1 (gotcha #28)."""
        a = self._acq
        if a is None or now < a["t_settle"]:
            return
        # Accumulate X and Y (not R): averaging R would add a positive bias,
        # R of pure noise is never negative, so its mean is not zero.
        a["x"] += x
        a["y"] += y
        for k in range(2):
            a["adc"][k] += adc[k]
        a["overload"] = a["overload"] or overloaded
        a["locked"] = a["locked"] and (locked or self.cfg.reference.source == "internal")
        a["n"] += 1
        if now < a["t_end"]:
            return
        n = a["n"]
        mx, my = a["x"] / n, a["y"] / n
        self._sample = {
            "acq_id": a["id"],
            "x": mx, "y": my, "r": math.hypot(mx, my),
            "theta_deg": math.degrees(math.atan2(my, mx)),
            "adc": [v / n for v in a["adc"]],
            "overload": a["overload"], "ref_locked": a["locked"],
            "unit": tables.unit_for(self.cfg.signal.input),
            "settle_s": a["settle_s"], "avg_s": a["avg_s"], "n_avg": n,
            "time": time.time(),
        }
        self._acq = None
        if a["overload"] or not a["locked"]:
            # outside the lock would be tidier, but _emit only queues a message
            what = "OVERLOADED" if a["overload"] else "taken on an UNLOCKED reference"
            self._on_event("warn", f"sample #{a['id']} was {what}")

    def _run_pending_auto(self) -> None:
        with self._lock:
            job = self._auto_pending
            self._auto_pending = None
        if job is None:
            return
        n, op = job
        err = ""
        phase, sen = self.cfg.reference.phase_deg, self.cfg.signal.sensitivity_index
        tc = self._tc_actual
        try:
            with self._hw:
                getattr(self.backend, AUTO_OPS[op])()
                # What the operation changed, read back from the instrument.
                # The TIME CONSTANT too: the manual's own example (section
                # 6.7.02) follows ASM with "TC 13 -- set time constant to
                # 200 ms, since previous ASM changed it". Without this read-
                # back the brain would keep computing settle times from the
                # OLD tau, and every later acquisition could be read too early.
                phase = float(self.backend.get_phase())
                sen = int(self.backend.get_sensitivity_index())
                tc = float(self.backend.get_time_constant())
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        # REFP. may report anywhere in +-360 deg; status and describe use -180..180
        phase = (phase + 180.0) % 360.0 - 180.0
        with self._lock:
            # the new phase / sensitivity / tau and "not busy" become visible TOGETHER
            self.cfg.reference.phase_deg = phase
            self.cfg.signal.sensitivity_index = sen
            if tc != self._tc_actual and math.isfinite(tc) and tc > 0:
                self._tc_actual = tc
                self.cfg.filter.time_constant_s = tc
            self._auto_error = err
            self._auto_busy = False
        if err:
            self._emit("error", f"{op.replace('_', '-')} #{n} failed: {err}")
        else:
            self._emit("info", f"{op.replace('_', '-')} #{n} done: phase {phase:+.2f} deg, "
                               f"sensitivity {tables.sensitivity_label(sen, self.cfg.signal.input)}")

    def _report_hw_error(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._hw_error = msg
        now = self._clock()
        if now - self._last_err_emit >= 5.0:      # rate-limit: one event per 5 s
            self._last_err_emit = now
            self._emit("error", f"hardware read failed: {msg}")

    # ---- internals ---------------------------------------------------------------------

    def _harmonic_max(self) -> int:
        """harmonic x reference must stay within the instrument's range. On an
        internal reference we know the reference (it is our oscillator); on an
        external one it is whatever arrives at REF IN, so only the configured
        envelope applies (the instrument itself flags a parameter error)."""
        hi = int(self.cfg.limits.harmonic_max)
        f_ref = self.cfg.reference.frequency_Hz
        if self.cfg.reference.source == "internal" and f_ref > 0:
            hi = min(hi, int(self.instrument_f_max() // f_ref))
        return max(1, min(127, hi))

    def _reclamp_frequency(self) -> None:
        f = self.cfg.reference.frequency_Hz
        if f > self.freq_max_Hz():
            self.set_frequency(f)          # clamps and warns

    def _reclamp_sensitivity(self) -> None:
        table = self.sensitivity_table()
        i = self.cfg.signal.sensitivity_index
        if i not in table:
            self.set_sensitivity(min(table) if i < min(table) else max(table))

    def _sanitise_config(self) -> None:
        """Clamp every setting in cfg, in place."""
        lim, ref, sig, flt = self.cfg.limits, self.cfg.reference, self.cfg.signal, self.cfg.filter
        ref.source = _parse_source(ref.source)
        sig.input = _parse_input(sig.input)
        flt.fast_mode = _parse_bool(flt.fast_mode)
        db = _parse_slope(flt.slope_db)
        allowed_s = tables.allowed_slopes(flt.fast_mode)
        flt.slope_db = db if db in allowed_s else max(allowed_s)
        allowed = self.allowed_tcs()
        flt.time_constant_s = _clamp(float(flt.time_constant_s), min(allowed), max(allowed))[0]
        table = self.sensitivity_table()
        sig.sensitivity_index = int(_clamp(int(sig.sensitivity_index), min(table), max(table))[0])
        ref.amplitude_V = _clamp(float(ref.amplitude_V), 0.0, lim.amplitude_max_V)[0]
        ref.harmonic = int(_clamp(int(ref.harmonic), 1, max(1, min(127, int(lim.harmonic_max))))[0])
        ref.frequency_Hz = _clamp(float(ref.frequency_Hz), lim.freq_min_Hz, self.freq_max_Hz())[0]
        ref.phase_deg = (float(ref.phase_deg) + 180.0) % 360.0 - 180.0
        if sig.line_filter not in _LINE_FILTER:
            sig.line_filter = "off"

    def _push_all(self) -> None:
        """Send the whole configuration to the instrument (with _hw held).

        Order matters: the input first (it decides what a sensitivity index
        means), fast mode before the time constant and slope (it decides which
        are legal), the reference before the harmonic, the amplitude LAST so
        OSC OUT only comes up once everything else is where it should be.
        """
        ref, sig, flt = self.cfg.reference, self.cfg.signal, self.cfg.filter
        b = self.backend
        with self._hw:
            b.set_input(*_INPUT_CODES[sig.input])
            b.set_coupling(dc=not sig.ac_coupled)
            b.set_fet(sig.fet)
            b.set_float(sig.float_shield)
            b.set_line_filter(_LINE_FILTER[sig.line_filter], int(sig.line_freq_Hz) != 60)
            b.set_auto_ac_gain(sig.auto_ac_gain)
            b.set_sensitivity_index(sig.sensitivity_index)
            b.set_fast_mode(flt.fast_mode)
            self._tc_actual = self._apply_tc()
            b.set_slope_index(SLOPES_DB.index(flt.slope_db))
            b.set_ref_source(REF_SOURCES.index(ref.source))
            b.set_osc_frequency(ref.frequency_Hz)
            b.set_harmonic(ref.harmonic)
            b.set_phase(ref.phase_deg)
            b.set_osc_amplitude(ref.amplitude_V)

    def _apply_tc(self) -> float:
        idx = tables.nearest_tc_index(self.cfg.filter.time_constant_s, self.allowed_tcs())
        with self._hw:
            self.backend.set_tc_index(idx)
            return float(self.backend.get_time_constant())

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)


# ---- parsing what arrives over the wire (humans type these, too) ---------------

def _parse_source(source) -> str:
    s = str(source).strip().lower().replace(" ", "_").replace("-", "_")
    s = {"int": "internal", "ext": "ext_ttl", "external": "ext_ttl", "ttl": "ext_ttl",
         "analog": "ext_analog", "analogue": "ext_analog", "ext_analogue": "ext_analog",
         "0": "internal", "1": "ext_ttl", "2": "ext_analog"}.get(s, s)
    if s not in REF_SOURCES:
        raise ValueError(f"reference must be one of {REF_SOURCES}, got {source!r}")
    return s


def _parse_input(mode) -> str:
    m = str(mode).strip()
    for known in INPUT_MODES:
        if m.lower() == known.lower():
            return known
    aliases = {"a": "A", "b": "-B", "a-b": "A-B", "diff": "A-B", "gnd": "ground",
               "i": "I high-BW", "current": "I high-BW", "i_hb": "I high-BW",
               "i_ln": "I low-noise"}
    if m.lower() in aliases:
        return aliases[m.lower()]
    raise ValueError(f"input must be one of {INPUT_MODES}, got {mode!r}")


def _parse_slope(slope) -> int:
    s = str(slope).lower().replace("db/oct", "").replace("db", "").strip()
    try:
        db = int(round(float(s)))
    except ValueError:
        raise ValueError(f"slope must be 6, 12, 18 or 24 dB/oct, got {slope!r}")
    if db not in SLOPES_DB:
        raise ValueError(f"slope must be 6, 12, 18 or 24 dB/oct, got {slope!r}")
    return db


def _parse_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _parse_sensitivity(value, table: dict[int, float], input_mode: str) -> int:
    """A label ("100 mV") or a table index (24) -> SEN index (not yet clamped)."""
    if isinstance(value, str):
        text = value.strip()
        for i in table:
            if tables.sensitivity_label(i, input_mode).lower() == text.lower():
                return i
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"unknown sensitivity {value!r} for input {input_mode} "
                             f"(use a label such as "
                             f"{tables.sensitivity_label(max(table), input_mode)!r} or an index)")
    if isinstance(value, bool):
        raise ValueError("sensitivity must be a label or a table index")
    v = _finite(value, "sensitivity index")
    if not float(v).is_integer():
        raise ValueError(f"sensitivity index must be a whole number, got {value!r} "
                         f"(for a full-scale VALUE use set_full_scale)")
    return int(v)


def _copy_sample(s: dict) -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in s.items()}
