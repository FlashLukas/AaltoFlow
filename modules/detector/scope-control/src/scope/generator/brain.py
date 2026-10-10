# COPIED from afg-control (src/afg/generator.py) -- the suite copies shared code
# instead of importing across modules. Channels renamed ch1/ch2 -> w1/w2
# (the Analog Discovery generator outputs W1/W2). Keep the two in step by
# hand when the original changes.
"""The Generator: the small "brain" between the wire and the backend.

A function generator needs no control loop, but three things make it more than
"send the number":

  * SAFETY. W1 may drive a magnet amplifier. Every request is CLAMPED to the
    lab's ceiling (config `limits_1` / `limits_2`: amplitude, peak voltage,
    frequency) AND to the instrument's own range for the chosen waveform and
    load (the backend's `envelope`), and a clamp is reported as a warn event.
    The peak rule is the important one: |offset| + amplitude/2 never passes
    `peak_max_V`, whatever order amplitude and offset are changed in.
  * HONESTY. The worker READS the instrument back after every change, and a
    channel counts as `settled` only when the read-back agrees with what was
    asked. An instrument that quietly coerces a value (a limit we did not know
    of) then makes a scan time out with `<ch>_mismatch` saying why, instead of
    recording data at a setting nobody chose.
  * COUPLING. "W2 follows W1": W2 gets W1's frequency, its phase is W1's
    plus an offset, and the two are phase-aligned after each change -- a
    synchronous trigger square next to the drive sine (the bench wiring:
    W1 -> scope W1, W2 -> scope W2 and EXT TRIG).

Threads (gotcha #1): ONE worker thread owns the instrument (pushes changes,
reads back, publishes a new snapshot dict each cycle). Setters only change
`_want` under the lock and bump a generation counter; they never touch the
snapshot. status() never touches hardware.

START-UP IS READ-ONLY (Lukas, 2026-09-27): start() opens the backend, READS
each channel and ADOPTS it as both desired and applied state, so the worker has
nothing to send. A sine left driving the magnet keeps driving it. The config's
channel values are overwritten with what was read (get_config tells the truth).

Generic over the backend: the number of channels, the waveforms and the
ranges come from the backend (`capabilities`, `envelope`), so the same brain
will serve the Analog Discovery's two generator outputs in the scope module.
"""

from __future__ import annotations

import threading
import time

from .base import WaveGen
from .config import GenConfig as Config, CHANNEL_NAMES
from . import waveforms
from ..softramp import SoftRamp   # src/scope/softramp.py (the master's copy)

_KNOBS = ("waveform", "frequency_Hz", "amplitude_Vpp", "offset_V", "phase_deg",
          "duty_pct", "symmetry_pct", "load_ohm")
_NAN = float("nan")
#: the knobs a RAMP can sweep (fly scans over any knob, 2026-10-10): wire name
#: -> (setting key, backend setter, unit of the rate)
RAMP_KNOBS = {"frequency": ("frequency_Hz", "set_frequency", "Hz/s"),
              "amplitude": ("amplitude_Vpp", "set_amplitude", "Vpp/s"),
              "offset": ("offset_V", "set_offset", "V/s"),
              "phase": ("phase_deg", "set_phase", "deg/s")}


class _Locked:
    """The backend behind ONE lock. Until ramps, only the brain's worker ever
    called the backend; a ramp's steps come from the ramp's own thread, so
    every call -- worker and ramp alike -- now goes through this lock."""

    def __init__(self, backend, lock):
        self._b = backend
        self._l = lock

    def __getattr__(self, name):
        attr = getattr(self._b, name)
        if not callable(attr):
            return attr
        lock = self._l

        def call(*args, **kw):
            with lock:
                return attr(*args, **kw)
        return call


#: how often the rarely changing settings (load, burst / sweep / modulation)
#: are read back; the rest is read every poll and after every push
_FULL_READ_S = 5.0


def parse_channel(ch) -> str:
    """'w1' / 'W1' / '1' / 1 -> 'w1';  'w2' / 2 -> 'w2'.
    (0-based numbers are NOT accepted: "channel 1" is W1 on the front panel.)"""
    key = str(ch).strip().lower()
    if key in ("1", "2"):
        key = "w" + key
    if key not in CHANNEL_NAMES:
        raise ValueError(f"unknown channel {ch!r} (use 'w1' or 'w2')")
    return key


def _index(ch: str) -> int:
    return CHANNEL_NAMES.index(ch)


def _clamp(v: float, lo: float, hi: float) -> tuple[float, bool]:
    if v < lo:
        return lo, True
    if v > hi:
        return hi, True
    return v, False


def _same(key: str, a, b, phase_tol: float = 0.05) -> bool:
    """Does the instrument's read-back `b` agree with what was asked, `a`?
    The tolerances are the AFG's display resolution, generously: a value the
    instrument ROUNDS is fine, a value it COERCES is not. Phases compare
    MODULO 360 (asked -90, the unit holds 270: the same phase) within
    `phase_tol` -- half the instrument's phase resolution."""
    if key in ("output", "waveform", "mode"):
        return a == b
    if key == "load_ohm":
        if a is None or b is None:
            return a is None and b is None
        return abs(a - b) <= 1e-3 * abs(a)
    if a is None or b is None:
        return False
    if key == "frequency_Hz":
        # 1e-6 relative: a DDS holds 1000 Hz as 1000.0000222 (the Analog
        # Discovery's, lab 2026-10-08) -- a rounding, not a coercion
        return abs(a - b) <= 2e-6 + 1e-6 * abs(a)
    if key == "amplitude_Vpp":
        return abs(a - b) <= 1e-4 + 5e-4 * abs(a)
    if key == "offset_V":
        return abs(a - b) <= 1e-3 + 5e-4 * abs(a)
    if key == "phase_deg":
        return abs(waveforms.wrap_phase(a - b)) <= phase_tol
    return abs(a - b) <= 0.05                       # duty / symmetry, percent


def _relevant(setting: dict) -> tuple:
    """The knobs that MEAN something for this setting's waveform (a DC level
    has no frequency; a sine has no duty cycle). Only these must agree with
    the read-back."""
    wf = setting.get("waveform")
    keys = ["output", "waveform", "load_ohm", "offset_V"]
    if wf not in ("dc",):
        keys.append("amplitude_Vpp")
    if wf not in ("dc", "noise"):
        keys += ["frequency_Hz", "phase_deg"]
    if wf == "pulse":
        keys.append("duty_pct")
    if wf == "ramp":
        keys.append("symmetry_pct")
    return tuple(keys)


class Generator:
    def __init__(self, backend: WaveGen, cfg: Config | None = None):
        self._io = threading.RLock()        # every backend call (see _Locked)
        self.backend = _Locked(backend, self._io)
        self.cfg = cfg or Config()
        # capabilities are a pure description (no I/O), safe to ask before open
        self.caps = dict(backend.capabilities())
        n = max(1, min(int(self.caps.get("channels", 2)), len(CHANNEL_NAMES)))
        self.channels = CHANNEL_NAMES[:n]
        self._lock = threading.RLock()
        # Where the MODULE's own settings are kept on this PC (afg.ini; the
        # service sets it). The coupling (W2 follows W1, phase follows,
        # phase offset) and the limits exist only in the module -- the AFG
        # cannot be asked for them -- so without this file a restart, even a
        # keep_outputs one, silently fell back to the defaults (lab PC
        # 2026-10-07). None = not saved (tests, a GUI on its own).
        self.persist_path = None
        self._persist_lock = threading.Lock()
        self._want = {ch: self._from_cfg(ch) for ch in self.channels}
        self._gen = {ch: 0 for ch in self.channels}
        self._applied_gen = {ch: -1 for ch in self.channels}
        self._applied = {ch: None for ch in self.channels}   # what the instrument was sent
        self._readback = {ch: None for ch in self.channels}  # what it last reported
        self._read_gen = {ch: -1 for ch in self.channels}    # gen the read-back belongs to
        self._mismatch = {ch: "" for ch in self.channels}
        # amplitude / offset as last ASKED per channel (see _change, _fit)
        self._asked: dict = {}
        # when each channel was last read back, and last read IN FULL (the
        # rarely changing load / mode / modulation queries) -- see _poll_once
        self._last_read = {ch: -1e9 for ch in self.channels}
        self._last_full = {ch: -1e9 for ch in self.channels}
        #: (mismatch text, read-backs in a row with it) -- see _read_back
        self._mismatch_seen: dict = {}
        #: what the backend learned about the firmware at open (queries it
        #: lacks), kept in status: as a start-up event alone it was gone for
        #: anyone who subscribed a few seconds late (lab PC 2026-10-06)
        self._probe_lines: list[str] = []
        # knobs the instrument CANNOT report (a firmware without the query,
        # e.g. ramp symmetry on the AFG1062 FV:V1.0.2): their value is the one
        # last set from here (the config's at start), and status says so
        self._not_read_back = {ch: [] for ch in self.channels}
        self._force_off = {ch: False for ch in self.channels}
        # numbered one-shot operations (outputs_off, align_phase): the reply
        # carries the number, the status the last one FINISHED (gotcha #17)
        self._op_next = 0
        self._ops: list[tuple[int, str]] = []
        self._op_done = 0
        self._op_ok = True
        self._align_pending = False
        self._connected = False
        self._idn = ""
        self._last_error = ""
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        # RAMPS (fly scans over any knob): one SoftRamp per channel and knob,
        # ONE running at a time (a fly axis flies one knob). The module numbers
        # them itself (ramp_id), so "my sweep is over" is one id for all.
        self._ramps = {}
        for ch in self.channels:
            for knob in RAMP_KNOBS:
                self._ramps[(ch, knob)] = SoftRamp(
                    (lambda v, ch=ch, knob=knob: self._ramp_step(ch, knob, v)),
                    (lambda ch=ch, knob=knob: self._want[ch][RAMP_KNOBS[knob][0]]),
                    limits=(lambda ch=ch, knob=knob: self.ramp_limits(ch, knob)),
                    dt_s=float(getattr(self.cfg.hardware, "ramp_dt_s", 0.05)),
                    on_done=(lambda rid, reason, ch=ch, knob=knob:
                             self._ramp_done(ch, knob, reason)),
                    channel=f"{ch}_{knob}", name=f"sweep-{ch}-{knob}")
        self._ramp_active = None             # (ch, knob) of the running / last sweep
        self._force_phase: dict = {}         # ch -> send the phase even if unchanged
        self._ramp_id = 0
        self._ramp_quiet_until = 0.0         # no "changed at the instrument" before this
        self._stream_id = 0
        self._seen = self._cfg_snapshot()
        self._snapshot = self._build_snapshot("")
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    def _from_cfg(self, ch: str) -> dict:
        c = self.cfg.channel(ch)
        d = {k: getattr(c, k) for k in _KNOBS if k != "load_ohm"}
        d.update(output=False, load_ohm=50.0, mode="continuous")
        return d

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Open the backend, READ every channel and adopt it, start the worker.
        Nothing is written to the instrument (read-only start)."""
        if self._thread is not None:
            return
        self.backend.open()
        notes = []
        for ch in self.channels:
            notes += self._adopt(ch, self.backend.read_channel(_index(ch), full=True))
        for err in self.backend.drain_errors():
            notes.append(("warn", f"instrument error while reading its state: {err}"))
        self._idn = self.backend.idn()
        # optional backend method: what the real backend learned about its
        # firmware when it opened (queries it lacks); the simulator has none
        notes = [("warn", f"instrument: {w}")
                 for w in getattr(self.backend, "open_warnings", [])] + notes
        report = getattr(self.backend, "probe_report", None)
        if callable(report):
            self._probe_lines = list(report())
            notes = [("info", f"instrument: {line}") for line in self._probe_lines] + notes
        self._connected = True
        self._stop.clear()
        snap = self._build_snapshot("")
        with self._lock:
            self._snapshot = snap
        self._thread = threading.Thread(target=self._worker, name="afg-worker",
                                        daemon=True)
        self._thread.start()
        self._emit("info", f"connected: {self._idn or 'generator'}  (state read, nothing changed)")
        for ch in self.channels:
            self._emit("info", f"{ch.upper()}: found {self._describe_setting(snap, ch)}")
        for level, msg in notes:
            self._emit(level, msg)

    def _adopt(self, ch: str, got: dict) -> list:
        """The instrument's present state becomes desired AND applied state
        (so the worker sends nothing) and is copied into the config."""
        notes = []
        unread = list(got.get("unread") or [])
        nrb = list(got.get("not_read_back") or [])
        with self._lock:
            self._not_read_back[ch] = nrb
            want = self._want[ch]
            for key in _KNOBS + ("output", "mode"):
                if key in got and (got[key] is not None or key == "load_ohm") \
                        and key not in unread:
                    want[key] = got[key]
            self._applied[ch] = dict(want)
            self._readback[ch] = dict(want)
            self._applied_gen[ch] = self._read_gen[ch] = self._gen[ch]
            self._to_cfg(ch)
            self._seen = self._cfg_snapshot()
            p = waveforms.peak(want["waveform"], want["amplitude_Vpp"], want["offset_V"])
            lim = self.cfg.limits(ch)
            if p > lim.peak_max_V + 1e-9 or want["amplitude_Vpp"] > lim.amplitude_max_Vpp + 1e-9:
                # NOT clamped: that would be a write at start. Say so loudly.
                notes.append(("warn", f"{ch.upper()}: the instrument is set ABOVE this "
                                      f"module's safety limits (peak {p:g} V, limit "
                                      f"{lim.peak_max_V:g} V) -- left as it is"))
        if unread:
            notes.append(("warn", f"{ch.upper()}: could not read " + ", ".join(unread) +
                                  " -- showing the config value there (NOT written)"))
        if nrb:
            notes.append(("info", f"{ch.upper()}: this instrument cannot report "
                                  + ", ".join(nrb) + " while running -- showing what it "
                                  "said at start (or the config value), then the value "
                                  "last set from here (NOT written, NOT read back)"))
        if want.get("mode", "continuous") != "continuous":
            notes.append(("warn", f"{ch.upper()}: the instrument is in {want['mode']} mode "
                                  f"-- left as it is; this module sets only the "
                                  f"continuous-wave parameters"))
        if want["waveform"] == "arb":
            notes.append(("info", f"{ch.upper()}: playing an arbitrary / special waveform "
                                  f"-- kept; it cannot be selected from here"))
        return notes

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Every output OFF, disconnect. Safe to call more than once / on a crash.
        (Not part of the read-only start rule: a stopping service leaves no
        output driving anything.)

        keep_outputs=True is a RESTART for a code update (Lukas 2026-10-06: a
        restart switched off outputs he was using): close the instrument and
        release its address exactly the same, but change no output -- the
        next start adopts the state, as every start does."""
        self.ramp_stop(quiet=True)          # no sweep step may follow the outputs off
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        was_connected = self._connected
        try:
            if self._connected and not keep_outputs:
                for ch in self.channels:
                    try:
                        self.backend.set_output(_index(ch), False)
                    except Exception:
                        pass
        finally:
            try:
                self.backend.close(outputs_off=not keep_outputs)
            finally:
                self._connected = False
                with self._lock:
                    for ch in self.channels:
                        if not keep_outputs:
                            self._want[ch]["output"] = False
                        self._applied[ch] = None
                    self._snapshot = self._build_snapshot("")
                if was_connected:
                    self._emit("info", "disconnected (outputs left as they are)"
                               if keep_outputs else "disconnected (outputs off)")

    # ---- the envelope: lab ceiling AND instrument range --------------------

    def envelope(self, ch: str, waveform: str | None = None, load_ohm="current") -> dict:
        """What may be asked of channel `ch` right now: the narrower of the
        lab's limits and the instrument's range for that waveform and load."""
        with self._lock:
            w = self._want[ch]
            waveform = waveform or w["waveform"]
            load = w["load_ohm"] if load_ohm == "current" else load_ohm
        hw = self.backend.envelope(waveform if waveform != "arb" else "arb", load)
        lim = self.cfg.limits(ch)
        fmin, fmax = hw.get("freq_min_Hz"), hw.get("freq_max_Hz")
        if fmax is not None:
            fmax = min(fmax, lim.freq_max_Hz)
        return {"freq_min_Hz": fmin, "freq_max_Hz": fmax,
                "amp_min_Vpp": hw["amp_min_Vpp"],
                "amp_max_Vpp": min(hw["amp_max_Vpp"], lim.amplitude_max_Vpp),
                "peak_max_V": min(hw["peak_max_V"], lim.peak_max_V),
                "duty_min_pct": hw.get("duty_min_pct", 0.0),
                "duty_max_pct": hw.get("duty_max_pct", 100.0)}

    def _fit(self, ch: str, w: dict, yields: str = "amplitude_Vpp") -> list[str]:
        """Clamp the desired setting `w` IN PLACE into the envelope. Returns
        what had to change (for the warn event).

        When amplitude and offset together pass the peak limit, the OFFSET is
        kept and the AMPLITUDE is cut back (`yields` is kept for callers but
        no longer changes this). Together with `_change` re-fitting from the
        pair AS ASKED, the result depends only on what was asked, never on the
        order (lab PC 2026-10-07: from 1 Vpp at +9.5 V, asking 20 Vpp then
        +1 V ended at 1 Vpp -- the amplitude had been cut against the OLD
        offset; now it is 18 Vpp at +1 V whichever comes first)."""
        env = self.envelope(ch, w["waveform"], w["load_ohm"])
        notes = []
        if env["freq_max_Hz"] is not None:
            w["frequency_Hz"], c = _clamp(w["frequency_Hz"], env["freq_min_Hz"], env["freq_max_Hz"])
            if c:
                notes.append(f"frequency {w['frequency_Hz']:g} Hz")
        if w["waveform"] != "dc":
            w["amplitude_Vpp"], c = _clamp(w["amplitude_Vpp"], env["amp_min_Vpp"], env["amp_max_Vpp"])
            if c:
                notes.append(f"amplitude {w['amplitude_Vpp']:g} Vpp")
            room = env["peak_max_V"] - abs(w["offset_V"])
            if w["amplitude_Vpp"] / 2 > room:
                asked = w["amplitude_Vpp"]
                w["amplitude_Vpp"] = max(env["amp_min_Vpp"], 2 * room)
                notes.append(f"amplitude {w['amplitude_Vpp']:g} Vpp for now, of {asked:g} "
                             f"asked (peak limit {env['peak_max_V']:g} V at offset "
                             f"{w['offset_V']:g} V; it follows when the offset changes)")
            half = 0.0 if w["waveform"] == "dc" else w["amplitude_Vpp"] / 2
        else:
            half = 0.0
        lim_off = max(0.0, env["peak_max_V"] - half)
        w["offset_V"], c = _clamp(w["offset_V"], -lim_off, lim_off)
        if c:
            notes.append(f"offset {w['offset_V']:g} V (peak limit {env['peak_max_V']:g} V)")
        w["duty_pct"], c = _clamp(w["duty_pct"], env["duty_min_pct"], env["duty_max_pct"])
        if c:
            notes.append(f"duty {w['duty_pct']:g} %")
        w["symmetry_pct"], _ = _clamp(w["symmetry_pct"], 0.0, 100.0)
        # The phase is KEPT as asked (lab PC 2026-10-07): -180 used to be
        # wrapped to +180 here, the status then echoed +180, and a scan that
        # asked -180 waited for an echo that never came. The instrument gets
        # an equivalent value in its own range (see `phase_to_send`); only a
        # silly number of turns is clamped.
        w["phase_deg"], c = _clamp(float(w["phase_deg"]), -360.0, 360.0)
        if c:
            notes.append(f"phase {w['phase_deg']:g} deg")
        return notes

    def phase_resolution(self) -> float:
        """The instrument's phase step in degrees (AFG1062: whole degrees,
        measured 2026-10-07); 0 = continuous."""
        return float(self.caps.get("phase_resolution_deg", 0.0) or 0.0)

    def phase_to_send(self, deg: float) -> float:
        """The value the INSTRUMENT is given for a phase of `deg`: the same
        angle in 0 <= x < 360 (the AFG1062 rejects a negative phase with
        -201), rounded to its resolution (it TRUNCATES 31.6 to 31, and a
        31 deg sent as 0.54105207 rad came back as 30 -- so the module rounds,
        and sends exact whole degrees)."""
        x = float(deg) % 360.0
        res = self.phase_resolution()
        if res > 0:
            x = round(x / res) * res
        return round(x % 360.0, 9)

    def _phase_tol(self) -> float:
        return max(0.05, self.phase_resolution() / 2.0 + 1e-6)

    # ---- commands (each clamps, stores, wakes the worker) ------------------

    def _change(self, ch, what: str, **values) -> None:
        """Apply `values` to channel `ch`'s desired setting, clamp the whole
        setting, keep the config in step, wake the worker."""
        ch = parse_channel(ch)
        if ch not in self.channels:
            raise ValueError(f"{ch} does not exist on this instrument")
        with self._lock:
            w = dict(self._want[ch])
            w.update(values)
            # amplitude and offset are fitted from the values last ASKED (not
            # the clamped ones), so the order of two requests cannot matter
            asked = self._asked.setdefault(
                ch, {"amplitude_Vpp": w["amplitude_Vpp"], "offset_V": w["offset_V"]})
            for k in ("amplitude_Vpp", "offset_V"):
                if k in values:
                    asked[k] = float(values[k])
                w[k] = asked[k]
            notes = self._fit(ch, w)
            self._want[ch] = w
            self._gen[ch] += 1
            self._to_cfg(ch)
            if self._follows() and ch == "w1":
                self._mirror_to_ch2(notes_out=notes)
        self._wake.set()
        if notes:
            self._emit("warn", f"{ch.upper()}: {what} -- limited: " + "; ".join(notes))
        else:
            self._emit("info", f"{ch.upper()}: {what}")

    def _refuse_if_following(self, ch: str, what: str) -> None:
        if parse_channel(ch) != "w2":
            return
        if what == "frequency" and self._follows():
            raise ValueError("W2's frequency follows W1: set it on W1 (or switch "
                             "'frequency follows W1' off)")
        if what == "phase" and self._phase_follows():
            raise ValueError("W2's phase follows W1 (+ offset): set W1's phase or "
                             "the offset (or switch 'phase follows W1' off)")

    def set_output(self, ch, on: bool) -> None:
        ch = parse_channel(ch)
        if not on:
            with self._lock:
                self._force_off[ch] = True       # really send it, even if "already off"
        self._change(ch, f"output {'ON' if on else 'OFF'}", output=bool(on))

    def set_waveform(self, ch, waveform: str) -> None:
        waveform = str(waveform).strip().lower()
        if waveform not in self.caps.get("waveforms", ()):
            # a shape the instrument has but cannot be driven to remotely
            # says WHY (lab PC 2026-10-07: "unknown waveform 'noise'" read
            # like a typo); anything else is simply unknown
            why = (self.caps.get("unavailable") or {}).get(waveform)
            if why:
                raise ValueError(f"{waveform} is not available on this instrument: {why}")
            raise ValueError(f"unknown waveform {waveform!r} (use one of "
                             f"{', '.join(self.caps.get('waveforms', ()))})")
        self._change(ch, f"waveform {waveform}", waveform=waveform)

    def set_frequency(self, ch, hz: float) -> None:
        self._refuse_if_following(ch, "frequency")
        self._take_over(ch, "frequency")
        self._change(ch, f"frequency {float(hz):g} Hz", frequency_Hz=float(hz))

    def set_amplitude(self, ch, vpp: float) -> None:
        self._take_over(ch, "amplitude")
        self._change(ch, f"amplitude {float(vpp):g} Vpp", amplitude_Vpp=float(vpp))

    def set_offset(self, ch, volts: float) -> None:
        self._take_over(ch, "offset")
        self._change(ch, f"offset {float(volts):g} V", offset_V=float(volts))

    def set_phase(self, ch, deg: float) -> None:
        self._refuse_if_following(ch, "phase")
        self._take_over(ch, "phase")
        # a phase SET is always sent, even when the value is unchanged: on an
        # instrument where it re-syncs the outputs (the Analog Discovery), a
        # repeated "phase 0" is how the outputs are put in phase again (lab
        # 2026-10-10: W2 phase 0 asked again after a W1 frequency change, phase_21
        # stayed at +86 deg)
        with self._lock:
            self._force_phase[parse_channel(ch)] = True
        self._change(ch, f"phase {float(deg):g} deg", phase_deg=float(deg))

    def set_duty(self, ch, pct: float) -> None:
        self._change(ch, f"duty {float(pct):g} %", duty_pct=float(pct))

    def set_symmetry(self, ch, pct: float) -> None:
        if not self.caps.get("ramp_symmetry", True):
            raise ValueError("this instrument has no ramp symmetry setting (the "
                             "AFG1062 firmware lacks the command)")
        self._change(ch, f"symmetry {float(pct):g} %", symmetry_pct=float(pct))

    def set_load(self, ch, load_ohm) -> None:
        """50 ohm, a value, or None / "highz" / "inf" for high-Z. The AFG
        rescales its displayed volts for the new load; the worker reads them
        back afterwards and adopts them."""
        if isinstance(load_ohm, str):
            t = load_ohm.strip().lower()
            load_ohm = None if t in ("highz", "high-z", "inf", "infinity", "") else float(t)
        if load_ohm is not None:
            load_ohm = float(load_ohm)
            if not 1.0 <= load_ohm <= 10000.0:
                raise ValueError("load must be 1..10000 ohm or high-Z")
        if not self.caps.get("load_settable", False):
            raise ValueError("this instrument has no load setting")
        self._change(ch, "load " + ("high-Z" if load_ohm is None else f"{load_ohm:g} ohm"),
                     load_ohm=load_ohm)

    def set_follow(self, on: bool, phase_offset_deg: float | None = None,
                   phase: bool | None = None) -> None:
        """"W2's frequency follows W1" on/off; optionally a new phase offset
        and whether the PHASE follows too (Lukas 2026-10-07: "i want to be able
        to select if also the phase follows or not"). The phase can only
        follow while the frequency does: at different frequencies a phase
        relation means nothing."""
        if len(self.channels) < 2:
            raise ValueError("needs two channels")
        notes = []
        with self._lock:
            co = self.cfg.coupling
            co.ch2_follows_ch1 = bool(on)
            if phase is not None:
                co.ch2_phase_follows = bool(phase)
            if phase_offset_deg is not None:
                # kept as asked, like a phase setpoint (a scan echoes it)
                co.phase_offset_deg, _ = _clamp(float(phase_offset_deg), -360.0, 360.0)
            if on:
                self._mirror_to_ch2(notes_out=notes)
            self._seen = self._cfg_snapshot()
        self._wake.set()
        self._persist()
        what = "frequency and phase follow" if self._phase_follows() else "frequency follows"
        self._emit("info", f"W2 {what} W1: {'ON' if on else 'off'}"
                           + (f", phase offset {self.cfg.coupling.phase_offset_deg:g} deg"
                              if self._phase_follows() else ""))
        if notes:
            self._emit("warn", "W2 clamped -> " + ", ".join(notes))

    def set_phase_follow(self, on: bool) -> None:
        """Whether W2's phase follows W1's (+ offset) while its frequency
        does. Off: W2's phase is its own setting again."""
        self.set_follow(self._follows(), None, bool(on))

    def set_phase_offset(self, deg: float) -> None:
        self.set_follow(self._follows(), deg)

    def _follows(self) -> bool:
        """W2's FREQUENCY follows W1."""
        return len(self.channels) > 1 and bool(self.cfg.coupling.ch2_follows_ch1)

    def _phase_follows(self) -> bool:
        """W2's PHASE follows W1 (+ offset): only while the frequency does."""
        return self._follows() and bool(getattr(self.cfg.coupling, "ch2_phase_follows", True))

    def _mirror_to_ch2(self, notes_out: list) -> None:
        """W2 takes W1's frequency and -- if the phase follows too -- W1's
        phase + offset; ask for an alignment (so W2's phase, followed or its
        own, counts from W1's). Called with the lock held."""
        w1, w2 = self._want["w1"], dict(self._want["w2"])
        w2["frequency_Hz"] = w1["frequency_Hz"]
        if self._phase_follows():
            w2["phase_deg"] = waveforms.wrap_phase(w1["phase_deg"]
                                                   + self.cfg.coupling.phase_offset_deg)
        notes_out += self._fit("w2", w2)
        if w2 != self._want["w2"]:
            self._want["w2"] = w2
            self._gen["w2"] += 1
            self._to_cfg("w2")
        self._align_pending = True

    def _coupling_after_panel(self, ch: str, changed: list) -> None:
        """A change made AT THE AFG while W2 follows W1 (lab PC 2026-10-08:
        W1 set to 118 Hz at the panel, W2 stayed at 114 Hz -- the coupling
        silently broken). The coupling is the user's standing instruction:
          * W1 changed -> W2 follows, exactly as after a set from here;
          * W2's followed knob changed -> the user overrode the coupling at
            the instrument: that follow is switched OFF (said, and saved),
            rather than fighting the panel in a loop."""
        if len(self.channels) < 2 or not self._follows():
            return
        notes, followed, dropped = [], "", ""
        with self._lock:
            co = self.cfg.coupling
            if ch == "w1" and ("frequency_Hz" in changed
                                or (self._phase_follows() and "phase_deg" in changed)):
                self._mirror_to_ch2(notes_out=notes)
                w2 = self._want["w2"]
                followed = f"{w2['frequency_Hz']:g} Hz" + (
                    f", phase {w2['phase_deg']:g} deg" if self._phase_follows() else "")
            elif ch == "w2" and "frequency_Hz" in changed:
                co.ch2_follows_ch1 = False
                dropped = "frequency follow (and phase follow with it)"
            elif ch == "w2" and self._phase_follows() and "phase_deg" in changed:
                co.ch2_phase_follows = False
                dropped = "phase follow"
            self._seen = self._cfg_snapshot()
        if followed:
            self._wake.set()
            self._emit("info", f"W1 changed at the instrument -> W2 follows: {followed}")
            if notes:
                self._emit("warn", "W2 clamped -> " + ", ".join(notes))
        if dropped:
            self._persist()
            self._emit("warn", f"W2 changed at the instrument: {dropped} switched OFF")

    # ---- RAMPS: sweep a knob at a set pace (fly scans over any knob) ----------

    def ramp_limits(self, ch: str, knob: str) -> tuple[float, float]:
        """The range a sweep of `knob` may cover NOW: the same clamps as a set
        (lab limits AND the instrument's range; amplitude and offset by the
        peak rule against the other's present value)."""
        key = RAMP_KNOBS[knob][0]
        with self._lock:
            w = dict(self._want[ch])
        env = self.envelope(ch)
        lim = self.cfg.limits(ch)
        peak = min(float(env["peak_max_V"]), float(lim.peak_max_V))
        if key == "frequency_Hz":
            if env.get("freq_max_Hz") is None:
                raise ValueError(f"{ch.upper()} has no frequency for {w['waveform']}")
            return (float(env["freq_min_Hz"]),
                    min(float(env["freq_max_Hz"]), float(lim.freq_max_Hz)))
        if key == "amplitude_Vpp":
            hi = min(float(env["amp_max_Vpp"]), float(lim.amplitude_max_Vpp),
                     2.0 * max(0.0, peak - abs(float(w["offset_V"]))))
            return float(env["amp_min_Vpp"]), max(float(env["amp_min_Vpp"]), hi)
        if key == "offset_V":
            half = 0.0 if w["waveform"] == "dc" else float(w["amplitude_Vpp"]) / 2.0
            room = max(0.0, peak - half)
            return -room, room
        return -180.0, 360.0                     # phase: as a set

    def ramp_rate_limits(self, knob: str) -> tuple[float, float, float]:
        """(min, max, default) of a sweep's pace, in the knob's unit per second
        (config group `hardware`, ramp_rate_*)."""
        hw = self.cfg.hardware
        name = {"frequency": "freq", "amplitude": "amp", "offset": "offset",
                "phase": "phase"}[knob]
        lo = float(getattr(hw, f"ramp_{name}_rate_min"))
        hi = float(getattr(hw, f"ramp_{name}_rate_max"))
        df = float(getattr(hw, f"ramp_{name}_rate_default"))
        return lo, hi, min(max(df, lo), hi)

    def ramp_start(self, ch, knob: str, to: float, rate: float) -> int:
        """Sweep `knob` of channel `ch` from where it is to `to` at `rate`
        (knob units per second). Returns the sweep's number.

        Clamped like a set (warned); the follower's knob (W2 frequency /
        phase while it follows W1) is REFUSED -- sweep W1, W2 follows each
        step as it follows a set. Never switches an output: a sweep of an
        output that is off moves its setting, nothing comes out. A running
        sweep of any knob is stopped first (one at a time)."""
        ch = parse_channel(ch)
        knob = str(knob).strip().lower()
        if knob not in RAMP_KNOBS:
            raise ValueError(f"cannot sweep {knob!r} (use {', '.join(RAMP_KNOBS)})")
        if ch not in self.channels:
            raise ValueError(f"{ch} does not exist on this instrument")
        self._refuse_if_following(ch, knob if knob in ("frequency", "phase") else "")
        to, rate = float(to), abs(float(rate))
        if not (to == to and rate == rate) or rate <= 0:
            raise ValueError("a sweep needs a finite target and a rate > 0")
        lo, hi = self.ramp_limits(ch, knob)
        rlo, rhi, _ = self.ramp_rate_limits(knob)
        to_c = min(max(to, lo), hi)
        rate_c = min(max(rate, rlo), rhi)
        self.ramp_stop(quiet=True)
        unit = RAMP_KNOBS[knob][2]
        with self._lock:
            self._ramp_id += 1
            rid = self._ramp_id
            self._ramp_active = (ch, knob)
        self._ramp_quiet_until = float("inf")
        self._ramps[(ch, knob)].start(to_c, rate_c)
        if to_c != to or rate_c != rate:
            self._emit("warn", f"{ch.upper()}: sweep of {knob} limited to {to_c:g} at "
                               f"{rate_c:g} {unit} (asked {to:g} at {rate:g})")
        self._emit("info", f"{ch.upper()}: {knob} sweep -> {to_c:g} at {rate_c:g} {unit} "
                           f"(#{rid})")
        return rid

    def ramp_stop(self, quiet: bool = False) -> bool:
        """End the running sweep WHERE IT IS. True if one was running."""
        was = False
        for r in self._ramps.values():
            was = r.stop() or was
        if was and not quiet and self._ramp_active is not None:
            ch, knob = self._ramp_active
            self._emit("info", f"{ch.upper()}: {knob} sweep stopped at "
                               f"{self._want[ch][RAMP_KNOBS[knob][0]]:g}")
        return was

    def _take_over(self, ch, knob: str) -> None:
        """A set of a knob that is being swept takes it over: the sweep stops
        first (BEFORE any lock its step may be waiting for)."""
        ch = parse_channel(ch)
        act = self._ramp_active
        if act is not None and self._ramps[act].running and (
                act == (ch, knob) or (self._follows() and ch == "w1"
                                      and act[1] in ("frequency", "phase"))):
            self._ramps[act].stop()
            self._emit("info", f"{act[0].upper()}: {act[1]} sweep stopped by a set")

    def ramping(self) -> bool:
        return any(r.running for r in self._ramps.values())

    def _ramp_step(self, ch: str, knob: str, v: float) -> None:
        """One step of a sweep, on the sweep's thread: straight to the
        instrument (one command), bypassing the worker's push -- and the
        desired / sent / read-back records updated to it, so the worker
        neither pushes the old value back nor takes the change for a hand at
        the front panel. A follower (W2 while it follows W1) gets its step
        in the same breath. Quiet: no event per step."""
        key, setter, _ = RAMP_KNOBS[knob]
        steps = [(ch, key, v)]
        if ch == "w1" and len(self.channels) > 1 and self._follows():
            if key == "frequency_Hz":
                steps.append(("w2", key, v))
            if self._phase_follows() and key == "phase_deg":
                steps.append(("w2", key,
                              waveforms.wrap_phase(v + self.cfg.coupling.phase_offset_deg)))
        for c, k, val in steps:
            send = self.phase_to_send(val) if k == "phase_deg" else val
            # a backend may have a lighter call for a sweep step (the Analog
            # Discovery: a phase step WITHOUT the synced restart of both
            # outputs a phase set does -- that would glitch on every step)
            fn = getattr(self.backend, "ramp_" + setter, None) or getattr(self.backend, setter)
            fn(_index(c), send)
            with self._lock:
                self._want[c][k] = val
                if self._applied[c] is not None:
                    self._applied[c][k] = val
                if self._readback[c] is not None:
                    self._readback[c][k] = val
                if k in ("amplitude_Vpp", "offset_V") and c in self._asked:
                    self._asked[c][k] = val

    def _ramp_done(self, ch: str, knob: str, reason: str) -> None:
        """A sweep ended (reached, stopped, failed): the config follows the
        value, a follower is re-aligned, and the read-back may judge again
        once the instrument has caught up."""
        with self._lock:
            for c in self.channels:
                self._to_cfg(c)
            self._seen = self._cfg_snapshot()
        if (len(self.channels) > 1 and self._follows() and ch == "w1"
                and knob in ("frequency", "phase")):
            self._align_pending = True          # the follower in phase again
        self._ramp_quiet_until = time.monotonic() + 1.5
        self._wake.set()
        if reason.startswith("error"):
            self._emit("error", f"{ch.upper()}: {knob} sweep failed: {reason[7:]}")
        elif reason == "done":
            self._emit("info", f"{ch.upper()}: {knob} sweep done")

    def ramp_status(self) -> dict:
        act = self._ramp_active
        r = self._ramps.get(act) if act else None
        st = r.status() if r else {}
        return {"ramping": self.ramping(), "ramp_id": int(self._ramp_id),
                "ramp_knob": f"{act[0]}_{act[1]}" if act else "",
                "ramp_target": st.get("ramp_target"), "ramp_rate": st.get("ramp_rate"),
                "ramp_value": st.get("ramp_value"), "ramp_error": st.get("ramp_error", "")}

    # ---- the sweeps' record, in the stream format (fly scans bin by it) ------

    def stream_start(self) -> int:
        """Start recording every sweepable knob (group "ramp"): each channel
        of the stream is one knob, with its own time stamps (t_ch)."""
        for r in self._ramps.values():
            r.stream_start()
        with self._lock:
            self._stream_id += 1
            return self._stream_id

    def stream_read(self) -> dict:
        return self._merge([r.stream_read() for r in self._ramps.values()])

    def stream_stop(self) -> dict:
        return self._merge([r.stream_stop() for r in self._ramps.values()])

    def _merge(self, chunks: list) -> dict:
        """One reply from every knob's record: values and own stamps per
        channel (t_ch); `t` = the stamps of the knob being swept (or the
        first), as a default for clients that look at `t` alone."""
        values, t_ch, delay = {}, {}, {}
        overflow = False
        for c in chunks:
            for name, vals in (c.get("values") or {}).items():
                values[name] = vals
                t_ch[name] = list(c.get("t") or [])
                delay[name] = 0.0
            overflow = overflow or bool(c.get("overflow"))
        act = self._ramp_active
        main = f"{act[0]}_{act[1]}" if act else next(iter(t_ch), None)
        return {"id": self._stream_id, "t": t_ch.get(main, []), "t_ch": t_ch,
                "values": values, "delay_s": delay, "overflow": overflow,
                "now": time.time()}

    def outputs_off(self) -> int:
        """Every output OFF (the safety action). Returns the operation number."""
        self.ramp_stop(quiet=True)
        for ch in self.channels:
            with self._lock:
                self._force_off[ch] = True
                self._want[ch]["output"] = False
                self._gen[ch] += 1
        self._emit("info", "all outputs OFF")
        return self._queue_op("outputs_off")

    def align_phase(self) -> int:
        """Restart the channels' phases together. Returns the operation number."""
        if not self.caps.get("phase_align", False):
            raise ValueError("this instrument cannot align the channel phases")
        return self._queue_op("align_phase")

    def _queue_op(self, kind: str) -> int:
        with self._lock:
            self._op_next += 1
            self._ops.append((self._op_next, kind))
            n = self._op_next
        self._wake.set()
        return n

    def desired(self, ch: str) -> dict:
        """A copy of channel `ch`'s DESIRED setting (what was last asked, after
        clamping). describe builds its shape from this rather than from the
        status snapshot, which lags one worker cycle behind a setter."""
        with self._lock:
            return dict(self._want[parse_channel(ch)])

    def not_read_back(self, ch: str) -> list[str]:
        """The knobs of channel `ch` the instrument cannot report (their value
        is the one last set from here). [] for the simulator."""
        with self._lock:
            return list(self._not_read_back[parse_channel(ch)])

    def follows(self) -> bool:
        """Does W2's frequency follow W1?"""
        return self._follows()

    def phase_follows(self) -> bool:
        """Does W2's phase follow W1 (+ offset)?"""
        return self._phase_follows()

    # ---- status ----------------------------------------------------------

    def status(self) -> dict:
        """The latest snapshot (a copy), with any request the worker has not
        finished laid over it. Never touches hardware.

        The snapshot is rebuilt by the worker, so right after a setter it
        still showed the OLD setpoints with `settled` True for 0.5-1.6 s (lab
        PC 2026-10-07). adopt_then_flag protects a scan from that, but a GUI or
        script reading status would believe the old state. So a channel with a
        request not yet pushed AND read back shows the NEW setpoints and
        settled False at once."""
        with self._lock:
            snap = dict(self._snapshot)
            if not self._connected:
                return snap
            for ch in self.channels:
                done = (self._applied_gen[ch] == self._gen[ch]
                        and self._read_gen[ch] == self._gen[ch])
                if not done:
                    w = self._want[ch]
                    for k in ("output", "waveform", "frequency_Hz", "amplitude_Vpp",
                              "offset_V", "phase_deg", "duty_pct", "symmetry_pct"):
                        snap[f"{ch}_{k}"] = w[k]
                    snap[f"{ch}_settled"] = False
            if self.ramping():
                # a sweep moves the setpoint between two worker cycles: show
                # where it is now
                for ch in self.channels:
                    w = self._want[ch]
                    for k in ("frequency_Hz", "amplitude_Vpp", "offset_V", "phase_deg"):
                        snap[f"{ch}_{k}"] = w[k]
            snap.update(self.ramp_status())
            snap["follow"] = self._follows()
            snap["phase_follow"] = self._phase_follows()
            snap["phase_follow_set"] = bool(getattr(self.cfg.coupling, "ch2_phase_follows", True))
            snap["phase_offset_deg"] = float(self.cfg.coupling.phase_offset_deg)
            return snap

    # ---- settings (Settings dialog / wire use these) ---------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Called after set_config edited self.cfg in place.

        A channel value the user CHANGED (compared with what the brain last saw
        in the config) is sent through the normal setter -- the only way a
        config value reaches the instrument (read-only start); values the
        Settings dialog merely sends back unchanged are ignored. New limits
        re-clamp every channel (a LOWERED ceiling is pushed at once: that is
        the point of lowering it). A changed coupling is applied."""
        cur = self._cfg_snapshot()
        seen = self._seen
        setters = {"waveform": self.set_waveform, "frequency_Hz": self.set_frequency,
                   "amplitude_Vpp": self.set_amplitude, "offset_V": self.set_offset,
                   "phase_deg": self.set_phase, "duty_pct": self.set_duty,
                   "symmetry_pct": self.set_symmetry}
        if cur["coupling"] != seen["coupling"]:
            self.set_follow(cur["coupling"]["ch2_follows_ch1"],
                            cur["coupling"]["phase_offset_deg"],
                            cur["coupling"]["ch2_phase_follows"])
        for ch in self.channels:
            for key, fn in setters.items():
                if cur[ch][key] != seen[ch][key]:
                    try:
                        fn(ch, cur[ch][key])
                    except ValueError as exc:
                        self._emit("warn", f"{ch.upper()}: {exc}")
        # re-clamp to the (possibly new) limits
        for ch in self.channels:
            with self._lock:
                w = dict(self._want[ch])
                notes = self._fit(ch, w)
                if w != self._want[ch]:
                    self._want[ch] = w
                    self._gen[ch] += 1
                    self._to_cfg(ch)
            if notes:
                self._emit("warn", f"{ch.upper()}: re-clamped by the new limits -> "
                                   + ", ".join(notes))
        with self._lock:
            self._seen = self._cfg_snapshot()
        self._wake.set()
        self._persist()

    def _persist(self) -> None:
        """Write the whole config to `persist_path`: a temporary file in the
        same folder, then os.replace -- an interrupted write leaves the old
        file whole. The channel values in it are harmless: at start they are
        READ from the AFG (read-only start); only the module's own settings
        (coupling, limits, hardware, ui) come from the file."""
        path = self.persist_path
        if not path:
            return
        import os
        tmp = f"{path}.tmp"
        try:
            with self._persist_lock:
                self.cfg.save(tmp)
                os.replace(tmp, path)
        except OSError as exc:
            self._emit("warn", f"could not save the settings to {path}: {exc}")

    def _to_cfg(self, ch: str) -> None:
        """Copy the desired setting into the config group (lock held)."""
        c, w = self.cfg.channel(ch), self._want[ch]
        for k in _KNOBS:
            if k != "load_ohm" and w.get(k) is not None:
                setattr(c, k, w[k])

    def _cfg_snapshot(self) -> dict:
        d = {ch: {k: getattr(self.cfg.channel(ch), k) for k in _KNOBS if k != "load_ohm"}
             for ch in self.channels}
        co = self.cfg.coupling
        d["coupling"] = {"ch2_follows_ch1": bool(co.ch2_follows_ch1),
                         "ch2_phase_follows": bool(getattr(co, "ch2_phase_follows", True)),
                         "phase_offset_deg": float(co.phase_offset_deg)}
        return d

    # ---- the worker ------------------------------------------------------

    def _worker(self) -> None:
        period = 1.0 / max(0.2, float(self.cfg.hardware.poll_hz))
        while not self._stop.is_set():
            self._poll_once()
            # sleep until the next read-back, but wake at once for a command
            self._wake.wait(period)
            self._wake.clear()

    def _poll_once(self) -> None:
        """Push what changed, run queued operations, read back, publish."""
        error = ""
        pushed = set()
        try:
            for ch in self.channels:
                if self._push(ch):
                    pushed.add(ch)
            self._run_ops()
            for err in self.backend.drain_errors():
                self._emit("warn", f"instrument: {err}")
            # READ BACK what is due (lab PC 2026-10-07: ~2 s per change; each
            # full read is ~15 USB queries per channel):
            #  * a channel that was just pushed -- at once, that is "settled";
            #  * any other channel once per poll period (front-panel changes);
            #  * the rarely changing load / mode / modulation queries only
            #    every _FULL_READ_S;
            #  * and if a NEW request arrived meanwhile, stop reading: it is
            #    pushed in the next pass at once (the wake flag is set), and a
            #    read of the old state would be stale anyway.
            now = time.monotonic()
            period = 1.0 / max(0.2, float(self.cfg.hardware.poll_hz))
            for ch in self.channels:
                if self._new_request():
                    break
                if ch in pushed or now - self._last_read[ch] >= period:
                    full = now - self._last_full[ch] >= _FULL_READ_S
                    self._read_back(ch, ch in pushed, full)
                    self._last_read[ch] = now
                    if full:
                        self._last_full[ch] = now
        except Exception as exc:                  # never let the worker die
            error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                if self._ops:                     # a queued operation cannot finish
                    self._op_done, self._op_ok = self._ops[-1][0], False
                    self._ops.clear()
        if error != self._last_error:
            if error:
                self._emit("error", f"hardware: {error}")
            elif self._last_error:
                self._emit("info", "hardware answering again")
            self._last_error = error
        snap = self._build_snapshot(error)
        with self._lock:
            self._snapshot = snap                 # one assignment = atomic swap

    def _new_request(self) -> bool:
        """Has a setter asked for something the worker has not pushed yet?"""
        with self._lock:
            return any(self._gen[c] != self._applied_gen[c] for c in self.channels)

    def _push(self, ch: str) -> bool:
        """Send the backend whatever differs from what it holds. Returns True
        if anything was sent.

        ORDER IS SAFETY:
          * an output being switched OFF goes off FIRST; one being switched ON
            gets its whole setting BEFORE it opens -- it never drives the old one;
          * a waveform change goes BEFORE the frequency when the frequency rises
            and AFTER it when it falls, so the instrument never holds a
            frequency the waveform cannot make (a ramp is limited to 1 MHz);
          * of amplitude and offset, the change that LOWERS the peak goes first,
            so the output never passes the peak limit on the way between two
            settings that are both inside it.
        """
        i = _index(ch)
        b = self.backend
        with self._lock:
            want = dict(self._want[ch])
            gen = self._gen[ch]
            force_off = self._force_off[ch]
            self._force_off[ch] = False
        have = self._applied[ch] or {}
        if have == want and not force_off and not self._force_phase.get(ch):
            self._applied_gen[ch] = gen
            return False
        sent = False
        if not want["output"] and (have.get("output") or force_off or "output" not in have):
            b.set_output(i, False)
            sent = True
        if want["load_ohm"] != have.get("load_ohm") and self.caps.get("load_settable"):
            b.set_load(i, want["load_ohm"])
            # The AFG rescales the volts it SHOWS for the new load (the output
            # stage is unchanged: the same wire, a new meaning). Take what it
            # now reports as both held and desired -- pushing the old number
            # back would change the real output, which nobody asked for.
            got = b.read_channel(i, full=True)
            with self._lock:
                for k in ("amplitude_Vpp", "offset_V"):
                    if got.get(k) is not None:
                        have[k] = want[k] = got[k]
                        self._want[ch][k] = got[k]
                self._asked.pop(ch, None)            # rescaled by the load change
                self._to_cfg(ch)
            have["load_ohm"] = want["load_ohm"]
            sent = True
        wf_changes = want["waveform"] != have.get("waveform") and want["waveform"] != "arb"
        has_freq = want["waveform"] not in ("dc", "noise")
        f_changes = has_freq and want["frequency_Hz"] != have.get("frequency_Hz")
        if wf_changes and f_changes and want["frequency_Hz"] < (have.get("frequency_Hz") or 0):
            b.set_frequency(i, want["frequency_Hz"])
            f_changes = False
        if wf_changes:
            b.set_waveform(i, want["waveform"])
        if f_changes:
            b.set_frequency(i, want["frequency_Hz"])
        a_new, o_new = want["amplitude_Vpp"], want["offset_V"]
        a_old, o_old = have.get("amplitude_Vpp"), have.get("offset_V")
        a_changes = want["waveform"] != "dc" and a_new != a_old
        o_changes = o_new != o_old
        if a_changes and o_changes and a_old is not None and o_old is not None:
            # two ways to get there; take the one whose halfway point is lower
            amp_first = (waveforms.peak("sine", a_new, o_old)
                         <= waveforms.peak("sine", a_old, o_new))
            if amp_first:
                b.set_amplitude(i, a_new); b.set_offset(i, o_new)
            else:
                b.set_offset(i, o_new); b.set_amplitude(i, a_new)
        else:
            if a_changes:
                b.set_amplitude(i, a_new)
            if o_changes:
                b.set_offset(i, o_new)
        force_phase = self._force_phase.pop(ch, False)
        if has_freq and (want["phase_deg"] != have.get("phase_deg") or force_phase):
            b.set_phase(i, self.phase_to_send(want["phase_deg"]))
            if self._follows():
                self._align_pending = True
        if want["duty_pct"] != have.get("duty_pct") and want["waveform"] == "pulse":
            b.set_duty(i, want["duty_pct"])
        if (want["symmetry_pct"] != have.get("symmetry_pct") and want["waveform"] == "ramp"
                and self.caps.get("ramp_symmetry", True)):
            b.set_symmetry(i, want["symmetry_pct"])
        if want["output"] and not have.get("output"):
            b.set_output(i, True)
        if f_changes or wf_changes:
            if self._follows():
                self._align_pending = True
        # what the instrument now holds (duty/symmetry only where they apply,
        # so a later change of waveform still sends them)
        applied = dict(want)
        if want["waveform"] != "pulse":
            applied["duty_pct"] = have.get("duty_pct", want["duty_pct"])
        if want["waveform"] != "ramp":
            applied["symmetry_pct"] = have.get("symmetry_pct", want["symmetry_pct"])
        self._applied[ch] = applied
        self._applied_gen[ch] = gen
        return True

    def _run_ops(self) -> None:
        with self._lock:
            ops, self._ops = self._ops, []
            align = self._align_pending
            self._align_pending = False
        if (align or any(k == "align_phase" for _, k in ops)) \
                and self.caps.get("phase_align") and len(self.channels) > 1:
            self.backend.align_phase()
        if ops:
            with self._lock:
                self._op_done, self._op_ok = ops[-1][0], True

    def _read_back(self, ch: str, just_pushed: bool, full: bool = True) -> None:
        """Read the channel and compare.

        * after OUR push: the read-back must agree with what was asked; if
          not, the instrument coerced it -> `<ch>_mismatch` names it and the
          channel stays unsettled (a waiting scan times out with the reason);
        * with nothing pushed: a value that changed since the LAST read-back
          was changed at the front panel -> adopted (the instrument is the
          truth), with an info event.
        """
        got = self.backend.read_channel(_index(ch), full=full)
        if got.get("unread"):
            return                              # a partial read decides nothing
        # A knob in "not_read_back" is simply absent from `got`: the checks
        # below only compare keys that ARE there, so it never makes a
        # mismatch, and its value stays the one last set from here.
        with self._lock:
            self._not_read_back[ch] = list(got.get("not_read_back") or [])
        with self._lock:
            want = self._want[ch]
            prev = self._readback[ch] or {}
            gen = self._applied_gen[ch]
            pending = self._gen[ch] != gen
        if (not just_pushed and not pending and prev and not self.ramping()
                and time.monotonic() >= self._ramp_quiet_until):
            # compared with the PREVIOUS read-back, at the instrument's own
            # resolution: only a real change counts, never a re-reading
            changed = [k for k in _KNOBS + ("output", "mode")
                       if k in got and k in prev
                       and not _same(k, prev[k], got[k], self._phase_tol())]
            # a phase the instrument holds that is the SAME angle as the
            # setpoint (270 for an asked -90) is not a change at the panel
            if "phase_deg" in changed and _same("phase_deg", want.get("phase_deg"),
                                               got["phase_deg"], self._phase_tol()):
                changed.remove("phase_deg")
            if changed:
                with self._lock:
                    for k in changed:
                        self._want[ch][k] = got[k]
                        if self._applied[ch] is not None:
                            self._applied[ch][k] = got[k]
                    self._to_cfg(ch)
                    self._seen = self._cfg_snapshot()
                    self._asked.pop(ch, None)    # the instrument's values now
                self._emit("info", f"{ch.upper()}: changed at the instrument: "
                                   + ", ".join(f"{k} = {got[k]}" for k in changed))
                self._coupling_after_panel(ch, changed)
        with self._lock:
            want = self._want[ch]
            bad = [k for k in _relevant(want)
                   if k in got and not _same(k, want[k], got[k], self._phase_tol())]
            # merge: a quick read leaves the slow fields (load, mode) out; keep
            # their last values, so the next full read compares against them
            self._readback[ch] = {**(self._readback[ch] or {}), **got}
            self._read_gen[ch] = gen
            text = ", ".join(f"{k}: asked {want[k]}, instrument {got[k]}" for k in bad)
            # The channel is unsettled AT ONCE (a scan never measures on a
            # mismatch), but the WARNING waits for the same mismatch on the
            # next read-back too: right after a fast output toggle the read can
            # come before the AFG has applied OUTP:STAT (lab PC 2026-10-06:
            # "asked True, instrument False", then fine).
            seen = self._mismatch_seen.get(ch, ("", 0))
            count = seen[1] + 1 if (text and text == seen[0]) else (1 if text else 0)
            self._mismatch_seen[ch] = (text, count)
            self._mismatch[ch] = text
        if text and count == 2:
            self._emit("warn", f"{ch.upper()}: instrument does not hold the request -- {text}")

    # ---- snapshot --------------------------------------------------------

    def _build_snapshot(self, error: str) -> dict:
        with self._lock:
            want = {ch: dict(self._want[ch]) for ch in self.channels}
            gen = dict(self._gen)
            mismatch = dict(self._mismatch)
            not_read_back = {ch: list(self._not_read_back[ch]) for ch in self.channels}
            readback = {ch: dict(self._readback[ch] or {}) for ch in self.channels}
            read_gen = dict(self._read_gen)
            op_done, op_ok = self._op_done, self._op_ok
            ops_waiting = bool(self._ops) or self._align_pending
        snap = {"connected": self._connected,
                "idn": self._idn if self._connected else "",
                "model": self.caps.get("model", ""),
                "channels": list(self.channels),
                "hw_error": error,
                "follow": self._follows(),
                "phase_follow": self._phase_follows(),
                # the SETTING (the effective phase_follow is False while the
                # frequency does not follow)
                "phase_follow_set": bool(getattr(self.cfg.coupling, "ch2_phase_follows", True)),
                "phase_offset_deg": float(self.cfg.coupling.phase_offset_deg),
                "op_id": op_done, "op_ok": bool(op_ok and not ops_waiting)}
        all_off = self._connected and not error
        for ch in self.channels:
            w = want[ch]
            applied_now = (self._connected and self._applied[ch] is not None
                           and self._applied_gen[ch] == gen[ch])
            settled = (applied_now and read_gen[ch] == gen[ch] and not error
                       and not mismatch[ch])
            rb = readback[ch]
            env = self.envelope(ch)
            for k in ("output", "waveform", "frequency_Hz", "amplitude_Vpp", "offset_V",
                      "phase_deg", "duty_pct", "symmetry_pct"):
                snap[f"{ch}_{k}"] = w[k]
            load = w["load_ohm"]
            snap[f"{ch}_load_ohm"] = load
            # the load as describe's enum option: "50", "high-Z" or another value
            snap[f"{ch}_load"] = "high-Z" if load is None else f"{load:g}"
            snap[f"{ch}_mode"] = w.get("mode", "continuous")
            snap[f"{ch}_peak_V"] = waveforms.peak(w["waveform"], w["amplitude_Vpp"], w["offset_V"])
            snap[f"{ch}_peak_max_V"] = env["peak_max_V"]
            snap[f"{ch}_frequency_actual_Hz"] = rb.get("frequency_Hz", _NAN)
            snap[f"{ch}_amplitude_actual_Vpp"] = rb.get("amplitude_Vpp", _NAN)
            snap[f"{ch}_offset_actual_V"] = rb.get("offset_V", _NAN)
            snap[f"{ch}_settled"] = bool(settled)
            snap[f"{ch}_mismatch"] = mismatch[ch]
            # e.g. "symmetry_pct": shown as last set from here, not read back
            snap[f"{ch}_not_read_back"] = ", ".join(not_read_back[ch])
            if not (applied_now and not w["output"] and rb.get("output") is False):
                all_off = False
        snap["all_off"] = bool(all_off)
        # the firmware probe at open, readable any time (not only as a start-up event)
        snap["probe_report"] = list(self._probe_lines)
        return snap

    @staticmethod
    def _describe_setting(snap: dict, ch: str) -> str:
        wf = snap[f"{ch}_waveform"]
        out = "ON" if snap[f"{ch}_output"] else "off"
        if wf == "dc":
            return f"DC {snap[f'{ch}_offset_V']:g} V, output {out}, load {snap[f'{ch}_load']}"
        return (f"{wf} {snap[f'{ch}_frequency_Hz']:g} Hz, {snap[f'{ch}_amplitude_Vpp']:g} Vpp, "
                f"offset {snap[f'{ch}_offset_V']:g} V, output {out}, load {snap[f'{ch}_load']}")

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
