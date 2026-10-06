"""The Generator: the small "brain" between the wire and the backend.

A function generator needs no control loop, but three things make it more than
"send the number":

  * SAFETY. CH1 may drive a magnet amplifier. Every request is CLAMPED to the
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
  * COUPLING. "CH2 follows CH1": CH2 gets CH1's frequency, its phase is CH1's
    plus an offset, and the two are phase-aligned after each change -- a
    synchronous trigger square next to the drive sine (the bench wiring:
    CH1 -> scope CH1, CH2 -> scope CH2 and EXT TRIG).

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

from .backends.base import WaveGen
from .config import Config, CHANNEL_NAMES
from . import waveforms

_KNOBS = ("waveform", "frequency_Hz", "amplitude_Vpp", "offset_V", "phase_deg",
          "duty_pct", "symmetry_pct", "load_ohm")
_NAN = float("nan")


def parse_channel(ch) -> str:
    """'ch1' / 'CH1' / '1' / 1 -> 'ch1';  'ch2' / 2 -> 'ch2'.
    (0-based numbers are NOT accepted: "channel 1" is CH1 on the front panel.)"""
    key = str(ch).strip().lower()
    if key in ("1", "2"):
        key = "ch" + key
    if key not in CHANNEL_NAMES:
        raise ValueError(f"unknown channel {ch!r} (use 'ch1' or 'ch2')")
    return key


def _index(ch: str) -> int:
    return CHANNEL_NAMES.index(ch)


def _clamp(v: float, lo: float, hi: float) -> tuple[float, bool]:
    if v < lo:
        return lo, True
    if v > hi:
        return hi, True
    return v, False


def _same(key: str, a, b) -> bool:
    """Does the instrument's read-back `b` agree with what was asked, `a`?
    The tolerances are the AFG's display resolution, generously: a value the
    instrument ROUNDS is fine, a value it COERCES is not."""
    if key in ("output", "waveform", "mode"):
        return a == b
    if key == "load_ohm":
        if a is None or b is None:
            return a is None and b is None
        return abs(a - b) <= 1e-3 * abs(a)
    if a is None or b is None:
        return False
    if key == "frequency_Hz":
        return abs(a - b) <= 2e-6 + 1e-9 * abs(a)
    if key == "amplitude_Vpp":
        return abs(a - b) <= 1e-4 + 5e-4 * abs(a)
    if key == "offset_V":
        return abs(a - b) <= 1e-3 + 5e-4 * abs(a)
    if key == "phase_deg":
        return abs(waveforms.wrap_phase(a - b)) <= 0.05
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
        self.backend = backend
        self.cfg = cfg or Config()
        # capabilities are a pure description (no I/O), safe to ask before open
        self.caps = dict(backend.capabilities())
        n = max(1, min(int(self.caps.get("channels", 2)), len(CHANNEL_NAMES)))
        self.channels = CHANNEL_NAMES[:n]
        self._lock = threading.RLock()
        self._want = {ch: self._from_cfg(ch) for ch in self.channels}
        self._gen = {ch: 0 for ch in self.channels}
        self._applied_gen = {ch: -1 for ch in self.channels}
        self._applied = {ch: None for ch in self.channels}   # what the instrument was sent
        self._readback = {ch: None for ch in self.channels}  # what it last reported
        self._read_gen = {ch: -1 for ch in self.channels}    # gen the read-back belongs to
        self._mismatch = {ch: "" for ch in self.channels}
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
            notes += self._adopt(ch, self.backend.read_channel(_index(ch)))
        for err in self.backend.drain_errors():
            notes.append(("warn", f"instrument error while reading its state: {err}"))
        self._idn = self.backend.idn()
        # optional backend method: what the real backend learned about its
        # firmware when it opened (queries it lacks); the simulator has none
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

    def shutdown(self) -> None:
        """Every output OFF, disconnect. Safe to call more than once / on a crash.
        (Not part of the read-only start rule: a stopping service leaves no
        output driving anything.)"""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        was_connected = self._connected
        try:
            if self._connected:
                for ch in self.channels:
                    try:
                        self.backend.set_output(_index(ch), False)
                    except Exception:
                        pass
        finally:
            try:
                self.backend.close()
            finally:
                self._connected = False
                with self._lock:
                    for ch in self.channels:
                        self._want[ch]["output"] = False
                        self._applied[ch] = None
                    self._snapshot = self._build_snapshot("")
                if was_connected:
                    self._emit("info", "disconnected (outputs off)")

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

        When amplitude and offset together pass the peak limit, the knob that
        was just SET (`yields`) is the one cut back: sweep the amplitude at a
        fixed offset and the amplitude stops at the limit; sweep the offset and
        the offset does. Nobody's other setting changes behind their back."""
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
            if w["amplitude_Vpp"] / 2 > room and yields == "amplitude_Vpp":
                w["amplitude_Vpp"] = max(env["amp_min_Vpp"], 2 * room)
                notes.append(f"amplitude {w['amplitude_Vpp']:g} Vpp (peak limit "
                             f"{env['peak_max_V']:g} V)")
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
        w["phase_deg"] = waveforms.wrap_phase(w["phase_deg"])
        return notes

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
            notes = self._fit(ch, w, "offset_V" if "offset_V" in values else "amplitude_Vpp")
            self._want[ch] = w
            self._gen[ch] += 1
            self._to_cfg(ch)
            if self._follows() and ch == "ch1":
                self._mirror_to_ch2(notes_out=notes)
        self._wake.set()
        if notes:
            self._emit("warn", f"{ch.upper()}: {what} clamped -> " + ", ".join(notes))
        else:
            self._emit("info", f"{ch.upper()}: {what}")

    def _refuse_if_following(self, ch: str, what: str) -> None:
        if self._follows() and parse_channel(ch) == "ch2":
            raise ValueError(f"CH2 follows CH1: set the {what} on CH1 (or switch "
                             f"'CH2 follows CH1' off)")

    def set_output(self, ch, on: bool) -> None:
        ch = parse_channel(ch)
        if not on:
            with self._lock:
                self._force_off[ch] = True       # really send it, even if "already off"
        self._change(ch, f"output {'ON' if on else 'OFF'}", output=bool(on))

    def set_waveform(self, ch, waveform: str) -> None:
        waveform = str(waveform).strip().lower()
        if waveform not in self.caps.get("waveforms", ()):
            raise ValueError(f"unknown waveform {waveform!r} (use one of "
                             f"{', '.join(self.caps.get('waveforms', ()))})")
        self._change(ch, f"waveform {waveform}", waveform=waveform)

    def set_frequency(self, ch, hz: float) -> None:
        self._refuse_if_following(ch, "frequency")
        self._change(ch, f"frequency {float(hz):g} Hz", frequency_Hz=float(hz))

    def set_amplitude(self, ch, vpp: float) -> None:
        self._change(ch, f"amplitude {float(vpp):g} Vpp", amplitude_Vpp=float(vpp))

    def set_offset(self, ch, volts: float) -> None:
        self._change(ch, f"offset {float(volts):g} V", offset_V=float(volts))

    def set_phase(self, ch, deg: float) -> None:
        self._refuse_if_following(ch, "phase")
        self._change(ch, f"phase {float(deg):g} deg", phase_deg=float(deg))

    def set_duty(self, ch, pct: float) -> None:
        self._change(ch, f"duty {float(pct):g} %", duty_pct=float(pct))

    def set_symmetry(self, ch, pct: float) -> None:
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

    def set_follow(self, on: bool, phase_offset_deg: float | None = None) -> None:
        """Switch "CH2 follows CH1" on/off, optionally with a new offset."""
        if len(self.channels) < 2:
            raise ValueError("needs two channels")
        notes = []
        with self._lock:
            co = self.cfg.coupling
            co.ch2_follows_ch1 = bool(on)
            if phase_offset_deg is not None:
                co.phase_offset_deg = waveforms.wrap_phase(float(phase_offset_deg))
            if on:
                self._mirror_to_ch2(notes_out=notes)
            self._seen = self._cfg_snapshot()
        self._wake.set()
        self._emit("info", f"CH2 follows CH1: {'ON' if on else 'off'}"
                           + (f", phase offset {self.cfg.coupling.phase_offset_deg:g} deg"
                              if on else ""))
        if notes:
            self._emit("warn", "CH2 clamped -> " + ", ".join(notes))

    def set_phase_offset(self, deg: float) -> None:
        self.set_follow(self._follows(), deg)

    def _follows(self) -> bool:
        return len(self.channels) > 1 and bool(self.cfg.coupling.ch2_follows_ch1)

    def _mirror_to_ch2(self, notes_out: list) -> None:
        """CH2 takes CH1's frequency and CH1's phase + offset; ask for an
        alignment. Called with the lock held."""
        w1, w2 = self._want["ch1"], dict(self._want["ch2"])
        w2["frequency_Hz"] = w1["frequency_Hz"]
        w2["phase_deg"] = waveforms.wrap_phase(w1["phase_deg"]
                                               + self.cfg.coupling.phase_offset_deg)
        notes_out += self._fit("ch2", w2)
        if w2 != self._want["ch2"]:
            self._want["ch2"] = w2
            self._gen["ch2"] += 1
            self._to_cfg("ch2")
        self._align_pending = True

    def outputs_off(self) -> int:
        """Every output OFF (the safety action). Returns the operation number."""
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
        """Is "CH2 follows CH1" on?"""
        return self._follows()

    # ---- status ----------------------------------------------------------

    def status(self) -> dict:
        """The latest snapshot (a copy). Never touches hardware."""
        with self._lock:
            return dict(self._snapshot)

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
                            cur["coupling"]["phase_offset_deg"])
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
            for ch in self.channels:
                self._read_back(ch, ch in pushed)
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
        if have == want and not force_off:
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
            got = b.read_channel(i)
            with self._lock:
                for k in ("amplitude_Vpp", "offset_V"):
                    if got.get(k) is not None:
                        have[k] = want[k] = got[k]
                        self._want[ch][k] = got[k]
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
        if has_freq and want["phase_deg"] != have.get("phase_deg"):
            b.set_phase(i, want["phase_deg"])
            if self._follows():
                self._align_pending = True
        if want["duty_pct"] != have.get("duty_pct") and want["waveform"] == "pulse":
            b.set_duty(i, want["duty_pct"])
        if want["symmetry_pct"] != have.get("symmetry_pct") and want["waveform"] == "ramp":
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

    def _read_back(self, ch: str, just_pushed: bool) -> None:
        """Read the channel and compare.

        * after OUR push: the read-back must agree with what was asked; if
          not, the instrument coerced it -> `<ch>_mismatch` names it and the
          channel stays unsettled (a waiting scan times out with the reason);
        * with nothing pushed: a value that changed since the LAST read-back
          was changed at the front panel -> adopted (the instrument is the
          truth), with an info event.
        """
        got = self.backend.read_channel(_index(ch))
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
        if not just_pushed and not pending and prev:
            changed = [k for k in _KNOBS + ("output", "mode")
                       if k in got and not _same(k, prev.get(k), got[k])]
            if changed:
                with self._lock:
                    for k in changed:
                        self._want[ch][k] = got[k]
                        if self._applied[ch] is not None:
                            self._applied[ch][k] = got[k]
                    self._to_cfg(ch)
                    self._seen = self._cfg_snapshot()
                self._emit("info", f"{ch.upper()}: changed at the instrument: "
                                   + ", ".join(f"{k} = {got[k]}" for k in changed))
        with self._lock:
            want = self._want[ch]
            bad = [k for k in _relevant(want) if k in got and not _same(k, want[k], got[k])]
            self._readback[ch] = dict(got)
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
