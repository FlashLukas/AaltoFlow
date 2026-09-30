"""DspLockIn: the brain between the wire and the SR830 backend.

Set-and-forget for its SETTINGS (reference, sensitivity, time constant, ...):
validate, clamp, push, READ BACK, report. Two things make an SR830 more than
that:

1. IT IS A DETECTOR WITH MEMORY. Its output is a low-pass-filtered average,
   so after anything changes -- the field, the sample position, the time
   constant itself -- it needs several time constants to settle. A reading
   taken too early describes the PREVIOUS state, looks perfectly clean, and is
   wrong. So there are two ways to read it (the design proven in hf2-control):

     live      the latest reading, from the polling thread. Right for a front
               panel, WRONG for a scan point.
     acquire   the scan-safe read. `acquire()` returns an id at once (the
               suite's fire-and-forget contract); the polling thread then
               waits the settling time -- COMPUTED from the applied time
               constant and slope (filters.py), plus one period of the
               detection frequency when the synchronous filter is on --
               optionally averages, and LATCHES the result as `sample`,
               together with "did anything overload meanwhile". A caller waits
               until status shows ITS id with `acquiring` False (gotcha #17).

2. IT CHANGES SETTINGS ON ITS OWN. The time constant moves when the detection
   frequency crosses 200 Hz or the reserve changes (LIA status bit 5), Auto
   Gain picks a new sensitivity, Auto Phase a new phase. So after every push,
   after every auto function and whenever the status byte says so, the brain
   READS THE SETTINGS BACK, and status reports what the instrument is set to,
   not what we last asked for.

Threads and locks:

  * ONE polling thread owns the readings. `status()` only copies what that
    thread stored -- it never touches the hardware -- so a slow GPIB query
    never stalls the status publisher, and a dead bus shows up as `hw_error`.
  * EVERY backend call runs under `_hw` (an RLock): the service's command
    thread and the polling thread would otherwise talk on one GPIB session at
    the same time.
  * While an AUTO function runs, the SR830 executes nothing else: a query
    would sit in its buffer until Auto Gain finished and the GPIB read would
    time out. So the polling thread only SERIAL POLLS (`busy()`) until it is
    done, and every setter is refused meanwhile.
  * Live control state is the config (edited by the setters) plus brain
    attributes. The polling thread builds a NEW snapshot each cycle and never
    shares an object a setter writes to (gotcha #1).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from . import filters, tables
from .backends.base import SR830Backend
from .config import Config
from .stream import StreamRecorder

#: The channels of the fly-scan stream, named like the scan detectors in the
#: manifest, so a detector id IS its stream channel.
STREAM_CHANNELS = ("x", "y", "r", "theta", "aux1", "aux2", "aux3", "aux4")

#: LIA status byte bits (manual 5-23)
LIA_INPUT, LIA_FILTER, LIA_OUTPUT, LIA_UNLOCK, LIA_RANGE, LIA_TC = (1 << b for b in range(6))

AUTO_NAMES = ("gain", "reserve", "phase")


@dataclass
class Status:
    """One snapshot of the lock-in, for status() and the wire."""

    connected: bool
    idn: str = ""
    hw_error: str = ""
    # reference
    reference_source: str = "internal"
    freq_set_Hz: float = float("nan")     # the internal-mode setpoint, as asked
    ref_freq_Hz: float = float("nan")     # FREQ? -- measured in external mode
    detect_freq_Hz: float = float("nan")  # ref x harmonic: what is demodulated
    harmonic_set: int = 1
    harmonic: int = 1                     # read back
    phase_set_deg: float = 0.0
    phase_deg: float = 0.0                # read back (Auto Phase changes it)
    trigger: str = "sine"
    unlocked: bool = False                # external reference not locked
    sine_out_set_V: float = 0.0
    sine_out_V: float = 0.0               # read back
    # input
    input_source: str = "A"
    input_ground: str = "float"
    input_coupling: str = "AC"
    line_filter: str = "off"
    unit: str = "V"                       # of X, Y, R: "A" in current mode
    # gain + filter, all READ BACK
    sensitivity: str = ""                 # label in the input's unit ("10 mV" / "10 nA")
    sens_index: int = 0
    full_scale: float = float("nan")
    reserve: str = "normal"
    time_constant: str = ""
    tc_index: int = 0
    tc_s: float = float("nan")
    slope: str = "24 dB/oct"
    order: int = 4
    sync_filter: bool = False
    settle_s: float = float("nan")        # computed settling time
    overload: dict = field(default_factory=dict)   # input / filter / output, last poll
    aux_out_set_V: list = field(default_factory=list)
    live: dict = field(default_factory=dict)       # x, y, r, theta_deg, freq_Hz, aux_in[4]
    # acquisition
    acq_id: int = 0
    acquiring: bool = False
    acq_progress: float = 0.0
    sample: dict = field(default_factory=dict)
    # auto functions
    auto_id: int = 0
    auto_busy: bool = False
    auto_name: str = ""
    auto_note: str = ""


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def _finite(value, what: str) -> float:
    """float(value), refusing NaN and inf (NaN sails straight through _clamp)."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a number, got {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return v


def _empty_live() -> dict:
    nan = float("nan")
    return {"x": nan, "y": nan, "r": nan, "theta_deg": nan, "freq_Hz": nan,
            "aux_in": [nan] * 4}


class DspLockIn:
    def __init__(self, backend: SR830Backend, cfg: Config | None = None,
                 clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()        # serialises EVERY backend call
        self._lock = threading.Lock()       # guards the snapshot + acquisition + auto

        self._connected = False
        self._idn = ""
        self._hw_error = ""
        self._last_err_emit = -1e9

        # what the instrument reports it is set to (read back after each push)
        d = self.cfg.demod
        self._rb = {"sens": tables.sens_index(d.sensitivity),
                    "reserve": tables.RESERVES.index(d.reserve) if d.reserve in tables.RESERVES else 1,
                    "tc": tables.tc_index(d.time_constant),
                    "slope": tables.SLOPES.index(d.slope) if d.slope in tables.SLOPES else 3,
                    "phase_deg": self.cfg.reference.phase_deg,
                    "harmonic": self.cfg.reference.harmonic,
                    "sine_out_V": self.cfg.reference.sine_out_V}

        # written only by the polling thread (under _lock)
        self._live = _empty_live()
        self._lias = 0

        # acquisition state (under _lock)
        self._acq_id = 0
        self._acq: dict | None = None
        self._sample: dict = {}

        # auto functions (under _lock)
        self._auto_id = 0
        self._auto: dict | None = None
        self._auto_note = ""

        self.stream = StreamRecorder(STREAM_CHANNELS, delay_fn=self.stream_delays)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_event = lambda level, msg: None

    # ---- lifecycle -----------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Open the backend, ADOPT the instrument's settings, start polling.

        ADOPT, not push (Lukas, 2026-09-27, for every module): whoever set up
        the SR830 at the front panel -- sensitivity, time constant, a SINE OUT
        that drives a modulation coil, an AUX OUT that biases something -- must
        find it exactly as they left it after the service starts. So start()
        only READS: every setting is queried and copied into `cfg` (which is
        the brain's live control state), and the status / GUI / describe show
        what the instrument is really doing. The .ini values of [reference],
        [input], [demod] and [aux_out] are therefore NOT applied at start; they
        reach the instrument only when someone sets them explicitly (a setter,
        or set_config / Settings > Apply, which write only what differs).

        `poll=False` skips the thread, so a test can drive `poll_once()` by hand.
        """
        self._sanitise_config()                 # repairs .ini typos; no hardware
        with self._hw:
            self.backend.open()
            try:
                self._idn = self.backend.idn()
                self._adopt(self.backend.read_state())
                self.backend.read_lia_status()  # forget overloads from before we came
            except Exception:
                # A reply we cannot interpret: refuse to run rather than guess
                # (and guessing would later be WRITTEN back). Nothing was set.
                try:
                    self.backend.close()
                except Exception:
                    pass
                raise
            self._connected = True
        self._emit("info", f"connected: {self._idn or 'SR830'}")
        self._emit("info", "adopted the front panel: " + self._summary())
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll_loop,
                                            name="sr830-poll", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        """Stop polling, make the OUTPUTS safe (see config.Safety), disconnect.

        Safe to call more than once. The input side needs nothing: a lock-in
        input cannot hurt the sample. SINE OUT cannot be switched off on an
        SR830, so "safe" means its 4 mV minimum.
        """
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        was = self._connected
        try:
            if was:
                self._make_outputs_safe()
            with self._hw:
                self.backend.close()
        finally:
            self._connected = False
            with self._lock:
                self._acq = None
                self._auto = None
            if was:
                self._emit("info", "disconnected")

    def _make_outputs_safe(self) -> None:
        sf, lim = self.cfg.safety, self.cfg.limits
        try:
            with self._hw:
                if sf.sine_min_on_stop:
                    self.backend.set_sine_out(lim.sine_min_V)
                if sf.aux_out_zero_on_stop:
                    for k in range(4):
                        self.backend.set_aux_out(k + 1, 0.0)
        except Exception as exc:        # a dead bus must not stop the shutdown
            self._emit("error", f"could not make outputs safe: {exc}")
            return
        if sf.sine_min_on_stop or sf.aux_out_zero_on_stop:
            what = [w for w, on in (("SINE OUT to minimum", sf.sine_min_on_stop),
                                    ("AUX OUT to 0 V", sf.aux_out_zero_on_stop)) if on]
            self._emit("info", "outputs made safe: " + ", ".join(what))

    # ---- reference -------------------------------------------------------------

    def set_reference_source(self, source: str) -> None:
        self._check_idle()
        s = tables.choice(source, tables.REF_SOURCES, "reference source")
        ref = self.cfg.reference
        ref.source = s
        if self._connected:
            with self._hw:
                self.backend.set_ref_source(s == "internal")
                if s == "internal":
                    # back from external, the oscillator sits wherever the PLL
                    # left it -- put our own setpoint back
                    self.backend.set_frequency(ref.frequency_Hz)
                self._read_back()
        self._emit("info", f"reference = {s}" + (f" at {ref.frequency_Hz:g} Hz"
                                                 if s == "internal" else ""))

    def set_frequency(self, hz: float) -> None:
        """Internal reference only. In external mode the SR830 refuses FREQ
        (it measures the frequency), so refuse here with a clear message."""
        self._check_idle()
        ref = self.cfg.reference
        if ref.source != "internal":
            raise ValueError("the reference is EXTERNAL: its frequency is measured, "
                             "not set (switch to internal first)")
        lo, hi = self._freq_range()
        value, clamped = _clamp(_finite(hz, "frequency"), lo, hi)
        ref.frequency_Hz = value
        if self._connected:
            with self._hw:
                self.backend.set_frequency(value)
                self._read_back()
        if clamped:
            self._emit("warn", f"frequency clamped to {value:g} Hz (limit {lo:g}..{hi:g}"
                               f" for harmonic {ref.harmonic})")
        else:
            self._emit("info", f"frequency = {value:g} Hz")

    def set_harmonic(self, n: int) -> None:
        self._check_idle()
        ref = self.cfg.reference
        k = int(round(_finite(n, "harmonic")))
        hi = self._harmonic_max()
        value, clamped = _clamp(k, 1, hi)
        ref.harmonic = int(value)
        if self._connected:
            with self._hw:
                self.backend.set_harmonic(ref.harmonic)
                self._read_back()
        self._emit("warn" if clamped else "info",
                   f"harmonic = {ref.harmonic}" + (f" (clamped to 1..{hi}: detection "
                                                   f"must stay below {self.cfg.limits.freq_max_Hz:g} Hz)"
                                                   if clamped else ""))

    def set_phase(self, deg: float) -> None:
        self._check_idle()
        value, clamped = _clamp(round(_finite(deg, "phase"), 2), -180.0, 180.0)
        self.cfg.reference.phase_deg = value
        if self._connected:
            with self._hw:
                self.backend.set_phase(value)
                self._read_back()
        self._emit("warn" if clamped else "info",
                   f"phase = {value:+.2f} deg" + (" (clamped to +-180)" if clamped else ""))

    def set_trigger(self, trigger: str) -> None:
        self._check_idle()
        t = tables.choice(trigger, tables.TRIGGERS, "trigger")
        self.cfg.reference.trigger = t
        if self._connected:
            with self._hw:
                self.backend.set_trigger(tables.TRIGGERS.index(t))
        self._emit("info", f"external trigger = {t}")

    def set_sine_out(self, volts: float) -> None:
        """SINE OUT amplitude, Vrms, rounded to the SR830's 2 mV steps HERE, so
        the echoed setpoint equals what the instrument applies."""
        self._check_idle()
        lim = self.cfg.limits
        value, clamped = _clamp(_finite(volts, "sine amplitude"), lim.sine_min_V, lim.sine_max_V)
        value = round(round(value / 0.002) * 0.002, 3)
        value = min(max(value, lim.sine_min_V), lim.sine_max_V)
        self.cfg.reference.sine_out_V = value
        if self._connected:
            with self._hw:
                self.backend.set_sine_out(value)
                self._read_back()
        if clamped:
            self._emit("warn", f"sine out clamped to {value:.3f} V "
                               f"(limit {lim.sine_min_V:g}..{lim.sine_max_V:g} V)")
        else:
            self._emit("info", f"sine out = {value:.3f} Vrms")

    # ---- input --------------------------------------------------------------------

    def set_input_source(self, source: str) -> None:
        self._set_input("source", tables.choice(source, tables.INPUT_SOURCES, "input source"))

    def set_input_ground(self, ground: str) -> None:
        self._set_input("ground", tables.choice(ground, tables.GROUNDS, "input ground"))

    def set_input_coupling(self, coupling: str) -> None:
        self._set_input("coupling", tables.choice(coupling, tables.COUPLINGS, "input coupling"))

    def set_line_filter(self, line: str) -> None:
        self._set_input("line_filter", tables.choice(line, tables.LINE_FILTERS, "line filter"))

    def _set_input(self, name: str, value: str) -> None:
        self._check_idle()
        setattr(self.cfg.input, name, value)
        if self._connected:
            with self._hw:
                self._push_input()
                self._read_back()
        extra = ""
        if name == "source":
            extra = f" (X, Y, R now in {tables.unit_for(value)})"
        self._emit("info", f"input {name.replace('_', ' ')} = {value}{extra}")

    # ---- gain and filter ------------------------------------------------------------

    def set_sensitivity(self, value) -> None:
        """A label ('10 mV', or '10 nA' in current mode), an index is NOT
        accepted (ambiguous with a number), or a full scale as a number in the
        input's unit -- snapped UP to the next range, so the signal still fits."""
        self._check_idle()
        v = value
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = _finite(v, "sensitivity")
            if tables.is_current(self.cfg.input.source):
                v /= tables.CURRENT_PER_VOLT          # amps -> the volts-equivalent index
        i = tables.sens_index(v)
        self.cfg.demod.sensitivity = tables.SENS_LABELS_V[i]
        if self._connected:
            with self._hw:
                self.backend.set_sensitivity(i)
                self._read_back()
        self._emit("info", f"sensitivity = {tables.sens_label(i, self.cfg.input.source)}")

    def set_reserve(self, reserve: str) -> None:
        self._check_idle()
        r = tables.choice(reserve, tables.RESERVES, "reserve")
        self.cfg.demod.reserve = r
        if self._connected:
            with self._hw:
                self.backend.set_reserve(tables.RESERVES.index(r))
                self._read_back()       # the reserve can move the time constant
        self._emit("info", f"reserve = {r}")

    def set_time_constant(self, value) -> None:
        """A label ('30 ms') or seconds (0.03), snapped to the nearest step and
        clamped to limits.tc_max. The SR830 refuses > 30 s above 200 Hz: we
        say so instead of letting it quietly apply something else."""
        self._check_idle()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = _finite(value, "time constant")
        i = tables.tc_index(value)
        asked = tables.TC_LABELS[i]
        note = ""
        i_max = tables.tc_index(self.cfg.limits.tc_max)
        if i > i_max:
            i, note = i_max, f" (clamped to the limit {tables.TC_LABELS[i_max]})"
        if (i >= tables.TC_LONG_FIRST_INDEX
                and self._detect_hz() > tables.TC_LONG_MAX_FREQ_HZ):
            i = tables.TC_LONG_FIRST_INDEX - 1
            note = (f" (the SR830 allows at most {tables.TC_LABELS[i]} above "
                    f"{tables.TC_LONG_MAX_FREQ_HZ:g} Hz detection frequency)")
        self.cfg.demod.time_constant = tables.TC_LABELS[i]
        if self._connected:
            with self._hw:
                self.backend.set_time_constant(i)
                self._read_back()
        if note:
            self._emit("warn", f"time constant {asked} -> {tables.TC_LABELS[i]}{note}")
        else:
            self._emit("info", f"time constant = {tables.TC_LABELS[i]}")

    def set_slope(self, slope) -> None:
        self._check_idle()
        s = slope
        if isinstance(slope, (int, float)) and not isinstance(slope, bool):
            s = f"{int(slope)} dB/oct"                 # 24 -> "24 dB/oct"
        s = tables.choice(s, tables.SLOPES, "slope")
        self.cfg.demod.slope = s
        if self._connected:
            with self._hw:
                self.backend.set_slope(tables.SLOPES.index(s))
                self._read_back()
        self._emit("info", f"slope = {s} (filter order {tables.slope_order(s)})")

    def set_sync_filter(self, on) -> None:
        self._check_idle()
        on = _parse_bool(on)
        self.cfg.demod.sync_filter = on
        if self._connected:
            with self._hw:
                self.backend.set_sync(on)
        note = ""
        if on and self._detect_hz() >= tables.TC_LONG_MAX_FREQ_HZ:
            note = " (inactive: it only works below 200 Hz detection frequency)"
        self._emit("info", f"synchronous filter {'on' if on else 'off'}{note}")

    # ---- aux out ----------------------------------------------------------------------

    def set_aux_out(self, channel: int, volts: float) -> None:
        self._check_idle()
        k = int(channel)
        if k not in (1, 2, 3, 4):
            raise ValueError(f"aux out channel must be 1..4, got {channel!r}")
        lim = self.cfg.limits
        value, clamped = _clamp(_finite(volts, "aux out voltage"),
                                lim.aux_out_min_V, lim.aux_out_max_V)
        value = round(value, 3)                         # the SR830 sets the nearest mV
        self.cfg.aux_out.set(k - 1, value)
        if self._connected:
            with self._hw:
                self.backend.set_aux_out(k, value)
        if clamped:
            self._emit("warn", f"aux out {k} clamped to {value:+.3f} V "
                               f"(limit {lim.aux_out_min_V:g}..{lim.aux_out_max_V:g} V)")
        else:
            self._emit("info", f"aux out {k} = {value:+.3f} V")

    # ---- output off (the safety verb) ------------------------------------------------

    def output_off(self) -> None:
        """Take what the lock-in drives off the sample: SINE OUT to its 4 mV
        minimum (an SR830 cannot switch it off) and every AUX OUT to 0 V --
        the same as a clean stop does (config.Safety), but at any time.

        A verb of its own, not set_sine_out(min) / set_aux_out(k, 0): over the
        wire it is the SAFETY verb a viewer may always send (net/service.py,
        control), and those setters can also turn an output UP. Like them it
        is refused while an auto function runs (the SR830 is busy then; it
        takes a few seconds at most).
        """
        self.set_sine_out(self.cfg.limits.sine_min_V)
        for k in (1, 2, 3, 4):
            self.set_aux_out(k, 0.0)

    # ---- auto functions ------------------------------------------------------------------

    def auto_gain(self) -> int:
        return self._start_auto("gain")

    def auto_reserve(self) -> int:
        return self._start_auto("reserve")

    def auto_phase(self) -> int:
        return self._start_auto("phase")

    def _start_auto(self, name: str) -> int:
        """Start AGAN / ARSV / APHS. Returns a run number at once (a scan
        routine waits for THAT run to finish, gotcha #17).

        Finished means: the instrument reports no command in progress, the
        settings have been read back and -- for Auto Phase -- one settling time
        has passed, because "the outputs will take many time constants to reach
        their new values" (manual 5-11).
        """
        if not self._connected:
            raise ValueError("not connected")
        self._check_idle()
        note = ""
        if name == "gain" and self.status_tc_s() > 1.0:
            note = "Auto Gain does nothing above a 1 s time constant (manual 5-11)"
        if self._acq is not None:
            # An auto function changes the gain or the phase in the middle of
            # the averaging window: the sample would mix two settings.
            raise ValueError("an acquisition is running; start the auto "
                             "function before acquiring, not during it")
        # Send the command AND mark it running inside ONE _hw section. The
        # polling thread checks `_auto` under _hw too, so it can never slip a
        # query in between -- a query sent while AGAN runs sits unanswered in
        # the SR830's buffer and the GPIB read times out.
        with self._hw:
            self.backend.auto(name)
            with self._lock:
                self._auto_id += 1
                self._auto = {"id": self._auto_id, "name": name, "stage": "running",
                              "t_hold": 0.0, "before": dict(self._rb)}
                self._auto_note = ""
                n = self._auto_id
        self._emit("warn" if note else "info", f"auto {name} #{n} started" +
                   (f" -- {note}" if note else ""))
        return n

    # ---- the scan-safe read -----------------------------------------------------------------

    def acquire(self) -> int:
        """Start a settle-then-latch acquisition. Returns its id immediately.

        The clock starts NOW: call it after everything the measurement depends
        on has been set.
        """
        if not self._connected:
            raise ValueError("not connected")
        self._check_idle()
        acq = self.cfg.acquisition
        settle = self.settle_time_s() + max(0.0, float(acq.extra_wait_s))
        avg = max(0.0, float(acq.average_tc)) * self.status_tc_s()
        now = self._clock()
        with self._lock:
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can ever show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = {"id": self._acq_id, "t0": now, "t_settle": now + settle,
                         "t_end": now + settle + avg, "settle_s": settle,
                         "avg_s": avg, "n": 0, "x": 0.0, "y": 0.0, "f": 0.0,
                         "aux": [0.0] * 4, "overload": 0}
            return self._acq_id

    def settle_time_s(self) -> float:
        """Settling time with the APPLIED time constant and slope, plus one
        detection period if the synchronous filter is active (it averages over
        whole periods; manual chapter 3, "the settling time of the synchronous
        filter is one period of the detection frequency")."""
        tc = self.status_tc_s()
        order = self._rb["slope"] + 1
        t = filters.settle_time_s(tc, order, self.cfg.acquisition.settle_percent)
        f = self._detect_hz()
        if self.cfg.demod.sync_filter and 0 < f < tables.TC_LONG_MAX_FREQ_HZ:
            t += 1.0 / f
        return t

    def status_tc_s(self) -> float:
        return tables.TC_SECONDS[self._rb["tc"]]

    def stream_delays(self) -> dict:
        """How late each streamed channel is, in seconds: the filter's GROUP
        DELAY, order x tau, with the tau the instrument actually applied (why
        that number: see hf2-control / INSTRUMENT_MODULE_GUIDE 'Streams').
        AUX IN is not filtered: no delay. # VERIFY on the SR830: the extra
        delay of the synchronous filter, and the GPIB transport delay."""
        d = (self._rb["slope"] + 1) * self.status_tc_s()
        out = {k: d for k in ("x", "y", "r", "theta")}
        out.update({f"aux{k}": 0.0 for k in range(1, 5)})
        return out

    def get_sample(self) -> dict:
        with self._lock:
            return _copy(self._sample)

    # ---- status ------------------------------------------------------------------------

    def status(self) -> Status:
        """A snapshot. Never touches the hardware (see the module docstring)."""
        cfg = self.cfg
        ref, inp, dem = cfg.reference, cfg.input, cfg.demod
        rb = dict(self._rb)
        settle = self.settle_time_s()
        now = self._clock()
        with self._lock:
            a = self._acq
            progress = 0.0
            if a is not None:
                span = a["t_end"] - a["t0"]
                progress = 1.0 if span <= 0 else min(1.0, (now - a["t0"]) / span)
            live = _copy(self._live)
            lias = self._lias
            sample = _copy(self._sample)
            auto = self._auto
            auto_note = self._auto_note
            # The ids are read HERE, under the same lock as "busy". Read after
            # the lock, an acquire() slipping in between would give a frame
            # with the NEW id and the OLD acquiring=False -- exactly the stale
            # frame a scan's wait must never see (gotcha #17 / #28).
            acq_id = self._acq_id
            auto_id = self._auto_id
        ref_hz = live["freq_Hz"] if ref.source == "external" else ref.frequency_Hz
        return Status(
            connected=self._connected,
            idn=self._idn,
            hw_error=self._hw_error,
            reference_source=ref.source,
            freq_set_Hz=ref.frequency_Hz,
            ref_freq_Hz=ref_hz,
            detect_freq_Hz=ref_hz * rb["harmonic"],
            harmonic_set=ref.harmonic,
            harmonic=rb["harmonic"],
            phase_set_deg=ref.phase_deg,
            phase_deg=rb["phase_deg"],
            trigger=ref.trigger,
            unlocked=bool(lias & LIA_UNLOCK) and ref.source == "external",
            sine_out_set_V=ref.sine_out_V,
            sine_out_V=rb["sine_out_V"],
            input_source=inp.source,
            input_ground=inp.ground,
            input_coupling=inp.coupling,
            line_filter=inp.line_filter,
            unit=tables.unit_for(inp.source),
            sensitivity=tables.sens_label(rb["sens"], inp.source),
            sens_index=rb["sens"],
            full_scale=tables.sens_full_scale(rb["sens"], inp.source),
            reserve=tables.RESERVES[rb["reserve"]],
            time_constant=tables.TC_LABELS[rb["tc"]],
            tc_index=rb["tc"],
            tc_s=tables.TC_SECONDS[rb["tc"]],
            slope=tables.SLOPES[rb["slope"]],
            order=rb["slope"] + 1,
            sync_filter=dem.sync_filter,
            settle_s=settle,
            overload={"input": bool(lias & LIA_INPUT), "filter": bool(lias & LIA_FILTER),
                      "output": bool(lias & LIA_OUTPUT)},
            aux_out_set_V=[cfg.aux_out.get(k) for k in range(4)],
            live=live,
            acq_id=acq_id,
            acquiring=a is not None,
            acq_progress=progress,
            sample=sample,
            auto_id=auto_id,
            auto_busy=auto is not None,
            auto_name=auto["name"] if auto else "",
            auto_note=auto_note,
        )

    # ---- config (Settings pane / wire) ------------------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-validate everything in cfg (possibly edited in place over the
        wire) and write to the instrument ONLY the settings that differ from
        what it is set to now (read fresh, so a knob turned at the front panel
        since start is not an accidental difference).

        Why not push everything: a set_config that only changes, say, the
        acquisition group must not rewrite SINE OUT and AUX OUT -- and a
        Settings > Apply must change what the user changed, nothing else.
        This is an EXPLICIT user action, so writing is allowed here; note that
        clamping to [limits] happens here too, so a limit narrowed in the .ini
        is enforced from this moment on."""
        self._check_idle()
        changed: list[str] = []
        if self._connected:
            with self._hw:
                # Read the instrument FIRST: a value outside [limits] that the
                # SR830 already has (adopted at start, or set at the front
                # panel) is not an edit, so it is not clamped -- otherwise an
                # Apply that only changed, say, average_tc would silently move
                # a SINE OUT or AUX OUT that nobody touched.
                now = self.backend.read_state()
                self._sanitise_config(found=now)
                changed = self._push_changed(now)
        else:
            self._sanitise_config()
        self._emit("info", "settings applied" + (
            f"; written to the SR830: {', '.join(changed)}" if changed
            else "; nothing on the SR830 needed changing"))

    # ---- polling ----------------------------------------------------------------------------

    def _poll_loop(self) -> None:
        # Deadlines with time.sleep, not Event.wait(period): on Windows a timed
        # wait rounds up to the 15.6 ms tick (gotcha #34).
        period = 1.0 / max(1.0, float(self.cfg.hardware.poll_hz))
        next_t = time.monotonic()
        while not self._stop.is_set():
            next_t += period
            wait = next_t - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            else:
                next_t = time.monotonic()
            if self._stop.is_set():
                break
            self.poll_once()

    def poll_once(self) -> None:
        """One cycle: follow a running auto function, else read outputs, aux
        and the status byte, then advance any acquisition. Public so tests can
        drive it in fake time."""
        try:
            with self._hw:
                # Checked with _hw held, so an auto function cannot start between
                # this check and the queries below (see _start_auto).
                if self._auto is not None and self._follow_auto():
                    return                      # instrument busy: do not query it
                out = self.backend.read_outputs()
                aux = [float(v) for v in self.backend.read_aux()]
                lias = int(self.backend.read_lia_status())
                if lias & (LIA_TC | LIA_RANGE):
                    before = self._rb["tc"]
                    self._read_back()
                    if self._rb["tc"] != before:
                        self._emit("warn", "the SR830 changed the time constant to "
                                           f"{tables.TC_LABELS[self._rb['tc']]} on its own "
                                           "(frequency range, reserve or slope)")
        except Exception as exc:          # never let the polling thread die
            self._report_hw_error(exc)
            return

        x, y, f = float(out["x"]), float(out["y"]), float(out["freq_Hz"])
        r = math.hypot(x, y)
        th = math.degrees(math.atan2(y, x))
        live = {"x": x, "y": y, "r": r, "theta_deg": th, "freq_Hz": f, "aux_in": aux}
        # WALL clock: a fly scan lines this up with other instruments' streams
        self.stream.append(time.time(), (x, y, r, th, *aux))
        now = self._clock()
        with self._lock:
            recovered = bool(self._hw_error)
            self._hw_error = ""
            self._live = live
            self._lias = lias
            self._advance_acquisition(now, x, y, f, aux, lias)
        if recovered:
            self._emit("info", "hardware reads recovered")
        if lias & LIA_INPUT:
            self._emit_rate_limited("warn", "INPUT OVERLOAD: raise the reserve or "
                                            "the sensitivity range")

    def _follow_auto(self) -> bool:
        """Advance a running auto function. True = the instrument is still busy
        and must not be queried this cycle."""
        a = self._auto
        if a["stage"] == "running":
            with self._hw:
                if self.backend.busy():
                    return True
                self._read_back()
            before, after = a["before"], self._rb
            note = _auto_result(a["name"], before, after, self.cfg.input.source)
            if a["name"] == "phase":
                # let the outputs settle on the new phase before calling it done
                with self._lock:
                    a["stage"] = "settling"
                    a["t_hold"] = self._clock() + self.settle_time_s()
                    self._auto_note = note
                return False
            self._finish_auto(note)
            return False
        if a["stage"] == "settling" and self._clock() >= a["t_hold"]:
            self._finish_auto(self._auto_note)
        return False

    def _finish_auto(self, note: str) -> None:
        with self._lock:
            n = self._auto["id"] if self._auto else self._auto_id
            self._auto = None
            self._auto_note = note
        self._emit("info", f"auto #{n} done: {note}")

    def _advance_acquisition(self, now, x, y, f, aux, lias) -> None:
        """Called with _lock held. Clearing `acquiring` and publishing the
        sample happen HERE, in one critical section (gotcha #28)."""
        a = self._acq
        if a is None:
            return
        # An overload anywhere in the window taints the sample, even during the
        # settle time: the filter still carries it.
        a["overload"] |= lias & (LIA_INPUT | LIA_FILTER | LIA_OUTPUT)
        if now < a["t_settle"]:
            return
        # Accumulate X and Y (not R): R of pure noise is never negative, so an
        # averaged R would carry a positive bias.
        a["x"] += x
        a["y"] += y
        a["f"] += f
        for k in range(4):
            a["aux"][k] += aux[k]
        a["n"] += 1
        if now < a["t_end"]:
            return
        n = a["n"]
        mx, my = a["x"] / n, a["y"] / n
        self._sample = {
            "acq_id": a["id"],
            "x": mx, "y": my, "r": math.hypot(mx, my),
            "theta_deg": math.degrees(math.atan2(my, mx)),
            "freq_Hz": a["f"] / n,
            "aux_in": [v / n for v in a["aux"]],
            "overload": 1 if a["overload"] else 0,
            "unit": tables.unit_for(self.cfg.input.source),
            "sensitivity": tables.sens_label(self._rb["sens"], self.cfg.input.source),
            "settle_s": a["settle_s"], "avg_s": a["avg_s"], "n_avg": n,
            "time": time.time(),
        }
        self._acq = None

    def _report_hw_error(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._hw_error = msg
        self._emit_rate_limited("error", f"hardware read failed: {msg}")

    def _emit_rate_limited(self, level: str, msg: str) -> None:
        now = self._clock()
        if now - self._last_err_emit >= 5.0:        # one message per 5 s
            self._last_err_emit = now
            self._emit(level, msg)

    # ---- internals -----------------------------------------------------------------------

    def _check_idle(self) -> None:
        if self._auto is not None:
            raise ValueError(f"auto {self._auto['name']} is running; wait until it finishes")

    def _detect_hz(self) -> float:
        ref = self.cfg.reference
        if ref.source == "external":
            f = self._live.get("freq_Hz", float("nan"))
            f = f if math.isfinite(f) else 0.0
        else:
            f = ref.frequency_Hz
        return f * self._rb["harmonic"]

    def _freq_range(self) -> tuple[float, float]:
        lim = self.cfg.limits
        n = max(1, int(self.cfg.reference.harmonic))
        return lim.freq_min_Hz, lim.freq_max_Hz / n

    def _harmonic_max(self) -> int:
        lim = self.cfg.limits
        ref = self.cfg.reference
        try:
            f = float(ref.frequency_Hz if ref.source == "internal"
                      else self._live.get("freq_Hz", 0.0))
        except (TypeError, ValueError):
            f = 0.0
        if not math.isfinite(f) or f <= 0:
            f = lim.freq_min_Hz
        return max(1, min(int(lim.harmonic_max), int(lim.freq_max_Hz // f)))

    def _sanitise_config(self, found: dict | None = None) -> None:
        """Validate and clamp every setting in cfg, in place. A bad enum value
        (a typo in the .ini) falls back to the default instead of reaching the
        instrument.

        `found` = the instrument's read_state(): a value that is ALREADY what
        the instrument is set to is left alone even outside [limits] (the
        limits guard what we SEND; they do not move what someone else set)."""
        from .config import CHOICES, Config as _C
        defaults = _C()
        for (group, name), options in CHOICES.items():
            grp = getattr(self.cfg, group)
            val = getattr(grp, name)
            num = _maybe_number(val)        # "0.1" typed into the .ini = 0.1 s / 0.1 V
            if (group, name) == ("demod", "sensitivity"):
                try:
                    grp.sensitivity = tables.SENS_LABELS_V[tables.sens_index(num)]
                    continue
                except ValueError:
                    pass
            elif name in ("time_constant", "tc_max"):
                try:
                    setattr(grp, name, tables.TC_LABELS[tables.tc_index(num)])
                    continue
                except ValueError:
                    pass
            else:
                try:
                    setattr(grp, name, tables.choice(val, options, name))
                    continue
                except ValueError:
                    pass
            fallback = getattr(getattr(defaults, group), name)
            self._emit("warn", f"{group}.{name} = {val!r} is not valid; using {fallback!r}")
            setattr(grp, name, fallback)
        # Booleans that came over the wire as text ("False" is truthy, gotcha #3)
        for group, name in (("demod", "sync_filter"), ("safety", "sine_min_on_stop"),
                            ("safety", "aux_out_zero_on_stop"),
                            ("hardware", "front_panel_override")):
            grp = getattr(self.cfg, group)
            try:
                setattr(grp, name, _parse_bool(getattr(grp, name)))
            except ValueError:
                fallback = getattr(getattr(defaults, group), name)
                self._emit("warn", f"{group}.{name} = {getattr(grp, name)!r} is not "
                                   f"on/off; using {fallback!r}")
                setattr(grp, name, fallback)
        ref, lim = self.cfg.reference, self.cfg.limits
        f = found or {}

        def as_found(key, value, tol=0.0) -> bool:
            # True when `value` is what the instrument already has
            return key in f and abs(float(value) - float(f[key])) <= tol

        if not as_found("harmonic", ref.harmonic):
            ref.harmonic = int(_clamp(int(ref.harmonic), 1, int(lim.harmonic_max))[0])
        # the frequency is only ever written in internal mode, and FREQ? is the
        # oscillator there; in external mode FREQ? is a measurement, not "found"
        if not (f.get("internal") and ref.source == "internal"
                and as_found("freq_Hz", ref.frequency_Hz,
                             1e-9 * max(1.0, abs(float(ref.frequency_Hz))))):
            lo, hi = self._freq_range()
            ref.frequency_Hz = _clamp(float(ref.frequency_Hz), lo, hi)[0]
        ref.phase_deg = _clamp(float(ref.phase_deg), -180.0, 180.0)[0]
        if not as_found("sine_out_V", ref.sine_out_V, 0.001):
            ref.sine_out_V = _clamp(float(ref.sine_out_V), lim.sine_min_V, lim.sine_max_V)[0]
        for k in range(4):
            v = self.cfg.aux_out.get(k)
            if "aux_out_V" in f and abs(float(v) - float(f["aux_out_V"][k])) <= 0.0005:
                continue
            self.cfg.aux_out.set(k, _clamp(v, lim.aux_out_min_V, lim.aux_out_max_V)[0])
        dem = self.cfg.demod
        if (tables.tc_index(dem.time_constant) > tables.tc_index(lim.tc_max)
                and not as_found("tc", tables.tc_index(dem.time_constant))):
            dem.time_constant = lim.tc_max

    def _push_input(self) -> None:
        inp = self.cfg.input
        self.backend.set_input(tables.INPUT_SOURCES.index(inp.source),
                               tables.GROUNDS.index(inp.ground),
                               tables.COUPLINGS.index(inp.coupling),
                               tables.LINE_FILTERS.index(inp.line_filter))

    def _adopt(self, st: dict) -> None:
        """Copy the instrument's settings into cfg and the read-back (with _hw
        held). Nothing is clamped or written: a value outside [limits] is
        ADOPTED as it is, with a warning -- the limits guard what WE send, they
        are not a reason to change what someone else set."""
        ref, inp, dem = self.cfg.reference, self.cfg.input, self.cfg.demod
        ref.source = "internal" if st["internal"] else "external"
        f = float(st["freq_Hz"])
        # In external mode FREQ? is the MEASURED reference (possibly 0 while
        # unlocked). Keep it as the setpoint anyway when it is sensible, so a
        # later switch to internal continues at the frequency the experiment ran.
        if math.isfinite(f) and f > 0:
            ref.frequency_Hz = f
        ref.harmonic = int(st["harmonic"])
        ref.phase_deg = round(float(st["phase_deg"]), 2)
        ref.trigger = _pick(tables.TRIGGERS, st["trigger"], "RSLP?")
        # rounded like the setters round them, so the *_set echo keys match
        ref.sine_out_V = round(float(st["sine_out_V"]), 3)
        inp.source = _pick(tables.INPUT_SOURCES, st["source"], "ISRC?")
        inp.ground = _pick(tables.GROUNDS, st["ground"], "IGND?")
        inp.coupling = _pick(tables.COUPLINGS, st["coupling"], "ICPL?")
        inp.line_filter = _pick(tables.LINE_FILTERS, st["line"], "ILIN?")
        dem.sensitivity = _pick(tables.SENS_LABELS_V, st["sens"], "SENS?")
        dem.reserve = _pick(tables.RESERVES, st["reserve"], "RMOD?")
        dem.time_constant = _pick(tables.TC_LABELS, st["tc"], "OFLT?")
        dem.slope = _pick(tables.SLOPES, st["slope"], "OFSL?")
        dem.sync_filter = bool(st["sync"])
        for k, v in enumerate(list(st["aux_out_V"])[:4]):
            self.cfg.aux_out.set(k, round(float(v), 3))
        self._rb = {"sens": int(st["sens"]), "reserve": int(st["reserve"]),
                    "tc": int(st["tc"]), "slope": int(st["slope"]),
                    "phase_deg": ref.phase_deg, "harmonic": ref.harmonic,
                    "sine_out_V": float(st["sine_out_V"])}
        # Say so when the front panel is outside OUR envelope -- but leave it.
        lim = self.cfg.limits
        notes = []
        if not lim.sine_min_V <= ref.sine_out_V <= lim.sine_max_V:
            notes.append(f"SINE OUT {ref.sine_out_V:.3f} V")
        for k in range(4):
            v = self.cfg.aux_out.get(k)
            if not lim.aux_out_min_V <= v <= lim.aux_out_max_V:
                notes.append(f"AUX OUT {k + 1} {v:+.3f} V")
        if tables.tc_index(dem.time_constant) > tables.tc_index(lim.tc_max):
            notes.append(f"time constant {dem.time_constant}")
        if ref.harmonic > lim.harmonic_max:
            notes.append(f"harmonic {ref.harmonic}")
        if notes:
            self._emit("warn", "adopted as found, outside [limits]: " + ", ".join(notes)
                       + " (a setter clamps it only when you change it)")

    def _summary(self) -> str:
        """One line for the event log: what the SR830 was found doing."""
        ref, inp, dem = self.cfg.reference, self.cfg.input, self.cfg.demod
        f = f"{ref.frequency_Hz:g} Hz internal" if ref.source == "internal" else "external ref"
        return (f"{f}, harmonic {ref.harmonic}, phase {ref.phase_deg:+.2f} deg, "
                f"sine {ref.sine_out_V:.3f} V, input {inp.source}, "
                f"{tables.sens_label(self._rb['sens'], inp.source)}, "
                f"{dem.time_constant}, {dem.slope}, reserve {dem.reserve}, aux out "
                + "/".join(f"{self.cfg.aux_out.get(k):g}" for k in range(4)) + " V")

    def _push_changed(self, now: dict | None = None) -> list[str]:
        """Write the cfg settings that DIFFER from the instrument (with _hw
        held), then read back. Returns what was written (for the event log).

        Order matters on an SR830: the allowed time constant depends on slope
        and reserve, so those go first; and the frequency only exists in
        internal mode.
        """
        ref, inp, dem = self.cfg.reference, self.cfg.input, self.cfg.demod
        b = self.backend
        if now is None:
            now = b.read_state()
        done: list[str] = []

        def differs(a: float, c: float, tol: float) -> bool:
            return abs(float(a) - float(c)) > tol

        internal = ref.source == "internal"
        if internal != bool(now["internal"]):
            b.set_ref_source(internal)
            done.append("reference source")
        # re-written if it differs at all: the SR830 rounds FREQ to 5 digits,
        # so a 6-digit request is sent again each time -- harmless, same result
        if internal and differs(ref.frequency_Hz, now["freq_Hz"],
                                1e-9 * max(1.0, abs(ref.frequency_Hz))):
            b.set_frequency(ref.frequency_Hz)
            done.append("frequency")
        if int(ref.harmonic) != int(now["harmonic"]):
            b.set_harmonic(ref.harmonic)
            done.append("harmonic")
        if differs(ref.phase_deg, now["phase_deg"], 0.005):         # 0.01 deg steps
            b.set_phase(ref.phase_deg)
            done.append("phase")
        if tables.TRIGGERS.index(ref.trigger) != int(now["trigger"]):
            b.set_trigger(tables.TRIGGERS.index(ref.trigger))
            done.append("trigger")
        if differs(ref.sine_out_V, now["sine_out_V"], 0.001):        # 2 mV steps
            b.set_sine_out(ref.sine_out_V)
            done.append("sine out")
        want_in = (tables.INPUT_SOURCES.index(inp.source), tables.GROUNDS.index(inp.ground),
                   tables.COUPLINGS.index(inp.coupling),
                   tables.LINE_FILTERS.index(inp.line_filter))
        if want_in != tuple(int(now[k]) for k in ("source", "ground", "coupling", "line")):
            self._push_input()
            done.append("input")
            # Switching ISRC between voltage and current may move sensitivity
            # on its own (VERIFY #6): compare the gain/filter against what the
            # instrument has NOW, not before the input change.
            now = b.read_state()
        for key, value, name, setter in (
                ("sens", tables.sens_index(dem.sensitivity), "sensitivity", b.set_sensitivity),
                ("reserve", tables.RESERVES.index(dem.reserve), "reserve", b.set_reserve),
                ("slope", tables.SLOPES.index(dem.slope), "slope", b.set_slope),
                ("tc", tables.tc_index(dem.time_constant), "time constant",
                 b.set_time_constant)):
            if value != int(now[key]):
                setter(value)
                done.append(name)
        if bool(dem.sync_filter) != bool(now["sync"]):
            b.set_sync(dem.sync_filter)
            done.append("sync filter")
        for k in range(4):
            if differs(self.cfg.aux_out.get(k), now["aux_out_V"][k], 0.0005):   # 1 mV steps
                b.set_aux_out(k + 1, self.cfg.aux_out.get(k))
                done.append(f"aux out {k + 1}")
        self._read_back()
        return done

    def _read_back(self) -> None:
        """What the instrument is actually set to (with _hw held)."""
        rb = self.backend.read_settings()
        self._rb = {"sens": int(rb["sens"]), "reserve": int(rb["reserve"]),
                    "tc": int(rb["tc"]), "slope": int(rb["slope"]),
                    "phase_deg": float(rb["phase_deg"]), "harmonic": int(rb["harmonic"]),
                    "sine_out_V": float(rb["sine_out_V"])}

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)


def _auto_result(name: str, before: dict, after: dict, source: str) -> str:
    if name == "gain":
        a, b = (tables.sens_label(x["sens"], source) for x in (before, after))
        return f"sensitivity {a} -> {b}" if a != b else f"sensitivity stays {b}"
    if name == "reserve":
        a, b = tables.RESERVES[before["reserve"]], tables.RESERVES[after["reserve"]]
        return f"reserve {a} -> {b}" if a != b else f"reserve stays {b}"
    return f"phase {before['phase_deg']:+.2f} -> {after['phase_deg']:+.2f} deg"


def _pick(options, index, query: str) -> str:
    """options[index] for an index the instrument replied, refusing nonsense
    (a garbled reply must not silently become some other setting)."""
    i = int(index)
    if not 0 <= i < len(options):
        raise ValueError(f"{query} replied {index!r}: not one of 0..{len(options) - 1}")
    return options[i]


def _maybe_number(v):
    """'0.1' -> 0.1; anything that is not a plain number is returned unchanged."""
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return v
    return v


def _parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "on", "yes"):
        return True
    if s in ("0", "false", "off", "no"):
        return False
    raise ValueError(f"expected on/off, got {v!r}")


def _copy(d: dict) -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in d.items()}
