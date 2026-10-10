"""The Generator: the small "brain" between the wire and the backend.

The SMB100A needs no control loop, so this is far simpler than clMag's
Controller -- no PID, no state machine. Its whole job is:

  * hold the DESIRED signal (frequency, power, phase, RF on/off),
  * CLAMP every request to the configured safety limits (and announce a clamp
    as an event, so nothing silently drives the sample too hard),
  * push the accepted value to whichever backend is wired in (sim or real),
  * report a status() snapshot the service/GUI/coordinator can read,
  * SWEEP a knob (frequency, power or phase) continuously at a set pace, for
    fly scans (2026-10-10; see "the SWEEPS" below).

ADOPT ON START (Lukas's rule, 2026-09-27: "all modules should read the
instrument state on startup, not to change anything"). start() only READS the
generator -- RF on/off, frequency, level, phase -- and takes those as the
desired signal. Nothing is written at start, so a generator that is already
feeding a running experiment keeps doing exactly that when the service is
(re)started. The [signal] group of the config is therefore NOT a power-on
state any more: it is a set of defaults that is applied only when the user
changes it explicitly (Settings dialog / set_config), see apply_config().

It exposes the same shape clMag's Controller does where it matters -- start(),
shutdown(), status(), get_config()/apply_config(), and an `_on_event` hook the
service replaces to forward events over the wire -- so the networking layer is a
near-copy of clMag's.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field

from .backends.base import RFSource
from .config import Config, Signal
from .softramp import SoftRamp

#: The knobs a sweep can walk, and how each one is named on the wire:
#: knob -> (brain attribute, wire unit, rate unit on the wire, limit field
#: prefix in config.Limits). The pace limits are config.Limits
#: ramp_rate_min_<rate unit> / ramp_rate_max_<rate unit>.
SWEEP_KNOBS = {
    "frequency": ("_freq", "Hz", "Hz_per_s", "freq"),
    "power": ("_power", "dBm", "dB_per_s", "power"),
    "phase": ("_phase", "deg", "deg_per_s", "phase"),
}


@dataclass
class Status:
    """One snapshot of the generator, for status() and the wire."""

    rf_on: bool
    power_dBm: float
    frequency_Hz: float
    phase_deg: float
    connected: bool
    idn: str = ""
    # The SWEEPS, flat wire keys (see Generator.sweep_status): `ramping` = any
    # knob sweeping; per knob `<knob>_ramping`, `<knob>_ramp_id`, and the
    # target and pace in wire units (e.g. frequency_ramp_target_Hz).
    sweep: dict = field(default_factory=dict)


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


class Generator:
    def __init__(self, backend: RFSource, cfg: Config | None = None):
        self.backend = backend
        self.cfg = cfg or Config()
        # Placeholders until start() adopts the instrument's own values: they
        # are what status() reports while not connected, nothing more.
        s = self.cfg.signal
        self._freq = float(s.frequency_Hz)
        self._power = float(s.power_dBm)
        self._phase = float(s.phase_deg)
        self._rf_on = bool(s.rf_on)
        self._connected = False
        # The [signal] group as it was last seen, so apply_config() can tell
        # which default the user actually CHANGED (only those get applied).
        self._signal_seen = asdict(s)
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None
        # ONE lock around every backend call. Three threads talk to the box:
        # the service's commander (setters), its publisher (status() reads the
        # generator back) and, while a sweep runs, the sweep's own thread. A
        # VISA session must not interleave a write with another thread's
        # query, so each backend call takes this lock (RLock: a setter that
        # calls another setter must not deadlock itself).
        self._io = threading.RLock()
        # THE SWEEPS (fly scans, 2026-10-10): one software ramp per knob
        # (softramp.py, copied byte for byte from suite-common). The SMB100A
        # has list/sweep modes of its own, but none a fly scan could follow
        # sample by sample, so the SERVICE walks the knob: one FREQ / POW /
        # PHAS per step, and every value sent is recorded with its time.
        self._sweeps = {
            knob: SoftRamp(self._sweep_setter(knob),
                           (lambda a=spec[0]: getattr(self, a)),
                           limits=(lambda k=knob: self._knob_limits(k)),
                           dt_s=float(self.cfg.hardware.ramp_dt_s),
                           on_done=(lambda rid, why, k=knob: self._sweep_done(k, why)),
                           channel=knob, name=f"smb-{knob}-sweep")
            for knob, spec in SWEEP_KNOBS.items()}
        self._stream_id = 0

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Open the backend and ADOPT what the generator is doing right now.

        Queries only: the RF output is NOT switched off, and no frequency,
        level or phase is written. A value outside our configured limits is
        left alone on the instrument (we only warn); the limits apply to the
        next thing the user sets.
        """
        self.backend.open()
        self._connected = True
        readers = (("_rf_on", self.backend.read_output, bool),
                   ("_freq", self.backend.read_frequency, float),
                   ("_power", self.backend.read_power, float),
                   ("_phase", self.backend.read_phase, float))
        for attr, read, cast in readers:
            try:
                setattr(self, attr, cast(read()))
            except Exception as exc:           # keep the placeholder, say so
                self._emit("warn", f"could not read {attr.lstrip('_')} at start "
                                   f"({exc}); showing the config default")
        self._signal_seen = asdict(self.cfg.signal)
        self._emit("info", f"connected: {self.backend.idn() or 'SMB100A'}")
        self._emit("info", f"adopted from the instrument: RF "
                           f"{'ON' if self._rf_on else 'off'}, {self._freq:g} Hz, "
                           f"{self._power:g} dBm, {self._phase:g} deg (nothing written)")
        lim = self.cfg.limits
        for name, value, lo, hi, unit in (
                ("frequency", self._freq, lim.freq_min_Hz, lim.freq_max_Hz, "Hz"),
                ("power", self._power, lim.power_min_dBm, lim.power_max_dBm, "dBm"),
                ("phase", self._phase, lim.phase_min_deg, lim.phase_max_deg, "deg")):
            if not lo <= value <= hi:
                self._emit("warn", f"instrument {name} {value:g} {unit} is outside the "
                                   f"limits {lo:g}..{hi:g}; left as is (not clamped)")

    def shutdown(self, keep_outputs: bool = False) -> None:
        """RF off, disconnect. Safe to call more than once / on a crash.

        keep_outputs=True is a RESTART for a code update (Lukas 2026-10-06):
        disconnect and release the address the same, but leave the RF output
        as it is -- the next start adopts it."""
        # no sweep step may follow the RF off below
        for ramp in self._sweeps.values():
            ramp.stop()
        try:
            if self._connected and not keep_outputs:
                self.backend.set_output(False)
                self._rf_on = False
        finally:
            try:
                self.backend.close(rf_off=not keep_outputs)
            finally:
                self._connected = False
                self._emit("info", "disconnected (RF left as it is)"
                           if keep_outputs else "disconnected")

    # ---- commands (each clamps, then pushes) -----------------------------

    def set_rf(self, on: bool) -> None:
        # (a sweep never calls this: sweeping a knob never switches RF)
        with self._io:
            self._rf_on = bool(on)
            if self._connected:
                self.backend.set_output(self._rf_on)
        self._emit("info", f"RF {'ON' if self._rf_on else 'OFF'}")

    def rf_off(self) -> None:
        """RF off. Same as set_rf(False); a name of its own because over the
        wire it is the SAFETY verb a viewer may always send (net/service.py,
        control)."""
        self.set_rf(False)

    def set_power(self, dBm: float) -> None:
        # a set is a new instruction: it takes the knob over from a sweep
        # (stopped BEFORE the lock below, which a sweep step may be waiting for)
        if self._sweeps["power"].stop():
            self._emit("info", "power sweep stopped by a power set")
        lim = self.cfg.limits
        value, clamped = _clamp(float(dBm), lim.power_min_dBm, lim.power_max_dBm)
        with self._io:
            self._power = value
            if self._connected:
                self.backend.set_power(value)
        if clamped:
            self._emit("warn", f"power clamped to {value:g} dBm "
                               f"(limit {lim.power_min_dBm:g}..{lim.power_max_dBm:g})")
        else:
            self._emit("info", f"power = {value:g} dBm")

    def set_frequency(self, hz: float) -> None:
        # a set is a new instruction: it takes the knob over from a sweep
        # (stopped BEFORE the lock below, which a sweep step may be waiting for)
        if self._sweeps["frequency"].stop():
            self._emit("info", "frequency sweep stopped by a frequency set")
        lim = self.cfg.limits
        value, clamped = _clamp(float(hz), lim.freq_min_Hz, lim.freq_max_Hz)
        with self._io:
            self._freq = value
            if self._connected:
                self.backend.set_frequency(value)
        if clamped:
            self._emit("warn", f"frequency clamped to {value:g} Hz "
                               f"(limit {lim.freq_min_Hz:g}..{lim.freq_max_Hz:g})")
        else:
            self._emit("info", f"frequency = {value:g} Hz")

    def set_phase(self, deg: float) -> None:
        # a set is a new instruction: it takes the knob over from a sweep
        # (stopped BEFORE the lock below, which a sweep step may be waiting for)
        if self._sweeps["phase"].stop():
            self._emit("info", "phase sweep stopped by a phase set")
        lim = self.cfg.limits
        value, clamped = _clamp(float(deg), lim.phase_min_deg, lim.phase_max_deg)
        with self._io:
            self._phase = value
            if self._connected:
                self.backend.set_phase(value)
        if clamped:
            self._emit("warn", f"phase clamped to {value:g} deg "
                               f"(limit {lim.phase_min_deg:g}..{lim.phase_max_deg:g})")
        else:
            self._emit("info", f"phase = {value:g} deg")

    # ---- the SWEEPS (fly scans) ------------------------------------------
    #
    # Why: a fly scan (scan-core, `type: fly` axis) records the detectors
    # while a knob moves CONTINUOUSLY and sorts every sample into the pixel of
    # the value the knob had at that moment. A generator jumps to the value it
    # is told, so the service walks it: ramp_frequency / ramp_power /
    # ramp_phase start a walk at a set pace, ramp_stop ends it where it is,
    # and an ordinary set of the same knob takes the knob over.
    #
    # What a fly scan bins by is the COMMANDED value (describe: readback
    # measured false). Decided 2026-10-10: FREQ? / POW? / PHAS? return the
    # SETTING the box holds, not a measurement of the output -- they would
    # only echo the number just sent, and each query costs a GPIB round trip
    # per step (VERIFY on the unit how long; a few ms expected). A synthesiser
    # that has taken FREQ is at that frequency within its setting time (a few
    # ms, datasheet), well inside one step, so the command IS the value.
    #
    # The RF output is NEVER switched by a sweep: a step only sends the knob.

    def _knob_limits(self, knob: str) -> tuple[float, float]:
        """The knob's safety envelope from config.Limits, read LIVE (an edited
        limit applies to the next step of a running sweep, too)."""
        _attr, unit, _runit, pre = SWEEP_KNOBS[knob]
        lim = self.cfg.limits
        return (float(getattr(lim, f"{pre}_min_{unit}")),
                float(getattr(lim, f"{pre}_max_{unit}")))

    def _rate_limits(self, knob: str) -> tuple[float, float]:
        runit = SWEEP_KNOBS[knob][2]
        lim = self.cfg.limits
        return (float(getattr(lim, f"ramp_rate_min_{runit}")),
                float(getattr(lim, f"ramp_rate_max_{runit}")))

    def _sweep_setter(self, knob: str):
        attr = SWEEP_KNOBS[knob][0]
        set_fn = f"set_{knob}"

        def step(value: float) -> None:
            """One step, on the sweep's own thread. Quiet (no event per step:
            a sweep is tens of steps a second) and without the backend's
            settle pause (nothing is read back after it)."""
            with self._io:
                setattr(self, attr, float(value))
                if self._connected:
                    getattr(self.backend, set_fn)(float(value), settle=False)
        return step

    def ramp(self, knob: str, to: float, rate: float) -> int:
        """Sweep `knob` to `to` at `rate` (wire units: Hz, dBm, deg and per
        second); returns the sweep's number. The target is clamped to the
        knob's limits and the pace to the configured sweep paces, both with a
        warning -- like every setter here. A sweep of the same knob already
        running is taken over from wherever it got to."""
        if knob not in SWEEP_KNOBS:
            raise ValueError(f"cannot sweep {knob!r}; one of {sorted(SWEEP_KNOBS)}")
        unit, runit = SWEEP_KNOBS[knob][1], SWEEP_KNOBS[knob][2].replace("_per_s", "/s")
        r = abs(float(rate))
        if not r > 0:                                    # also catches NaN
            raise ValueError("rate must be > 0")
        target = float(to)
        if not math.isfinite(target):
            raise ValueError(f"target must be a finite number, got {to!r}")
        lo, hi = self._knob_limits(knob)
        rlo, rhi = self._rate_limits(knob)
        r, rclamped = _clamp(r, rlo, rhi)
        value, clamped = _clamp(target, lo, hi)
        sw = self._sweeps[knob]
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

    def ramp_phase(self, deg: float, rate_deg_per_s: float) -> int:
        return self.ramp("phase", deg, rate_deg_per_s)

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
        for knob, (_attr, unit, runit, _pre) in SWEEP_KNOBS.items():
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
        """A snapshot. When connected we read back from the instrument (the
        source of truth); otherwise we report the desired values."""
        sweep = self.sweep_status()          # in memory: no hardware read
        if self._connected:
            try:
                with self._io:
                    return Status(
                        rf_on=self.backend.read_output(),
                        power_dBm=self.backend.read_power(),
                        frequency_Hz=self.backend.read_frequency(),
                        phase_deg=self.backend.read_phase(),
                        connected=True,
                        idn=self.backend.idn(),
                        sweep=sweep,
                    )
            except Exception as exc:                 # never let status() throw
                self._emit("error", f"status read failed: {exc}")
        return Status(self._rf_on, self._power, self._freq, self._phase,
                      self._connected, "", sweep)

    # ---- settings (Settings dialog / wire use these) ---------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Called after set_config / the Settings dialog edited self.cfg in place.

        Two jobs, both triggered by an explicit user action:
          * a [signal] default the user CHANGED (compared with the last copy we
            saw) is applied now -- frequency, power and phase. `rf_on` is never
            switched from here: RF is switched only by set_rf, on purpose, so
            saving the Settings dialog can never key the output.
          * a value that no longer fits (possibly new) limits is re-clamped.
        Anything unchanged and inside the limits is NOT re-sent, so e.g. a
        theme change never writes to the instrument.
        """
        new = asdict(self.cfg.signal)
        old = self._signal_seen
        self._signal_seen = new
        lim = self.cfg.limits
        for key, current, setter, lo, hi in (
                ("frequency_Hz", self._freq, self.set_frequency,
                 lim.freq_min_Hz, lim.freq_max_Hz),
                ("power_dBm", self._power, self.set_power,
                 lim.power_min_dBm, lim.power_max_dBm),
                ("phase_deg", self._phase, self.set_phase,
                 lim.phase_min_deg, lim.phase_max_deg)):
            if new.get(key) != old.get(key):
                setter(float(new[key]))          # the user changed this default
            elif not lo <= current <= hi:
                setter(current)                  # re-clamp to the new limits

    # ---- internals -------------------------------------------------------

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
