"""The Synthesizer: the small "brain" between the wire and the backend.

A two-channel RF synthesizer needs no control loop, but it is not quite
"set and forget" either: after every frequency change the PLL has to re-lock,
the output can fail to LEVEL at the requested power, and an external reference
that is missing unlocks everything. So the brain does four things:

  * holds the DESIRED state of each channel (RF on/off, frequency, power,
    phase) and of the shared reference,
  * CLAMPS every request to the configured safety limits (and says so as a
    warn event, so nothing silently drives the sample too hard),
  * runs ONE worker thread that owns the hardware: it pushes whatever changed
    to the backend and reads back lock / leveling / temperature,
  * publishes a status SNAPSHOT that the worker rebuilds as one new dict.

Why a worker thread, and why setters never touch the snapshot (gotcha #1):
the serial port is one wire shared by both channels, so exactly one thread
must talk to it; and if a setter edited the snapshot while the worker was
building the next one, the edit could land on the object about to be thrown
away. So setters only change `_want` (and bump a generation counter), and the
worker copies the result into each fresh snapshot. status() never touches
hardware -- it just returns the latest snapshot.

How a caller knows a setting has ARRIVED (the settle rule describe declares):
the snapshot's per-channel `a_frequency_Hz` etc. are the values the worker has
actually PUSHED, and `a_settled` is true only when the worker has pushed the
latest request for that channel AND, while its output is ON, its PLL
reports lock. (With the output OFF nothing is radiated, so a pushed request
counts as settled: switching RF off must never hang a scan on a missing
reference, and the lock is waited for when the output is switched on.) A scan therefore waits for "the
service echoes my value" and then "settled" -- the adopt-then-flag rule --
and never measures on an unlocked or not-yet-programmed synthesizer.

START-UP IS READ-ONLY (Lukas, 2026-09-27: "all modules should read the
instrument state on startup, not to change anything"). start() opens the
backend, READS what each channel is doing (RF on/off, frequency, power, PLL)
and which reference is selected, and ADOPTS that as both the desired and the
applied state -- so nothing differs and the worker sends nothing. If output A
was radiating 2.45 GHz before the service started, it still is, and the
status, the GUI and describe say so. The config's channel / reference values
are no longer pushed at start; they are overwritten with what was read (so
get_config and a saved .ini tell the truth), and a value is sent to the
instrument only when someone sets it (a setter, or set_config changing it).

SWEEPS (fly scans, 2026-10-10). Frequency, power and phase of EACH channel
can also be walked continuously at a set pace (ramp_frequency / ramp_power /
ramp_phase, see "the SWEEPS" below). A sweep step is the one exception to
"only the worker talks to the serial port": it writes from its own thread,
so every backend call -- the worker's and the sweep's -- now goes through ONE
hardware lock, `_io`.
"""

from __future__ import annotations

import math
import threading
import time

from .backends.base import DualSynth
from .config import Config, REFERENCE_SOURCES
from .softramp import SoftRamp

#: channel names on the wire and in the GUI, and the instrument's numbering
CHANNELS = ("a", "b")
_INDEX = {"a": 0, "b": 1}

_KNOBS = ("frequency_Hz", "power_dBm", "phase_deg")
_NAN = float("nan")

#: The knobs a sweep can walk, PER CHANNEL: knob -> (key in `_want` and in the
#: status, wire unit, rate unit on the wire, limit field prefix in
#: config.Limits). The pace limits are config.Limits
#: ramp_rate_min_<rate unit> / ramp_rate_max_<rate unit>.
SWEEP_KNOBS = {
    "frequency": ("frequency_Hz", "Hz", "Hz_per_s", "freq"),
    "power": ("power_dBm", "dBm", "dB_per_s", "power"),
    "phase": ("phase_deg", "deg", "deg_per_s", "phase"),
}

#: Every sweep by its name on the wire: "<channel>_<knob>", e.g. "a_frequency"
#: -- the same id as the describe control it sweeps (windfreak.a_frequency),
#: so a ramp block, its status keys and its stream channel all share it.
SWEEPS = tuple(f"{ch}_{knob}" for ch in CHANNELS for knob in SWEEP_KNOBS)


def parse_channel(ch) -> str:
    """'a' / 'A' / 0 / '0' -> 'a';  'b' / 'B' / 1 / '1' -> 'b'."""
    key = str(ch).strip().lower()
    key = {"0": "a", "1": "b"}.get(key, key)
    if key not in _INDEX:
        raise ValueError(f"unknown channel {ch!r} (use 'a' or 'b')")
    return key


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


class Synthesizer:
    def __init__(self, backend: DualSynth, cfg: Config | None = None):
        self.backend = backend
        self.cfg = cfg or Config()
        self._lock = threading.RLock()
        # DESIRED state, per channel. Before start() these are the config
        # values (shown while disconnected); start() replaces them with what
        # the instrument is actually doing (read-only start rule).
        self._want = {}
        for ch in CHANNELS:
            c = self.cfg.channel(ch)
            self._want[ch] = {"rf_on": False,
                              "frequency_Hz": float(c.frequency_Hz),
                              "power_dBm": float(c.power_dBm),
                              "phase_deg": float(c.phase_deg)}
        self._clamp_all(emit=False)
        self._want_ref = (self.cfg.reference.source, float(self.cfg.reference.ext_MHz))
        # generation counters: bumped by every setter, copied by the worker
        # once it has pushed that request -> "has my command been applied?"
        self._gen = {"a": 0, "b": 0, "ref": 0}
        self._applied_gen = {"a": -1, "b": -1, "ref": -1}
        self._applied = {"a": None, "b": None, "ref": None}   # what the hardware holds
        self._connected = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._unlocked_polls = {"a": 0, "b": 0}
        self._lock_warned = {"a": False, "b": False}
        self._hot_warned = False
        self._last_error = ""
        # set_rf(False) must really send "off" even when the snapshot already
        # says off: a channel read as muted-but-amplifier-on (or unreadable)
        # may count as "off" in the status, and "All RF off" is the safe action.
        self._force_off = {"a": False, "b": False}
        # Is each channel's PLL powered? READ at start, then tracked from what
        # the worker sends (RF on powers it; RF off powers it down only in the
        # "full quiet" mode). Not derived from config: the instrument may have
        # been left in either state.
        self._pll_on_hw = {"a": False, "b": False}
        # The PLL grid is written only when the user CHANGES it (set_config),
        # never at start. `_spacing_seen` is the config value last seen.
        self._spacing_seen = float(self.cfg.hardware.channel_spacing_Hz)
        self._want_spacing = None           # None = nothing to send
        # the channel config groups as last seen, so apply_config can tell
        # "the user changed this in Settings" from "the dialog sent it back"
        self._seen = self._channel_cfg()
        # The backend was BUILT with this flag (it decides what "RF off" sends),
        # so the brain must keep the value it was built with. Reading it live
        # from cfg would let a set_config change the snapshot's idea of the PLL
        # while the hardware still does the old thing. The hardware group
        # therefore takes effect at the next service start.
        self._pll_off_when_off = bool(self.cfg.hardware.pll_off_when_rf_off)
        # ONE lock around every backend call (the serial port). Until the
        # sweeps there was one thread on the port (the worker); a sweep step
        # now writes from its own thread, and two threads must never
        # interleave bytes on one serial line (a write in the middle of the
        # worker's query would garble the reply). RLock: harmless if a holder
        # re-enters. Lock order, to stay deadlock-free: `_io` first, then
        # `_lock` -- never the other way round.
        self._io = threading.RLock()
        # THE SWEEPS (fly scans, 2026-10-10): one software ramp per channel
        # and knob (softramp.py, copied byte for byte from suite-common).
        self._sweeps = {}
        for ch in CHANNELS:
            for knob, spec in SWEEP_KNOBS.items():
                name = f"{ch}_{knob}"
                self._sweeps[name] = SoftRamp(
                    self._sweep_setter(ch, knob),
                    (lambda c=ch, k=spec[0]: self._want[c][k]),
                    limits=(lambda k=knob: self._knob_limits(k)),
                    dt_s=float(self.cfg.hardware.ramp_dt_s),
                    on_done=(lambda rid, why, n=name: self._sweep_done(n, why)),
                    channel=name, name=f"windfreak-{name}-sweep")
        self._stream_id = 0
        self._snapshot = self._build_snapshot({}, "", float("nan"))
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Open the backend, READ the instrument's state and adopt it, start
        the worker. Nothing is written to the instrument (read-only start)."""
        if self._thread is not None:
            return
        self.backend.open()
        state = self.backend.read_state()
        notes = self._adopt(state)
        self._connected = True
        self._stop.clear()
        snap = self._build_snapshot({}, "", _NAN)
        with self._lock:
            self._snapshot = snap
        self._thread = threading.Thread(target=self._worker, name="synth-worker",
                                        daemon=True)
        self._thread.start()
        self._emit("info", f"connected: {self.backend.idn() or 'SynthHD'}  "
                           f"(state read, nothing changed)")
        for ch in CHANNELS:
            self._emit("info", f"{ch.upper()}: found {snap[f'{ch}_frequency_Hz'] / 1e6:.6f} MHz, "
                               f"{snap[f'{ch}_power_dBm']:g} dBm, "
                               f"RF {'ON' if snap[f'{ch}_rf_on'] else 'off'}")
        for level, msg in notes:
            self._emit(level, msg)

    def _adopt(self, state: dict) -> list:
        """Make the instrument's present state the desired AND the applied
        state (so the worker has nothing to send), and copy it into the config
        groups. Returns the (level, message) notes to emit once started."""
        notes = []
        unread = list(state.get("unread") or [])
        lim = self.cfg.limits
        bounds = {"frequency_Hz": (lim.freq_min_Hz, lim.freq_max_Hz),
                  "power_dBm": (lim.power_min_dBm, lim.power_max_dBm),
                  "phase_deg": (lim.phase_min_deg, lim.phase_max_deg)}
        with self._lock:
            for ch in CHANNELS:
                got = state["channels"][_INDEX[ch]]
                want = self._want[ch]
                for key in _KNOBS:
                    v = got.get(key)
                    if v is None:
                        continue            # unreadable: keep the config value (warned below)
                    want[key] = float(v)
                    lo, hi = bounds[key]
                    if not lo <= want[key] <= hi:
                        # NOT clamped: clamping would be a write. Say so instead.
                        notes.append(("warn", f"{ch.upper()}: instrument holds {key} = "
                                              f"{want[key]:g}, outside the limits "
                                              f"{lo:g}..{hi:g} -- left as it is"))
                want["rf_on"] = bool(got.get("rf_on"))
                if got.get("rf_partial"):
                    notes.append(("warn", f"{ch.upper()}: output only half on (mute and "
                                          f"amplifier disagree, or unreadable) -- shown as "
                                          f"{'ON' if want['rf_on'] else 'off'}; 'RF off' "
                                          f"switches both off"))
                pll = got.get("pll_on")
                self._pll_on_hw[ch] = (bool(pll) if pll is not None
                                       else want["rf_on"] or not self._pll_off_when_off)
                self._applied[ch] = dict(want)
                self._applied_gen[ch] = self._gen[ch]
                c = self.cfg.channel(ch)
                c.frequency_Hz = want["frequency_Hz"]
                c.power_dBm = want["power_dBm"]
                c.phase_deg = want["phase_deg"]
            src, ext = state.get("reference"), state.get("ext_MHz")
            src = src if src in REFERENCE_SOURCES else self._want_ref[0]
            ext = float(ext) if ext is not None else self._want_ref[1]
            self._want_ref = (src, ext)
            self._applied["ref"] = self._want_ref
            self._applied_gen["ref"] = self._gen["ref"]
            # keep the config in step: describe's SHAPE follows the reference,
            # so an adopted "external" makes ext_ref a control (revision moves)
            self.cfg.reference.source = src
            self.cfg.reference.ext_MHz = ext
            self._seen = self._channel_cfg()
        if unread:
            notes.append(("warn", "could not read " + ", ".join(unread) +
                                  " -- showing the config value there (NOT written)"))
        return notes

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Both outputs off, disconnect. Safe to call more than once / on a crash.

        keep_outputs=True is a RESTART for a code update (Lukas 2026-10-06):
        disconnect and release the port the same, but leave both RF outputs
        as they are -- the next start adopts them."""
        # no sweep step may follow the RF off below (and none may race the
        # backend's close): stop every sweep FIRST
        for ramp in self._sweeps.values():
            ramp.stop()
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if not keep_outputs:
            with self._lock:
                for ch in CHANNELS:
                    self._want[ch]["rf_on"] = False
        was_connected = self._connected
        try:
            if self._connected and not keep_outputs:
                # shutdown is NOT part of the read-only start rule: RF off on
                # the way out stays (a service that stops leaves nothing on)
                for ch in CHANNELS:
                    self.backend.set_output(_INDEX[ch], False)
        finally:
            try:
                self.backend.close(rf_off=not keep_outputs)
            finally:
                self._connected = False
                with self._lock:
                    self._applied = {"a": None, "b": None, "ref": None}
                    self._pll_on_hw = {"a": False, "b": False}
                    self._snapshot = self._build_snapshot({}, "", float("nan"))
                if was_connected:
                    self._emit("info", "disconnected (RF left as it is)"
                               if keep_outputs else "disconnected (RF off)")

    # ---- commands (each clamps, stores, wakes the worker) ------------------

    def set_rf(self, ch, on: bool) -> None:
        ch = parse_channel(ch)
        with self._lock:
            self._want[ch]["rf_on"] = bool(on)
            if not on:
                self._force_off[ch] = True   # really send it, even if "already off"
            self._gen[ch] += 1
        self._wake.set()
        self._emit("info", f"{ch.upper()}: RF {'ON' if on else 'OFF'}")

    def all_rf_off(self) -> None:
        for ch in CHANNELS:
            self.set_rf(ch, False)

    def set_frequency(self, ch, hz: float) -> None:
        self._take_over(ch, "frequency")
        lim = self.cfg.limits
        self._set(ch, "frequency_Hz", float(hz), lim.freq_min_Hz, lim.freq_max_Hz,
                  "frequency", "Hz")

    def set_power(self, ch, dBm: float) -> None:
        self._take_over(ch, "power")
        lim = self.cfg.limits
        self._set(ch, "power_dBm", float(dBm), lim.power_min_dBm, lim.power_max_dBm,
                  "power", "dBm")

    def set_phase(self, ch, deg: float) -> None:
        self._take_over(ch, "phase")
        lim = self.cfg.limits
        self._set(ch, "phase_deg", float(deg), lim.phase_min_deg, lim.phase_max_deg,
                  "phase", "deg")

    def _take_over(self, ch, knob: str) -> None:
        """A set is a new instruction: it takes the knob over from a sweep of
        THAT channel's knob (a set of another knob, or of the other channel,
        leaves a sweep running). Called BEFORE any lock is taken: stop() waits
        for the step in progress, which may itself be waiting for a lock."""
        ch = parse_channel(ch)
        if self._sweeps[f"{ch}_{knob}"].stop():
            self._emit("info", f"{ch.upper()}: {knob} sweep stopped by a {knob} set")

    def set_reference(self, source: str, ext_MHz: float | None = None) -> None:
        """Select the PLL reference for BOTH channels."""
        source = str(source)
        if source not in REFERENCE_SOURCES:
            raise ValueError(f"unknown reference {source!r} "
                             f"(use one of {', '.join(REFERENCE_SOURCES)})")
        lim = self.cfg.limits
        with self._lock:
            ext = self._want_ref[1] if ext_MHz is None else float(ext_MHz)
        ext, clamped = _clamp(ext, lim.ext_ref_min_MHz, lim.ext_ref_max_MHz)
        with self._lock:
            self._want_ref = (source, ext)
            self._gen["ref"] += 1
            # keep the config in step, so get_config / a saved .ini tell the truth
            self.cfg.reference.source = source
            self.cfg.reference.ext_MHz = ext
        self._wake.set()
        if clamped:
            self._emit("warn", f"external reference clamped to {ext:g} MHz "
                               f"(limit {lim.ext_ref_min_MHz:g}..{lim.ext_ref_max_MHz:g})")
        what = f"external {ext:g} MHz" if source == "external" else source
        self._emit("info", f"reference = {what}")

    def set_ext_ref(self, ext_MHz: float) -> None:
        """Change only the declared external reference frequency."""
        with self._lock:
            source = self._want_ref[0]
        self.set_reference(source, ext_MHz)

    def _set(self, ch, key, value, lo, hi, name, unit) -> None:
        ch = parse_channel(ch)
        value, clamped = _clamp(value, lo, hi)
        with self._lock:
            self._want[ch][key] = value
            self._gen[ch] += 1
            # keep the config group in step, so get_config / a saved .ini /
            # a GUI that connects later show the value that is really set
            setattr(self.cfg.channel(ch), key, value)
            self._seen[ch][key] = value
        self._wake.set()
        # frequencies read better in MHz than as 2.5e+09 Hz
        show = (lambda v: f"{v / 1e6:.6f} MHz") if unit == "Hz" else (lambda v: f"{v:g} {unit}")
        if clamped:
            self._emit("warn", f"{ch.upper()}: {name} clamped to {show(value)} "
                               f"(limit {show(lo)} .. {show(hi)})")
        else:
            self._emit("info", f"{ch.upper()}: {name} = {show(value)}")

    # ---- status ----------------------------------------------------------

    def status(self) -> dict:
        """The latest snapshot (a copy). Never touches hardware.

        The sweeps' fields are laid over that COPY, live and from memory (the
        snapshot itself is never edited -- gotcha #1): the worker rebuilds it
        only at poll_hz, and a fly scan waiting for the end of a sweep should
        not wait a poll period for nothing. While a knob sweeps, its value
        (`a_frequency_Hz` ...) is also the live one, the last value the sweep
        SENT; when it is not sweeping the snapshot's value stands, because
        only the snapshot pairs a value with its own `<ch>_settled` (a fresher
        value next to an older settled flag could let a scan go on before the
        PLL has locked on it)."""
        sweep = self.sweep_status()          # in memory: no hardware read
        with self._lock:
            snap = dict(self._snapshot)
            applied = {ch: self._applied[ch] for ch in CHANNELS}
        if self._connected:
            for name in SWEEPS:
                ch, knob = name.split("_", 1)
                if sweep[f"{name}_ramping"] and applied[ch] is not None:
                    key = SWEEP_KNOBS[knob][0]
                    snap[f"{ch}_{key}"] = applied[ch][key]
        snap.update(sweep)
        return snap

    # ---- the SWEEPS (fly scans) ------------------------------------------
    #
    # Why: a fly scan (scan-core, `type: fly` axis) records the detectors
    # while a knob moves CONTINUOUSLY and sorts every sample into the pixel of
    # the value the knob had at that moment. A synthesizer jumps to the value
    # it is told, so the service walks it: ramp_frequency / ramp_power /
    # ramp_phase start a walk of ONE channel's knob at a set pace, ramp_stop
    # ends it where it is, and an ordinary set of the same channel's knob
    # takes it over. The two channels are independent synthesizers sharing a
    # serial line, so each channel's knob has its own sweep (six in all,
    # named like the describe controls: "a_frequency", "b_phase", ...). The
    # channel is an argument of the ramp verbs, exactly as of set_frequency.
    #
    # What a fly scan bins by is the COMMANDED value (describe: readback
    # measured false). Decided 2026-10-10: the SynthHD has no measurement of
    # its output to read back -- "f?" / "W?" return the SETTING (the
    # frequency snapped to the channel grid, the requested power) and there
    # is no phase readback at all. A read-back would only echo the number
    # just sent, and as a query it waits for a reply line: on the serial line
    # that is a round trip per step, which roughly halves the step rate
    # (VERIFY on the unit how long one query takes). The PLL is at a new
    # frequency within ~100 us (datasheet lock time), far inside one 20 ms
    # step, so the command IS the value. `<ch>_locked` still tells whether
    # the PLL keeps up; a fly scan on an unlocked channel is visible there.
    #
    # The RF output is NEVER switched by a sweep: a step sends only the knob.
    #
    # Threads: a step runs on the sweep's own thread and writes to the serial
    # port directly, under `_io` (the worker takes `_io` for each of its own
    # backend calls). It also updates `_want` AND `_applied` to the value
    # sent, so the worker finds nothing to re-send. Going through the worker
    # instead (set `_want`, wake it) was rejected: after every push the
    # worker reads lock, leveled, frequency and temperature (seven queries),
    # which would cap the step rate and blur the time each value was sent.

    def _knob_limits(self, knob: str) -> tuple[float, float]:
        """The knob's safety envelope from config.Limits, read LIVE (an edited
        limit applies to the next step of a running sweep, too)."""
        _key, unit, _runit, pre = SWEEP_KNOBS[knob]
        lim = self.cfg.limits
        return (float(getattr(lim, f"{pre}_min_{unit}")),
                float(getattr(lim, f"{pre}_max_{unit}")))

    def _rate_limits(self, knob: str) -> tuple[float, float]:
        runit = SWEEP_KNOBS[knob][2]
        lim = self.cfg.limits
        return (float(getattr(lim, f"ramp_rate_min_{runit}")),
                float(getattr(lim, f"ramp_rate_max_{runit}")))

    def _sweep_setter(self, ch: str, knob: str):
        key = SWEEP_KNOBS[knob][0]
        set_fn = f"set_{knob}"
        i = _INDEX[ch]

        def step(value: float) -> None:
            """One step, on the sweep's own thread. Quiet (no event per step:
            a sweep is tens of steps a second) and only this one knob."""
            value = float(value)
            with self._io:
                if self._connected and self._applied[ch] is not None:
                    # VERIFY on the unit, for SWEEPS: how fast the SynthHD
                    # takes back-to-back writes (it re-levels the power after
                    # every frequency / power write) -- that bounds
                    # hardware.ramp_dt_s.
                    getattr(self.backend, set_fn)(i, value)
                with self._lock:
                    self._want[ch][key] = value
                    if self._applied[ch] is not None:
                        # a NEW dict (the snapshot builder may hold the old one)
                        self._applied[ch] = {**self._applied[ch], key: value}
                    # get_config / a saved .ini tell the truth, and
                    # apply_config does not mistake the sweep for a user edit
                    setattr(self.cfg.channel(ch), key, value)
                    self._seen[ch][key] = value
        return step

    def ramp(self, ch, knob: str, to: float, rate: float) -> int:
        """Sweep one channel's `knob` to `to` at `rate` (wire units: Hz, dBm,
        deg and per second); returns the sweep's number. The target is
        clamped to the knob's limits and the pace to the configured sweep
        paces, both with a warning -- like every setter here. A sweep of the
        same knob already running is taken over from wherever it got to."""
        ch = parse_channel(ch)
        if knob not in SWEEP_KNOBS:
            raise ValueError(f"cannot sweep {knob!r}; one of {sorted(SWEEP_KNOBS)}")
        _key, unit, runit, _pre = SWEEP_KNOBS[knob]
        shown_runit = runit.replace("_per_s", "/s")
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
        sw = self._sweeps[f"{ch}_{knob}"]
        sw.dt_s = max(0.001, float(self.cfg.hardware.ramp_dt_s))   # live config
        rid = sw.start(value, r)
        C = ch.upper()
        if clamped or rclamped:
            self._emit("warn", f"{C}: {knob} sweep clamped to {value:g} {unit} at "
                               f"{r:g} {shown_runit} (limits {lo:g}..{hi:g} {unit}, "
                               f"{rlo:g}..{rhi:g} {shown_runit})")
        self._emit("info", f"{C}: {knob} sweep -> {value:g} {unit} at {r:g} {shown_runit}")
        return rid

    def ramp_frequency(self, ch, hz: float, rate_Hz_per_s: float) -> int:
        return self.ramp(ch, "frequency", hz, rate_Hz_per_s)

    def ramp_power(self, ch, dBm: float, rate_dB_per_s: float) -> int:
        return self.ramp(ch, "power", dBm, rate_dB_per_s)

    def ramp_phase(self, ch, deg: float, rate_deg_per_s: float) -> int:
        return self.ramp(ch, "phase", deg, rate_deg_per_s)

    def ramp_stop(self, knob: str | None = None) -> bool:
        """End a sweep where it is -- of one channel's knob ("a_frequency"),
        or of every knob of both channels (None). True if one was running.
        A stop: allowed for a viewer too."""
        if knob is not None and knob not in self._sweeps:
            raise ValueError(f"no sweep {knob!r}; one of {list(SWEEPS)}")
        was = False
        for name in ([knob] if knob else list(SWEEPS)):
            if self._sweeps[name].stop():
                was = True
                ch, k = name.split("_", 1)
                key, unit = SWEEP_KNOBS[k][0], SWEEP_KNOBS[k][1]
                with self._lock:
                    here = self._want[ch][key]
                self._emit("info", f"{ch.upper()}: {k} sweep stopped at {here:g} {unit}")
        return was

    def _sweep_done(self, name: str, reason: str) -> None:
        # a fresh snapshot now, so the published value does not sit on the
        # last poll's mid-sweep number for up to a poll period
        self._wake.set()
        ch, knob = name.split("_", 1)
        key, unit = SWEEP_KNOBS[knob][0], SWEEP_KNOBS[knob][1]
        if reason == "done":
            with self._lock:
                here = self._want[ch][key]
            self._emit("info", f"{ch.upper()}: {knob} sweep done at {here:g} {unit}")
        elif reason.startswith("error"):
            self._emit("error", f"{ch.upper()}: {knob} sweep ended: {reason}")

    def sweep_status(self) -> dict:
        """The sweeps' live values as flat wire keys (in memory, no hardware).
        `<sweep>_ramp_id` is the newest sweep of that knob started; a caller
        whose sweep has number n waits for `<sweep>_ramp_id >= n` and
        `<sweep>_ramping` false -- numbered so a "not ramping" from before the
        start can never pass for the end (docs/DEVELOPER_NOTES.md gotcha #17).
        `ramping` = any knob of either channel sweeping."""
        out = {"ramping": False}
        for name in SWEEPS:
            _key, unit, runit, _pre = SWEEP_KNOBS[name.split("_", 1)[1]]
            r = self._sweeps[name].status()
            out[f"{name}_ramping"] = r["ramping"]
            out[f"{name}_ramp_id"] = r["ramp_id"]
            out[f"{name}_ramp_target_{unit}"] = r["ramp_target"]
            out[f"{name}_ramp_rate_{runit}"] = r["ramp_rate"]
            out["ramping"] = out["ramping"] or r["ramping"]
        return out

    # The stream verbs: ONE stream (group "ramp") with one channel per sweep
    # ("a_frequency", ..., "b_phase") -- every value each sweep sent, with the
    # time the backend had it. Each channel keeps its own time stamps (`t_ch`,
    # guide 6b "Streams"): the knobs are walked by separate threads. A knob
    # at rest still contributes its value (softramp records the rest value),
    # so a fly row's lead-in has a value to look up.

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

    # ---- settings (Settings dialog / wire use these) ---------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Called after set_config edited self.cfg in place.

        * a channel value the user CHANGED (compared with what the brain last
          saw in the config) is sent, through the normal setter -- the only way
          a config value reaches the instrument now (read-only start); values
          the Settings dialog merely sends back unchanged are ignored;
        * a changed channel spacing is sent (never at start);
        * the desired state is re-clamped to the (possibly new) limits;
        * the reference group is picked up if it changed."""
        cur = self._channel_cfg()
        setters = {"frequency_Hz": self.set_frequency, "power_dBm": self.set_power,
                   "phase_deg": self.set_phase}
        knob_of = {spec[0]: knob for knob, spec in SWEEP_KNOBS.items()}
        for ch in CHANNELS:
            for key in _KNOBS:
                if cur[ch][key] != self._seen[ch][key]:
                    if self._sweeps[f"{ch}_{knob_of[key]}"].running:
                        # A knob that is SWEEPING has moved since the Settings
                        # dialog (or any set_config sender) read it, so the
                        # value sent back is stale, not an edit: applying it
                        # would stop the sweep and jump back. Keep the live
                        # value in the config instead (ramp_stop first, to
                        # set this knob from Settings).
                        with self._lock:
                            setattr(self.cfg.channel(ch), key, self._want[ch][key])
                        continue
                    setters[key](ch, cur[ch][key])
        with self._lock:
            self._seen = self._channel_cfg()
        spacing = float(self.cfg.hardware.channel_spacing_Hz)
        if spacing != self._spacing_seen:
            self._spacing_seen = spacing
            if spacing > 0:
                with self._lock:
                    self._want_spacing = spacing
                self._emit("info", f"channel spacing -> {spacing:g} Hz")
        self._clamp_all(emit=True)
        with self._lock:
            for ch in CHANNELS:
                self._gen[ch] += 1
        r = self.cfg.reference
        with self._lock:
            changed = (r.source, float(r.ext_MHz)) != self._want_ref
        if changed:
            self.set_reference(r.source, r.ext_MHz)
        self._wake.set()

    def _channel_cfg(self) -> dict:
        return {ch: {k: float(getattr(self.cfg.channel(ch), k)) for k in _KNOBS}
                for ch in CHANNELS}

    def _clamp_all(self, emit: bool) -> None:
        lim = self.cfg.limits
        bounds = {"frequency_Hz": (lim.freq_min_Hz, lim.freq_max_Hz),
                  "power_dBm": (lim.power_min_dBm, lim.power_max_dBm),
                  "phase_deg": (lim.phase_min_deg, lim.phase_max_deg)}
        with self._lock:
            for ch in CHANNELS:
                for key, (lo, hi) in bounds.items():
                    v, clamped = _clamp(self._want[ch][key], lo, hi)
                    self._want[ch][key] = v
                    if clamped:
                        setattr(self.cfg.channel(ch), key, v)
                        if hasattr(self, "_seen"):      # not yet built in __init__
                            self._seen[ch][key] = v
                    if clamped and emit:
                        self._emit("warn", f"{ch.upper()}: {key} re-clamped to {v:g} "
                                           f"by the new limits")

    # ---- the worker ------------------------------------------------------

    def _worker(self) -> None:
        period = 1.0 / max(0.5, float(self.cfg.hardware.poll_hz))
        while not self._stop.is_set():
            self._poll_once()
            # Sleep until the next poll, but wake at once when a setter ran, so
            # a command reaches the hardware within a millisecond or so rather
            # than up to one poll period later.
            self._wake.wait(period)
            self._wake.clear()

    def _poll_once(self) -> None:
        """Push what changed, read what the instrument reports, publish."""
        error = ""
        readings = {}
        temp = float("nan")
        try:
            # Every backend call under `_io` (a sweep step may write from its
            # own thread). Taken per call, not around the whole poll, so a
            # sweep step waits at most one query, not seven, and stays on time.
            with self._io:
                self._push_changes()
            for ch in CHANNELS:
                i = _INDEX[ch]
                with self._io:
                    locked = bool(self.backend.read_locked(i))
                with self._io:
                    leveled = bool(self.backend.read_leveled(i))
                with self._io:
                    actual = float(self.backend.read_frequency(i))
                readings[ch] = {"locked": locked, "leveled": leveled,
                                "frequency_actual_Hz": actual}
            with self._io:
                temp = float(self.backend.read_temperature())
        except Exception as exc:                  # never let the worker die
            error = f"{type(exc).__name__}: {exc}"
        if error != self._last_error:
            if error:
                self._emit("error", f"hardware: {error}")
            elif self._last_error:
                self._emit("info", "hardware answering again")
            self._last_error = error
        snap = self._build_snapshot(readings, error, temp)
        with self._lock:
            self._snapshot = snap                  # one assignment = atomic swap
        self._watch_alarms(snap)

    def _push_changes(self) -> None:
        """Send the backend whatever differs from what it already holds.

        Order matters for safety: a channel being switched OFF is switched off
        FIRST; a channel being switched ON gets its frequency / power / phase
        BEFORE the output opens, so it never radiates the old setting.
        Right after start() nothing differs (the state was adopted), so the
        worker's first pass sends nothing.
        """
        for ch in CHANNELS:
            i = _INDEX[ch]
            with self._lock:
                want = dict(self._want[ch])
                gen = self._gen[ch]
                force_off = self._force_off[ch]
                self._force_off[ch] = False
            have = self._applied[ch]
            if have is not None and have == want and not force_off:
                self._applied_gen[ch] = gen
                continue
            have = have or {}
            if not want["rf_on"] and (have.get("rf_on") or force_off
                                      or "rf_on" not in have):
                self._output(ch, False)
            if have.get("frequency_Hz") != want["frequency_Hz"]:
                self.backend.set_frequency(i, want["frequency_Hz"])
            if have.get("power_dBm") != want["power_dBm"]:
                self.backend.set_power(i, want["power_dBm"])
            if have.get("phase_deg") != want["phase_deg"]:
                self.backend.set_phase(i, want["phase_deg"])
            if want["rf_on"] and not have.get("rf_on"):
                self._output(ch, True)
            self._applied[ch] = want
            self._applied_gen[ch] = gen
        with self._lock:
            want_ref = self._want_ref
            gen = self._gen["ref"]
            spacing, self._want_spacing = self._want_spacing, None
        if spacing is not None:
            self.backend.set_channel_spacing(spacing)
        if self._applied["ref"] != want_ref:
            self.backend.set_reference(*want_ref)
            self._applied["ref"] = want_ref
        self._applied_gen["ref"] = gen

    def _output(self, ch: str, on: bool) -> None:
        """Switch one output and keep track of its PLL power (what RF on/off
        sends decides that; see backends.synthhd.set_output)."""
        self.backend.set_output(_INDEX[ch], on)
        if on:
            self._pll_on_hw[ch] = True
        elif self._pll_off_when_off:
            self._pll_on_hw[ch] = False

    def _build_snapshot(self, readings: dict, error: str, temp: float) -> dict:
        with self._lock:
            want = {ch: dict(self._want[ch]) for ch in CHANNELS}
            gen = dict(self._gen)
            want_ref = self._want_ref
        snap = {"connected": self._connected,
                "idn": self.backend.idn() if self._connected else "",
                "hw_error": error,
                "temperature_C": temp}
        all_locked = True
        for ch in CHANNELS:
            have = self._applied[ch] if self._connected else None
            shown = have or want[ch]              # before start: the desired values
            r = readings.get(ch, {})
            locked = bool(r.get("locked", False))
            pll_powered = bool(self._connected and self._pll_on_hw[ch])
            applied = (self._connected and have is not None
                       and self._applied_gen[ch] == gen[ch])
            # With RF OFF nothing is radiated, so a missing lock cannot spoil a
            # measurement -- and switching OFF must never hang a scan just
            # because the reference is missing. The lock is waited for when
            # the output goes ON (that request needs locked to settle).
            settled = applied and not error and (locked or not shown["rf_on"])
            if pll_powered and not locked:
                all_locked = False
            snap[f"{ch}_rf_on"] = bool(shown["rf_on"])
            snap[f"{ch}_frequency_Hz"] = shown["frequency_Hz"]
            snap[f"{ch}_frequency_actual_Hz"] = r.get("frequency_actual_Hz",
                                                      shown["frequency_Hz"])
            snap[f"{ch}_power_dBm"] = shown["power_dBm"]
            snap[f"{ch}_phase_deg"] = shown["phase_deg"]
            snap[f"{ch}_locked"] = locked
            snap[f"{ch}_leveled"] = bool(r.get("leveled", False))
            snap[f"{ch}_pll_on"] = pll_powered
            snap[f"{ch}_settled"] = bool(settled)
        # True once BOTH outputs are off in the instrument and no request is
        # still waiting for the worker: what the `all_rf_off` action waits on.
        snap["rf_all_off"] = bool(
            self._connected and not error
            and all(self._applied[ch] is not None and not self._applied[ch]["rf_on"]
                    and self._applied_gen[ch] == gen[ch] for ch in CHANNELS))
        ref = self._applied["ref"] if (self._connected and self._applied["ref"]) else want_ref
        snap["reference"] = ref[0]
        snap["ext_ref_MHz"] = ref[1]
        snap["ref_settled"] = bool(self._connected and not error and all_locked
                                   and self._applied_gen["ref"] == gen["ref"])
        return snap

    def _watch_alarms(self, snap: dict) -> None:
        """Events for the things a person should notice: lost lock, heat."""
        for ch in CHANNELS:
            powered = snap[f"{ch}_pll_on"]
            if powered and not snap[f"{ch}_locked"]:
                self._unlocked_polls[ch] += 1
            else:
                self._unlocked_polls[ch] = 0
            # two polls in a row: a frequency hop's 100 us relock never warns
            if self._unlocked_polls[ch] >= 2 and not self._lock_warned[ch]:
                self._lock_warned[ch] = True
                hint = (" -- is the external reference connected and declared "
                        "at the right frequency?" if snap["reference"] == "external" else "")
                self._emit("warn", f"{ch.upper()}: PLL NOT LOCKED{hint}")
            elif self._unlocked_polls[ch] == 0 and self._lock_warned[ch]:
                self._lock_warned[ch] = False
                self._emit("info", f"{ch.upper()}: PLL locked again")
        t = snap["temperature_C"]
        limit = float(self.cfg.hardware.temp_warn_C)
        if t == t:                                  # not NaN
            if t > limit and not self._hot_warned:
                self._hot_warned = True
                self._emit("warn", f"internal temperature {t:.1f} C above {limit:g} C "
                                   f"(datasheet: keep below 75 C)")
            elif t < limit - 2.0:
                self._hot_warned = False

    # ---- internals -------------------------------------------------------

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
