"""SignalSource: the brain between the wire and the HP 8648D backend.

A CW signal generator needs no control loop, so the job is small but has to be
done carefully:

  * hold the DESIRED signal (frequency, level, RF on/off),
  * CLAMP every request to the safety envelope -- including the instrument's
    frequency-dependent maximum level (spec.py) -- and announce a clamp as a
    `warn` event, so nothing silently drives the sample harder than intended,
  * let ONE worker thread own the instrument: it writes pending changes, waits
    for the synthesiser to switch, reads everything back and publishes a fresh
    status snapshot,
  * watch the reverse-power protection (RPP) and follow it: when the box trips
    and turns its RF off, the desired state becomes "off" too, so nothing turns
    it back on behind the operator's back,
  * SWEEP frequency or level continuously at a set pace, for fly scans
    (2026-10-10; see "the SWEEPS" below). The 8648D has no phase control, so
    there is no phase sweep here (smb, the reference, has one).

THREADS (gotcha #1). Setters only change brain attributes (under `_lock`) and
wake the worker; they never touch the hardware and never touch the snapshot.
The worker builds a NEW Status object every cycle and swaps it in with one
assignment, so a reader always sees a complete, consistent frame. status()
therefore never talks to the instrument -- it cannot block on a slow GPIB bus,
and a GUI polling it at 60 ms costs nothing.

WHY THE ECHO IS HONEST. The worker reads back only AFTER it has written and
waited `hardware.switch_settle_s` (the synthesiser's switching time). So when
status shows the frequency a scan asked for, the instrument has not just
accepted it but has had time to get there -- that is what describe's `echoes`
settle policy waits on.

ADOPT AT START (Lukas, 2026-09-27: "all modules should read the instrument
state on startup, not to change anything"). start() READS the generator --
RF on/off, frequency, level, modulation, protection -- and makes that the
desired state. Nothing is written, so connecting the service never moves a
level, switches the RF or kills a modulation someone set up by hand. The
config's `signal` values are defaults for the Settings dialog, sent only when
you change them there (apply_config). RF OFF at shutdown is unchanged.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field, replace

from . import spec
from .backends.base import RPP_BIT, UNSPECIFIED_BIT, SigGenBackend
from .config import Config
from .softramp import SoftRamp

#: The knobs a sweep can walk, and how each one is named on the wire:
#: knob -> (brain attribute, wire unit, rate unit on the wire). The pace
#: limits are config.Limits ramp_rate_min_<rate unit> / ramp_rate_max_<rate
#: unit>. No "phase": the 8648D has no phase adjustment (its SCPI table has
#: no PHASe subsystem; PM is a modulation, not an offset).
SWEEP_KNOBS = {
    "frequency": ("_freq", "Hz", "Hz_per_s"),
    "power": ("_power", "dBm", "dB_per_s"),
}


@dataclass(frozen=True)
class Status:
    """One snapshot, for status() and the wire. Frozen: never edited, only
    replaced as a whole by the worker thread."""

    rf_on: bool                    # read back from the instrument
    frequency_Hz: float            # read back
    power_dBm: float               # read back
    rf_set: bool                   # what we asked for
    frequency_set_Hz: float
    power_set_dBm: float
    power_ceiling_dBm: float       # the clamp at the current frequency
    spec_max_dBm: float            # the instrument's specified max there
    rpp_tripped: bool = False      # reverse-power protection has fired
    level_unspecified: bool = False
    modulation_off: bool = True    # AM, FM and PM all off
    connected: bool = False
    idn: str = ""
    hw_error: str = ""
    modulation: dict = field(default_factory=dict)
    # The SWEEPS, flat wire keys (see SignalSource.sweep_status): `ramping` =
    # any knob sweeping; per knob `<knob>_ramping`, `<knob>_ramp_id`, and the
    # target and pace in wire units (e.g. frequency_ramp_target_Hz). The
    # worker fills it at each cycle; status() lays the LIVE values over a
    # COPY when they have changed since.
    sweep: dict = field(default_factory=dict)


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


class SignalSource:
    #: How many worker cycles between modulation checks (3 GPIB queries each;
    #: nothing in this module switches modulation on, so a slow check is enough).
    MOD_CHECK_EVERY = 10

    def __init__(self, backend: SigGenBackend, cfg: Config | None = None):
        self.backend = backend
        self.cfg = cfg or Config()
        s = self.cfg.signal
        self._lock = threading.Lock()          # guards the desired-state attributes
        self._hw_lock = threading.RLock()      # one owner of the instrument at a time
        self._freq = float(s.frequency_Hz)
        self._power = float(s.power_dBm)
        self._rf = False                       # placeholder until start() adopts
        self._dirty: set[str] = set()
        self._connected = False
        self._rpp_latched = False
        self._last_hw_error = ""
        self._cycle_n = 0
        self._need_adopt = False               # start() could not read: adopt later
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None
        # clamp the start-up values once, silently fixing a bad .ini
        self._freq = _clamp(self._freq, *self.freq_limits())[0]
        self._power = _clamp(self._power, self.power_floor(),
                             self.power_ceiling(self._freq))[0]
        # What cfg.signal held when we last looked: apply_config() sends the
        # signal defaults only when they CHANGE, so an OK in Settings that
        # did not touch them does not move the instrument.
        self._signal_seen = (float(s.frequency_Hz), float(s.power_dBm))
        self._status = self._snapshot(rf=False, f=self._freq, p=self._power,
                                      cond=0, mod={}, idn="")
        # THE SWEEPS (fly scans, 2026-10-10): one software ramp per knob
        # (softramp.py, copied byte for byte from suite-common). The 8648D
        # has a list/step sweep of its own on the front panel, but nothing a
        # fly scan could follow sample by sample, so the SERVICE walks the
        # knob: one FREQ:CW / POW:AMPL per step, every value sent recorded
        # with its time. The steps run on the ramp's own thread and write the
        # instrument DIRECTLY under _hw_lock (not through the worker's dirty
        # set: the worker waits switch_settle_s after each write and reads
        # five registers back, which would throttle a sweep to a few steps a
        # second and stamp each value late).
        self._sweeps = {
            knob: SoftRamp(getattr(self, f"_step_{knob}"),
                           (lambda a=spec_[0]: getattr(self, a)),
                           limits=(lambda k=knob: self._knob_limits(k)),
                           dt_s=float(self.cfg.hardware.ramp_dt_s),
                           on_done=(lambda rid, why, k=knob: self._sweep_done(k, why)),
                           channel=knob, name=f"hp8648-{knob}-sweep")
            for knob, spec_ in SWEEP_KNOBS.items()}
        self._stream_id = 0

    # ---- limits ----------------------------------------------------------

    def freq_limits(self) -> tuple[float, float]:
        """(lo, hi) in Hz: your envelope, but never wider than the instrument.
        An .ini that says 6 GHz must not advertise 6 GHz in describe -- a scan
        would then ask for a frequency the 8648D refuses, and wait forever for
        an echo that cannot come."""
        lim = self.cfg.limits
        return (max(lim.freq_min_Hz, spec.FREQ_MIN_HZ),
                min(lim.freq_max_Hz, spec.FREQ_MAX_HZ))

    def power_floor(self) -> float:
        """The lowest level commanded: envelope, never below the attenuator."""
        return max(self.cfg.limits.power_min_dBm, spec.POWER_MIN_DBM)

    def power_ceiling(self, freq_Hz: float | None = None) -> float:
        """The highest level the brain will command at `freq_Hz` (default: the
        current setpoint). The tighter of your envelope and the spec."""
        lim = self.cfg.limits
        f = self._freq if freq_Hz is None else freq_Hz
        top = lim.power_max_dBm
        if lim.enforce_spec_ceiling:
            top = min(top, spec.spec_max_dBm(f, self.cfg.hardware.option_1ea))
        return max(top, self.power_floor())

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Open the backend, READ the instrument and adopt its state, start the
        worker. Writes nothing (see the module docstring)."""
        with self._hw_lock:
            self.backend.open()
            self._connected = True
            try:
                rf = bool(self.backend.read_output())
                f = float(self.backend.read_frequency())
                p = float(self.backend.read_power())
                cond = int(self.backend.read_power_condition())
                mod = dict(self.backend.read_modulation())
                notes = list(self.backend.startup_notes())
                idn = self.backend.idn()
            except Exception as exc:
                # Could not read the state: keep the placeholders, mark nothing
                # dirty (so nothing is written), and let the worker keep trying
                # -- its read-back reports hw_error until the bus answers.
                self._emit("error", f"could not read the instrument at connect: {exc}")
                rf = f = p = None
                self._need_adopt = True
                cond, mod, notes, idn = 0, {}, [], ""
        if f is not None:
            with self._lock:
                self._rf, self._freq, self._power = rf, f, p
                self._dirty.clear()            # adopting is not a change to write
            self._rpp_latched = bool(cond & RPP_BIT)
            self._status = self._snapshot(rf=rf, f=f, p=p, cond=cond, mod=mod, idn=idn)
            self._emit("info", f"connected: {idn or 'HP 8648'} -- adopted RF "
                               f"{'ON' if rf else 'off'}, {f / 1e6:.5f} MHz, "
                               f"{p:+.1f} dBm (nothing written)")
            if rf:
                self._emit("warn", "the RF output was already ON at connect; "
                                   "it was left on")
            on = [k for k, v in mod.items() if v]
            if on:
                # Pure CW is this module's job, but switching a modulation off
                # is a change -- report it, leave it.
                self._emit("warn", f"modulation is ON at the instrument ({', '.join(on)}); "
                                   "left as found -- this module assumes a pure CW signal")
            if cond & RPP_BIT:
                self._emit("error", "REVERSE POWER PROTECTION was already tripped at "
                                    "connect. Remove the signal reaching the RF OUTPUT, "
                                    "then switch RF on to re-arm.")
            f_lo, f_hi = self.freq_limits()
            top = self.power_ceiling(f)
            if not f_lo <= f <= f_hi or not self.power_floor() <= p <= top:
                # Adopted as found: the limits guard what WE command. The next
                # setpoint (or an OK in Settings) is clamped as usual.
                self._emit("warn", f"the instrument is outside your envelope "
                                   f"({f / 1e6:g} MHz, {p:+.1f} dBm; ceiling here "
                                   f"{top:+.1f} dBm) -- left as found")
            for note in notes:
                self._emit("warn", note)
        # one synchronous read cycle (nothing is dirty, so nothing is written)
        self._cycle()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hp8648-worker",
                                        daemon=True)
        self._thread.start()

    def shutdown(self, keep_outputs: bool = False) -> None:
        """RF off, disconnect. Safe to call more than once / on a crash.

        keep_outputs=True is a RESTART for a code update (Lukas 2026-10-06):
        disconnect and release the address the same, but leave the RF output
        as it is -- the next start adopts it."""
        # no sweep step may follow the RF off below (and a step waiting for
        # _hw_lock must not get it after the backend is closed)
        for ramp in self._sweeps.values():
            ramp.stop()
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5.0)
        self._thread = None
        if not keep_outputs:
            with self._lock:
                self._rf = False
        was = self._connected
        with self._hw_lock:
            try:
                if self._connected and not keep_outputs:
                    self.backend.set_output(False)
            except Exception as exc:
                self._emit("error", f"RF off at shutdown failed: {exc}")
            finally:
                try:
                    self.backend.close(rf_off=not keep_outputs)
                finally:
                    self._connected = False
        old = self._status
        if keep_outputs:
            self._status = Status(**{**old.__dict__, "connected": False})
        else:
            self._status = Status(**{**old.__dict__, "connected": False,
                                     "rf_on": False, "rf_set": False})
        if was:
            self._emit("info", "disconnected (RF left as it is)"
                       if keep_outputs else "disconnected (RF off)")

    # ---- commands: clamp, remember, wake the worker ----------------------

    def set_rf(self, on: bool) -> None:
        on = bool(on)
        rearm = on and self._status.rpp_tripped
        with self._lock:
            self._rf = on
            self._dirty.add("rf")
        self._wake.set()
        if rearm:
            self._emit("warn", "RF on re-arms the reverse-power protection -- "
                               "make sure the reverse signal has been removed")
        self._emit("info", f"RF {'ON' if on else 'OFF'}")

    def rf_off(self) -> None:
        """RF off. Same as set_rf(False); a name of its own because over the
        wire it is the SAFETY verb a viewer may always send (net/service.py,
        control)."""
        self.set_rf(False)

    def set_frequency(self, hz: float) -> None:
        # a set is a new instruction: it takes the knob over from a sweep
        # (stopped BEFORE any lock: a sweep step may be waiting for _hw_lock)
        if self._sweeps["frequency"].stop():
            self._emit("info", "frequency sweep stopped by a frequency set")
        f_lo, f_hi = self.freq_limits()
        value, clamped = _clamp(float(hz), f_lo, f_hi)
        msgs = []
        with self._lock:
            self._freq = value
            self._dirty.add("freq")
            # The level ceiling steps down with frequency (13 -> 10 dBm above
            # 2500 MHz). A level that was fine at the old frequency may not be
            # at the new one, so it is lowered BEFORE the frequency moves.
            top = self.power_ceiling(value)
            if self._power > top:
                self._power = top
                self._dirty.add("power")
                msgs.append(("warn", f"power lowered to {top:g} dBm: the ceiling at "
                                     f"{value / 1e6:g} MHz is {top:g} dBm"))
        self._wake.set()
        if clamped:
            self._emit("warn", f"frequency clamped to {value:g} Hz "
                               f"(limit {f_lo:g}..{f_hi:g})")
        else:
            self._emit("info", f"frequency = {value / 1e6:.5f} MHz")
        for lvl, m in msgs:
            self._emit(lvl, m)

    def set_power(self, dBm: float) -> None:
        # a set is a new instruction: it takes the knob over from a sweep
        # (stopped BEFORE any lock: a sweep step may be waiting for _hw_lock)
        if self._sweeps["power"].stop():
            self._emit("info", "power sweep stopped by a power set")
        lo = self.power_floor()
        with self._lock:
            top = self.power_ceiling(self._freq)
            value, clamped = _clamp(float(dBm), lo, top)
            self._power = value
            self._dirty.add("power")
        self._wake.set()
        if clamped:
            self._emit("warn", f"power clamped to {value:g} dBm "
                               f"(limit {lo:g}..{top:g} at this frequency)")
        else:
            self._emit("info", f"power = {value:g} dBm")

    # ---- the SWEEPS (fly scans) ------------------------------------------
    #
    # Why: a fly scan (scan-core, `type: fly` axis) records the detectors
    # while a knob moves CONTINUOUSLY and sorts every sample into the pixel of
    # the value the knob had at that moment. A generator jumps to the value it
    # is told, so the service walks it: ramp_frequency / ramp_power start a
    # walk at a set pace, ramp_stop ends it where it is, and an ordinary set
    # of the same knob takes the knob over.
    #
    # What a fly scan bins by is the COMMANDED value (describe: readback
    # measured false), for both knobs. Decided 2026-10-10: FREQ:CW? and
    # POW:AMPL? return the SETTING the box holds (the manual calls POW:AMPL?
    # the "programmed" level), not a measurement of the output -- they would
    # only echo the number just sent, and each query costs a GPIB round trip
    # per step. The 8648 is an old instrument; what the command does NOT
    # capture:
    #   * FREQUENCY: every step is a synthesiser switch, specified < 75 ms
    #     below 1001 MHz and < 100 ms above (spec.switching_time_s). The
    #     output reaches each value up to one switching time AFTER the stamp,
    #     and may blank or glitch while the loop relocks -- most likely where
    #     the synthesiser changes its divider band. VERIFY on the unit: the
    #     real switching time per step and what the output does meanwhile.
    #     At a pace R that lag is a constant offset of about R x t_switch in
    #     the binned frequency; a slow pace, or zig-zag rows (the two
    #     directions shift oppositely), keeps it honest;
    #   * LEVEL: the step attenuator switches at fixed levels as the level
    #     moves (POW:ATT:AUTO ON); if it is mechanical, the level may jump
    #     briefly at each switch point and the relays wear. VERIFY on the unit.
    #     The level resolution is 0.1 dB, so steps smaller than that repeat.
    #
    # The RF output is NEVER switched by a sweep: a step only sends the knob.

    def _knob_limits(self, knob: str) -> tuple[float, float]:
        """The knob's EFFECTIVE envelope, read LIVE (an edited limit applies
        to the next step of a running sweep too) -- the same numbers the
        setters clamp with: the frequency never wider than the 8648D, the
        level never above the ceiling at the CURRENT frequency (spec.py)."""
        if knob == "frequency":
            return self.freq_limits()
        return (self.power_floor(), self.power_ceiling(self._freq))

    def _rate_limits(self, knob: str) -> tuple[float, float]:
        runit = SWEEP_KNOBS[knob][2]
        lim = self.cfg.limits
        return (float(getattr(lim, f"ramp_rate_min_{runit}")),
                float(getattr(lim, f"ramp_rate_max_{runit}")))

    def _path_ceiling(self, f0: float, f1: float) -> float:
        """The LOWEST level ceiling anywhere between two frequencies. The
        ceiling is a staircase (spec.py), and with option 1EA it is not even
        monotonic, so it is evaluated at both ends and just above every band
        edge in between."""
        lo, hi = min(f0, f1), max(f0, f1)
        pts = [lo, hi] + [e * (1.0 + 1e-12) + 1.0
                          for e in spec.band_edges(self.cfg.hardware.option_1ea)
                          if lo <= e < hi]
        return min(self.power_ceiling(f) for f in pts)

    def _step_frequency(self, value: float) -> None:
        """One frequency step, on the sweep's own thread. Quiet (no event
        per step: a sweep is ten steps a second) and without the worker's
        switch_settle_s wait (nothing is read back after it)."""
        value = float(value)
        lowered = None
        with self._hw_lock:                    # the same lock as every bus call
            with self._lock:                   # order: _hw_lock, then _lock
                self._freq = value
                # this write supersedes a frequency write still pending
                self._dirty.discard("freq")
                # Backstop for the level ceiling (ramp() already lowered the
                # level for the whole path; this catches a level raised by
                # someone else DURING the sweep): lower BEFORE the frequency
                # moves, as set_frequency does.
                top = self.power_ceiling(value)
                if self._power > top:
                    self._power = lowered = top
                    self._dirty.discard("power")
            if self._connected:
                if lowered is not None:
                    self.backend.set_power(lowered)
                self.backend.set_frequency(value)
        if lowered is not None:
            self._emit("warn", f"power lowered to {lowered:g} dBm during the frequency "
                               f"sweep: the ceiling at {value / 1e6:g} MHz")

    def _step_power(self, value: float) -> None:
        """One level step, on the sweep's own thread (see _step_frequency)."""
        with self._hw_lock:
            with self._lock:
                # softramp clamped it to the ceiling a moment ago; clamp again
                # here in case a frequency sweep moved the ceiling meanwhile
                value = min(float(value), self.power_ceiling(self._freq))
                self._power = value
                self._dirty.discard("power")
            if self._connected:
                self.backend.set_power(value)

    def ramp(self, knob: str, to: float, rate: float) -> int:
        """Sweep `knob` to `to` at `rate` (wire units: Hz or dBm, per
        second); returns the sweep's number. The target is clamped to the
        knob's effective limits and the pace to the configured sweep paces,
        both with a warning -- like every setter here. A sweep of the same
        knob already running is taken over from wherever it got to."""
        if knob not in SWEEP_KNOBS:
            raise ValueError(f"cannot sweep {knob!r}; one of {sorted(SWEEP_KNOBS)}")
        unit, runit = SWEEP_KNOBS[knob][1], SWEEP_KNOBS[knob][2].replace("_per_s", "/s")
        r = abs(float(rate))
        if not r > 0:                                    # also catches NaN
            raise ValueError("rate must be > 0")
        target = float(to)
        if not math.isfinite(target):
            raise ValueError(f"target must be a finite number, got {to!r}")
        sw = self._sweeps[knob]
        sw.stop()                    # a running sweep of this knob ends HERE first
        lo, hi = self._knob_limits(knob)
        rlo, rhi = self._rate_limits(knob)
        r, rclamped = _clamp(r, rlo, rhi)
        value, clamped = _clamp(target, lo, hi)
        if knob == "frequency":
            # The level must fit the ceiling over the WHOLE path (13 -> 10 dBm
            # above 2500 MHz): lower it once, now, rather than in the middle
            # of the sweep -- a fly scan over frequency wants ONE level.
            top = self._path_ceiling(self._freq, value)
            if self._power > top:
                with self._hw_lock:
                    with self._lock:
                        self._power = top
                        self._dirty.discard("power")
                    if self._connected:
                        self.backend.set_power(top)
                self._emit("warn", f"power lowered to {top:g} dBm before the sweep: "
                                   f"the ceiling on the way to {value / 1e6:g} MHz")
        sw.dt_s = max(0.001, float(self.cfg.hardware.ramp_dt_s))   # live config
        rid = sw.start(value, r)
        if clamped or rclamped:
            self._emit("warn", f"{knob} sweep clamped to {value:g} {unit} at {r:g} {runit} "
                               f"(limits {lo:g}..{hi:g} {unit}, {rlo:g}..{rhi:g} {runit})")
        self._emit("info", f"{knob} sweep -> {value:g} {unit} at {r:g} {runit}")
        return rid

    def ramp_frequency(self, hz: float, rate_Hz_per_s: float) -> int:
        return self.ramp("frequency", hz, rate_Hz_per_s)

    def ramp_power(self, dBm: float, rate_dB_per_s: float) -> int:
        return self.ramp("power", dBm, rate_dB_per_s)

    def ramp_stop(self, knob: str | None = None) -> bool:
        """End a sweep where it is -- of one knob, or of every knob (None).
        True if one was running. A stop: allowed for a viewer too."""
        if knob is not None and knob not in SWEEP_KNOBS:
            raise ValueError(f"no sweep {knob!r}; one of {sorted(SWEEP_KNOBS)}")
        was = False
        for k in ([knob] if knob else list(SWEEP_KNOBS)):
            if self._sweeps[k].stop():
                was = True
                self._emit("info", f"{k} sweep stopped at "
                                   f"{getattr(self, SWEEP_KNOBS[k][0]):g} {SWEEP_KNOBS[k][1]}")
        return was

    def _sweep_done(self, knob: str, reason: str) -> None:
        unit = SWEEP_KNOBS[knob][1]
        if reason == "done":
            self._emit("info", f"{knob} sweep done at "
                               f"{getattr(self, SWEEP_KNOBS[knob][0]):g} {unit}")
        elif reason.startswith("error"):
            self._emit("error", f"{knob} sweep ended: {reason}")

    def sweep_status(self) -> dict:
        """The sweeps' live values as flat wire keys (in memory, no hardware).
        `<knob>_ramp_id` is the newest sweep of that knob started; a caller
        whose sweep has number n waits for `<knob>_ramp_id >= n` and
        `<knob>_ramping` false -- numbered so a "not ramping" from before the
        start can never pass for the end (docs/DEVELOPER_NOTES.md gotcha #17)."""
        out = {"ramping": False}
        for knob, (_attr, unit, runit) in SWEEP_KNOBS.items():
            r = self._sweeps[knob].status()
            out[f"{knob}_ramping"] = r["ramping"]
            out[f"{knob}_ramp_id"] = r["ramp_id"]
            out[f"{knob}_ramp_target_{unit}"] = r["ramp_target"]
            out[f"{knob}_ramp_rate_{runit}"] = r["ramp_rate"]
            out["ramping"] = out["ramping"] or r["ramping"]
        return out

    # The stream verbs: ONE stream (group "ramp") with one channel per knob --
    # every value each sweep sent, with the time the backend had it. Each knob
    # keeps its own time stamps (`t_ch`, guide 6b "Streams"): the knobs are
    # walked by separate threads. A knob at rest still contributes its value
    # (softramp records the rest value), so a fly row's lead-in has a value.
    # delay_s is 0 for both: the stamp is when the GPIB write returned; the
    # synthesiser's switching lag after it is NOT corrected (see "the SWEEPS").

    def stream_start(self) -> int:
        for ramp in self._sweeps.values():
            ramp.stream_start()
        self._stream_id += 1
        return self._stream_id

    def stream_read(self) -> dict:
        return self._merge({k: r.stream_read() for k, r in self._sweeps.items()})

    def stream_stop(self) -> dict:
        return self._merge({k: r.stream_stop() for k, r in self._sweeps.items()})

    def _merge(self, chunks: dict) -> dict:
        first = next(iter(chunks.values()))
        return {"id": self._stream_id, "t": first["t"],
                "t_ch": {k: c["t"] for k, c in chunks.items()},
                "values": {k: c["values"][k] for k, c in chunks.items()},
                "delay_s": {k: 0.0 for k in chunks},
                "overflow": any(c["overflow"] for c in chunks.values()),
                "now": time.time()}

    # ---- status ----------------------------------------------------------

    def status(self) -> Status:
        """The latest snapshot. Never touches the hardware.

        The sweeps' fields are laid over it LIVE (in memory, no hardware): the
        snapshot is only rebuilt every poll_s, and a fly scan waiting for the
        end of a sweep should not wait a poll period for nothing. A COPY is
        returned (dataclasses.replace): the worker's snapshot is never edited
        (gotcha #1)."""
        live = self.sweep_status()
        st = self._status
        if st.sweep == live:
            return st
        return replace(st, sweep=live)

    def wait_idle(self, timeout: float = 2.0) -> bool:
        """Block until every pending change has been written AND read back.
        For scripts and tests; the wire uses the echo instead."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                idle = not self._dirty and not self._busy
            if idle:
                return True
            time.sleep(0.01)
        return False

    _busy = False

    # ---- settings --------------------------------------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Called after set_config (or the Settings dialog) edited self.cfg in
        place.

        * If the `signal` defaults were CHANGED, that is an explicit request:
          send them (clamped, like any setpoint).
        * Otherwise only re-clamp: a setpoint that no longer fits the new
          limits is moved, one that still fits is left alone -- so pressing
          OK in Settings does not rewrite (or move) the instrument.
        """
        sig = (float(self.cfg.signal.frequency_Hz), float(self.cfg.signal.power_dBm))
        if sig != self._signal_seen:
            self._signal_seen = sig
            self.set_frequency(sig[0])
            self.set_power(sig[1])
            return
        f_lo, f_hi = self.freq_limits()
        with self._lock:
            f, p = self._freq, self._power
        f_new = _clamp(f, f_lo, f_hi)[0]
        if f_new != f:
            self.set_frequency(f_new)          # also lowers a level that no longer fits
        with self._lock:
            p = self._power
        p_new = _clamp(p, self.power_floor(), self.power_ceiling(f_new))[0]
        if p_new != p:
            self.set_power(p_new)

    # ---- the worker ------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(max(0.02, float(self.cfg.hardware.poll_s)))
            self._wake.clear()
            if self._stop.is_set():
                break
            self._cycle()

    def _cycle(self) -> None:
        """Write what is pending, read everything back, publish a snapshot."""
        with self._lock:
            dirty, self._dirty = self._dirty, set()
            rf, f, p = self._rf, self._freq, self._power
            self._busy = bool(dirty)
        prev = self._status
        cond, mod, idn = 0, prev.modulation, prev.idn
        rf_rb, f_rb, p_rb = prev.rf_on, prev.frequency_Hz, prev.power_dBm
        try:
            with self._hw_lock:
                if not self._connected:
                    with self._lock:
                        self._busy = False
                    return
                # Re-read the setpoints NOW, holding the bus: a sweep step
                # (which also holds _hw_lock while it writes) may have moved
                # the frequency or level since the top of this cycle, and
                # writing the older value would step the output back.
                with self._lock:
                    rf, f, p = self._rf, self._freq, self._power
                b = self.backend
                if dirty:
                    self._write(b, dirty, rf, f, p, prev)
                    for err in b.drain_errors():
                        self._emit("warn", f"instrument error: {err}")
                rf_rb = bool(b.read_output())
                f_rb = float(b.read_frequency())
                p_rb = float(b.read_power())
                cond = int(b.read_power_condition())
                self._cycle_n += 1
                if not mod or self._cycle_n % self.MOD_CHECK_EVERY == 0:
                    mod = b.read_modulation()
                idn = b.idn()
            hw_error = ""
            self._last_hw_error = ""
            if self._need_adopt:
                # start() could not read the box; this is the first good read.
                # Adopt it now -- unless the user has already asked for
                # something, in which case that request wins.
                with self._lock:
                    if not dirty and not self._dirty:
                        self._rf, self._freq, self._power = rf_rb, f_rb, p_rb
                        rf, f, p = rf_rb, f_rb, p_rb
                self._need_adopt = False
                self._emit("info", f"instrument answered: adopted RF "
                                   f"{'ON' if rf_rb else 'off'}, {f_rb / 1e6:.5f} MHz, "
                                   f"{p_rb:+.1f} dBm")
        except Exception as exc:                       # never let the worker die
            hw_error = str(exc) or type(exc).__name__
            # A write that failed half-way must not be forgotten: put what was
            # pending back, so the next cycle tries again. Otherwise the GUI
            # would show a setpoint the instrument never received, and a scan
            # would wait out its whole timeout for an echo nobody retries.
            # (Anything the user changed meanwhile is already in _dirty too.)
            if dirty:
                with self._lock:
                    self._dirty |= dirty
            if hw_error != self._last_hw_error:
                self._emit("error", f"hardware read failed: {hw_error}")
            self._last_hw_error = hw_error
            cond = (RPP_BIT if prev.rpp_tripped else 0) | \
                   (UNSPECIFIED_BIT if prev.level_unspecified else 0)

        # Reverse-power protection: the box has already switched its RF off.
        # Make "off" the desired state too, or the next RF write would re-arm
        # it and put the level straight back onto whatever is feeding power in.
        tripped = bool(cond & RPP_BIT)
        if tripped and not self._rpp_latched:
            with self._lock:
                self._rf = False
                rf = False
            # and no sweep walks on as if nothing happened (no lock is held
            # here, so stop() can wait for a step in progress)
            if self.ramp_stop():
                self._emit("warn", "sweeps stopped: reverse power protection tripped")
            self._emit("error", "REVERSE POWER PROTECTION tripped: the instrument "
                                "switched its RF output off. Remove the signal "
                                "reaching the RF OUTPUT, then switch RF on to re-arm.")
        self._rpp_latched = tripped
        if mod and not all(not v for v in mod.values()) and prev.modulation_off:
            self._emit("warn", "a modulation is ON at the instrument "
                               f"({', '.join(k for k, v in mod.items() if v)}); "
                               "this module drives a pure CW signal")

        with self._lock:
            rf_set, f_set, p_set = self._rf, self._freq, self._power
            self._busy = False
        self._status = self._snapshot(rf=rf_rb, f=f_rb, p=p_rb, cond=cond, mod=mod,
                                      idn=idn, hw_error=hw_error,
                                      rf_set=rf_set, f_set=f_set, p_set=p_set)

    def _write(self, b, dirty, rf, f, p, prev) -> None:
        """Apply pending changes in the SAFE order.

        * RF off goes first (nothing below can then reach the sample),
        * a LOWER level before the frequency moves, a HIGHER one after -- so
          the output never sits above the ceiling of either frequency,
        * RF on goes last, when frequency and level are already right.
        """
        if "rf" in dirty and not rf:
            b.set_output(False)
        lowering = "power" in dirty and p <= prev.power_dBm
        if lowering:
            b.set_power(p)
        if "freq" in dirty:
            b.set_frequency(f)
        if "power" in dirty and not lowering:
            b.set_power(p)
        if dirty & {"freq", "power"}:
            # give the synthesiser its switching time before we read back,
            # so the echo means "arrived", not just "accepted"
            self._stop.wait(max(0.0, float(self.cfg.hardware.switch_settle_s)))
        if "rf" in dirty and rf:
            b.set_output(True)

    def _snapshot(self, *, rf, f, p, cond, mod, idn, hw_error="",
                  rf_set=None, f_set=None, p_set=None) -> Status:
        f_set = self._freq if f_set is None else f_set
        return Status(
            rf_on=bool(rf), frequency_Hz=float(f), power_dBm=float(p),
            rf_set=bool(self._rf if rf_set is None else rf_set),
            frequency_set_Hz=float(f_set),
            power_set_dBm=float(self._power if p_set is None else p_set),
            power_ceiling_dBm=float(self.power_ceiling(f_set)),
            spec_max_dBm=float(spec.spec_max_dBm(f_set, self.cfg.hardware.option_1ea)),
            rpp_tripped=bool(cond & RPP_BIT),
            level_unspecified=bool(cond & UNSPECIFIED_BIT),
            modulation_off=not any(mod.values()) if mod else True,
            connected=self._connected, idn=idn, hw_error=hw_error,
            modulation=dict(mod or {}),
            # the sweeps as they are NOW; status() replaces them with fresher
            # values between two cycles (a copy, never an edit). Built in the
            # snapshot too so that, with nothing sweeping, status() hands out
            # the snapshot itself. (None during __init__: no sweeps yet.)
            sweep=self.sweep_status() if hasattr(self, "_sweeps") else {},
        )

    def _emit(self, level: str, msg: str) -> None:
        try:
            self._on_event(level, msg)
        except Exception:
            pass
