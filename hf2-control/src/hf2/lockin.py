"""The LockIn: the brain between the wire and the backend.

Set-and-forget like the SMB100A for its SETTINGS (time constant, filter order,
reference mode, frequency): clamp, push, report. Where it differs is that a
lock-in is a DETECTOR WITH MEMORY. Its output is a low-pass-filtered average,
so after anything changes -- the field, the sample position, the time constant
itself -- the output needs several time constants to settle. A reading taken
too early describes the PREVIOUS state, looks perfectly clean, and is wrong.

So there are two ways to read it:

  live      the latest demodulator output, updated by the polling thread.
            Right for a front panel. WRONG for a scan point: nothing guarantees
            it has settled since the last change.

  acquire   the scan-safe read. `acquire()` returns an id at once (the suite's
            fire-and-forget contract); the polling thread then waits the
            settling time -- COMPUTED from each channel's time constant and
            order, see filters.py -- optionally averages over a window, and
            LATCHES the result as `sample`. Status shows `acq_id` and
            `acquiring`; a caller waits until `acq_id` is its own id AND
            `acquiring` is False. Checking the id first is what stops a stale
            "not acquiring" from the previous point fooling it (the same trap
            as clMag's stale `field_stable`).

Threads and locks (both rules learned the hard way elsewhere in the suite):

  * ONE polling thread owns the demodulator reads. `status()` only copies what
    that thread stored -- it never touches the hardware. So a slow USB read can
    never stall the status publisher, and a dead link shows up as `hw_error`
    instead of a healthy-looking panel full of zeros (kim's lesson).
  * EVERY backend call runs under `_hw` (an RLock). The service's command
    thread and the polling thread would otherwise talk to the instrument at the
    same time, and a vendor connection is not guaranteed to cope (kim's serial
    link desynchronised exactly that way).
  * Live control state is the config itself (cfg.ch1 / cfg.ch2, edited by the
    setters) plus a few brain attributes. The polling thread builds a NEW
    snapshot each cycle and never shares an object a setter writes to
    (gotcha #1, the lost-update race).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from . import filters
from .backends.base import LockInBackend
from .config import Config, REF_MODES

N_CHANNELS = 2


@dataclass
class Status:
    """One snapshot of the lock-in, for status() and the wire. Per-channel
    values are two-element lists, index 0 = channel 1."""

    connected: bool
    idn: str = ""
    hw_error: str = ""
    reference: list = field(default_factory=list)      # "internal" | "external"
    freq_set_Hz: list = field(default_factory=list)    # internal-mode setpoint
    ref_freq_Hz: list = field(default_factory=list)    # oscillator frequency (measured)
    tc_set_s: list = field(default_factory=list)       # time constant we asked for
    tc_s: list = field(default_factory=list)           # time constant the hardware applied
    order: list = field(default_factory=list)
    pll_locked: list = field(default_factory=list)     # bool, or None in internal mode
    settle_s: list = field(default_factory=list)       # computed settling time
    live: dict = field(default_factory=dict)           # x, y, r, theta_deg, freq_Hz, aux_in
    acq_id: int = 0
    acquiring: bool = False
    acq_progress: float = 0.0
    sample: dict = field(default_factory=dict)         # the last LATCHED acquisition


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
    return {"x": [nan, nan], "y": [nan, nan], "r": [nan, nan],
            "theta_deg": [nan, nan], "freq_Hz": [nan, nan], "aux_in": [nan, nan]}


class LockIn:
    def __init__(self, backend: LockInBackend, cfg: Config | None = None,
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

        # what the hardware reports back after a set (it may round the tau)
        self._tc_actual = [self.cfg.channel(i).time_constant_s for i in range(N_CHANNELS)]
        self._order_actual = [self.cfg.channel(i).order for i in range(N_CHANNELS)]

        # written only by the polling thread (under _lock)
        self._live = _empty_live()
        self._ref_freq = [float("nan")] * N_CHANNELS
        self._locked: list = [None] * N_CHANNELS

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
        """Open the backend, push both channels' settings, start polling.

        `poll=False` skips the thread, so a test can drive `poll_once()` by hand.
        """
        self._sanitise_config()
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            self._connected = True
            for i in range(N_CHANNELS):
                self._push_channel(i)
        self._emit("info", f"connected: {self._idn or 'HF2LI'}")
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="hf2-poll", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop polling and disconnect. Safe to call more than once.

        Nothing to make safe first: this module never enables a signal output,
        so disconnecting leaves the instrument exactly as it was.
        """
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

    # ---- settings (each clamps, stores in cfg, pushes) ------------------------

    def set_time_constant(self, channel: int, tc_s: float) -> None:
        i = self._index(channel)
        lim = self.cfg.limits
        value, clamped = _clamp(_finite(tc_s, "time constant"), lim.tc_min_s, lim.tc_max_s)
        ch = self.cfg.channel(i)
        ch.time_constant_s = value
        self._tc_actual[i] = self._apply_tc(i) if self._connected else value
        if clamped:
            self._emit("warn", f"ch{i + 1} time constant clamped to {_fmt_s(value)} "
                               f"(limit {_fmt_s(lim.tc_min_s)}..{_fmt_s(lim.tc_max_s)})")
        else:
            self._emit("info", f"ch{i + 1} time constant = {_fmt_s(self._tc_actual[i])}")

    def set_order(self, channel: int, order: int) -> None:
        i = self._index(channel)
        lim = self.cfg.limits
        n = int(round(_finite(order, "filter order")))
        value, clamped = _clamp(n, lim.order_min, lim.order_max)
        value = int(value)
        ch = self.cfg.channel(i)
        ch.order = value
        self._order_actual[i] = self._apply_order(i) if self._connected else value
        level = "warn" if clamped else "info"
        self._emit(level, f"ch{i + 1} filter order = {value}"
                          + (f" (clamped to {lim.order_min}..{lim.order_max})" if clamped else ""))

    def set_frequency(self, channel: int, hz: float) -> None:
        """Internal reference only. In external mode the PLL owns the
        oscillator, so a set would be silently overridden -- refuse instead."""
        i = self._index(channel)
        ch = self.cfg.channel(i)
        if ch.reference == "external":
            raise ValueError(f"ch{i + 1} follows the EXTERNAL reference; its frequency "
                             f"is measured, not set (switch it to internal first)")
        lim = self.cfg.limits
        value, clamped = _clamp(_finite(hz, "frequency"), lim.freq_min_Hz, lim.freq_max_Hz)
        ch.frequency_Hz = value
        if self._connected:
            with self._hw:
                self.backend.set_oscillator_frequency(ch.oscillator, value)
        if clamped:
            self._emit("warn", f"ch{i + 1} frequency clamped to {value:g} Hz "
                               f"(limit {lim.freq_min_Hz:g}..{lim.freq_max_Hz:g})")
        else:
            self._emit("info", f"ch{i + 1} frequency = {value:g} Hz")

    def set_reference(self, channel: int, mode: str) -> None:
        i = self._index(channel)
        m = _parse_mode(mode)
        ch = self.cfg.channel(i)
        ch.reference = m
        if self._connected:
            self._apply_reference(i)
        self._emit("info", f"ch{i + 1} reference = {m}"
                           + (f" (input {ch.ref_input})" if m == "external" else
                              f" at {ch.frequency_Hz:g} Hz"))

    # ---- the scan-safe read ---------------------------------------------------

    def acquire(self) -> int:
        """Start a settle-then-latch acquisition. Returns its id immediately.

        The settling time is the LONGER of the two channels' (a scan reads both
        from one acquisition, so both must have settled), plus extra_wait_s.
        The clock starts NOW: call this after everything the measurement depends
        on has been set.
        """
        if not self._connected:
            raise ValueError("not connected")
        acq = self.cfg.acquisition
        settle = max(self.settle_time_s(i) for i in range(N_CHANNELS))
        settle += max(0.0, float(acq.extra_wait_s))
        avg = max(0.0, float(acq.average_tc)) * max(self._tc_actual)
        now = self._clock()
        with self._lock:
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can ever show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = {"id": self._acq_id, "t0": now, "t_settle": now + settle,
                         "t_end": now + settle + avg, "settle_s": settle,
                         "avg_s": avg, "n": 0,
                         "x": [0.0] * N_CHANNELS, "y": [0.0] * N_CHANNELS,
                         "f": [0.0] * N_CHANNELS, "aux": [0.0, 0.0]}
            return self._acq_id

    def settle_time_s(self, i: int) -> float:
        """Settling time of channel index i with its APPLIED tau and order."""
        return filters.settle_time_s(self._tc_actual[i], self._order_actual[i],
                                     self.cfg.acquisition.settle_percent)

    def get_sample(self) -> dict:
        with self._lock:
            return _copy_sample(self._sample)

    # ---- status ------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        chans = [self.cfg.channel(i) for i in range(N_CHANNELS)]
        settle = [self.settle_time_s(i) for i in range(N_CHANNELS)]
        now = self._clock()
        with self._lock:
            a = self._acq
            progress = 0.0
            if a is not None:
                span = a["t_end"] - a["t0"]
                progress = 1.0 if span <= 0 else min(1.0, (now - a["t0"]) / span)
            return Status(
                connected=self._connected,
                idn=self._idn,
                hw_error=self._hw_error,
                reference=[c.reference for c in chans],
                freq_set_Hz=[c.frequency_Hz for c in chans],
                ref_freq_Hz=list(self._ref_freq),
                tc_set_s=[c.time_constant_s for c in chans],
                tc_s=list(self._tc_actual),
                order=list(self._order_actual),
                pll_locked=list(self._locked),
                settle_s=settle,
                live={k: list(v) for k, v in self._live.items()},
                acq_id=self._acq_id,
                acquiring=a is not None,
                acq_progress=progress,
                sample=_copy_sample(self._sample),
            )

    # ---- config (Settings dialog / wire) ------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp everything in cfg (possibly edited in place over the wire)
        and push both channels again."""
        self._sanitise_config()
        if self._connected:
            with self._hw:
                for i in range(N_CHANNELS):
                    self._push_channel(i)
        self._emit("info", "settings applied")

    # ---- polling --------------------------------------------------------------------

    def _poll_loop(self) -> None:
        period = 1.0 / max(1.0, float(self.cfg.hardware.poll_hz))
        while not self._stop.wait(period):
            self.poll_once()

    def poll_once(self) -> None:
        """One read of both demodulators + aux, then advance any acquisition.
        Public so tests (and a single-threaded script) can drive it."""
        chans = [self.cfg.channel(i) for i in range(N_CHANNELS)]
        try:
            with self._hw:
                rd = self.backend.read_demods([c.demod for c in chans])
                aux = list(self.backend.read_aux())
                locked = [self.backend.pll_locked(c.oscillator)
                          if c.reference == "external" else None for c in chans]
        except Exception as exc:          # never let the polling thread die
            self._report_hw_error(exc)
            return

        x = [float(r["x"]) for r in rd]
        y = [float(r["y"]) for r in rd]
        f = [float(r["freq_Hz"]) for r in rd]
        live = {"x": x, "y": y,
                "r": [math.hypot(a, b) for a, b in zip(x, y)],
                "theta_deg": [math.degrees(math.atan2(b, a)) for a, b in zip(x, y)],
                "freq_Hz": f, "aux_in": aux}
        ref = [f[i] / max(1, int(chans[i].harmonic)) for i in range(N_CHANNELS)]
        now = self._clock()
        recovered = False
        with self._lock:
            recovered = bool(self._hw_error)
            self._hw_error = ""
            self._live = live
            self._ref_freq = ref
            self._locked = locked
            self._advance_acquisition(now, x, y, f, aux)
        if recovered:
            self._emit("info", "hardware reads recovered")

    def _advance_acquisition(self, now, x, y, f, aux) -> None:
        """Called with _lock held."""
        a = self._acq
        if a is None or now < a["t_settle"]:
            return
        # Accumulate X and Y (not R). Averaging R would add a positive bias:
        # R of pure noise is never negative, so its mean is not zero.
        for i in range(N_CHANNELS):
            a["x"][i] += x[i]
            a["y"][i] += y[i]
            a["f"][i] += f[i]
        for k in range(2):
            a["aux"][k] += aux[k]
        a["n"] += 1
        if now < a["t_end"]:
            return
        n = a["n"]
        mx = [v / n for v in a["x"]]
        my = [v / n for v in a["y"]]
        self._sample = {
            "acq_id": a["id"],
            "x": mx, "y": my,
            "r": [math.hypot(p, q) for p, q in zip(mx, my)],
            "theta_deg": [math.degrees(math.atan2(q, p)) for p, q in zip(mx, my)],
            "freq_Hz": [v / n for v in a["f"]],
            "aux_in": [v / n for v in a["aux"]],
            "settle_s": a["settle_s"], "avg_s": a["avg_s"], "n_avg": n,
            "time": time.time(),
        }
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

    def _index(self, channel) -> int:
        try:
            c = int(channel)
        except (TypeError, ValueError):
            raise ValueError(f"channel must be 1 or 2, got {channel!r}")
        if c not in (1, 2):
            raise ValueError(f"channel must be 1 or 2, got {channel!r}")
        return c - 1

    def _sanitise_config(self) -> None:
        """Clamp every per-channel setting in cfg, in place."""
        lim = self.cfg.limits
        for i in range(N_CHANNELS):
            ch = self.cfg.channel(i)
            ch.reference = _parse_mode(ch.reference)
            ch.time_constant_s = _clamp(float(ch.time_constant_s), lim.tc_min_s, lim.tc_max_s)[0]
            ch.order = int(_clamp(int(ch.order), lim.order_min, lim.order_max)[0])
            ch.frequency_Hz = _clamp(float(ch.frequency_Hz), lim.freq_min_Hz, lim.freq_max_Hz)[0]

    def _push_channel(self, i: int) -> None:
        """Send one channel's whole configuration to the hardware (with _hw held)."""
        ch = self.cfg.channel(i)
        with self._hw:
            self.backend.setup_channel(ch, self.cfg.hardware.demod_rate_Sa_s)
            self._apply_reference(i)
            self._tc_actual[i] = self._apply_tc(i)
            self._order_actual[i] = self._apply_order(i)

    def _apply_reference(self, i: int) -> None:
        ch = self.cfg.channel(i)
        with self._hw:
            self.backend.set_reference(ch.oscillator, ch.reference == "external", ch.ref_input)
            if ch.reference == "internal":
                # leaving external mode, the oscillator is wherever the PLL left
                # it -- put our own setpoint back
                self.backend.set_oscillator_frequency(ch.oscillator, ch.frequency_Hz)

    def _apply_tc(self, i: int) -> float:
        ch = self.cfg.channel(i)
        with self._hw:
            self.backend.set_time_constant(ch.demod, ch.time_constant_s)
            return float(self.backend.get_time_constant(ch.demod))

    def _apply_order(self, i: int) -> int:
        ch = self.cfg.channel(i)
        with self._hw:
            self.backend.set_order(ch.demod, ch.order)
            return int(self.backend.get_order(ch.demod))

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)


def _parse_mode(mode) -> str:
    m = str(mode).strip().lower()
    m = {"int": "internal", "ext": "external"}.get(m, m)
    if m not in REF_MODES:
        raise ValueError(f"reference must be one of {REF_MODES}, got {mode!r}")
    return m


def _copy_sample(s: dict) -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in s.items()}


def _fmt_s(seconds: float) -> str:
    """0.00123 -> '1.23 ms', human-scaled for the event log."""
    s = float(seconds)
    if s >= 1:
        return f"{s:.4g} s"
    if s >= 1e-3:
        return f"{s * 1e3:.4g} ms"
    return f"{s * 1e6:.4g} us"
