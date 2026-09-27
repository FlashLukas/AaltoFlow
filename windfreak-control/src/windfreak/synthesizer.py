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
"""

from __future__ import annotations

import threading
import time

from .backends.base import DualSynth
from .config import Config, REFERENCE_SOURCES

#: channel names on the wire and in the GUI, and the instrument's numbering
CHANNELS = ("a", "b")
_INDEX = {"a": 0, "b": 1}

_KNOBS = ("frequency_Hz", "power_dBm", "phase_deg")


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
        # DESIRED state, per channel. RF always starts OFF (safety).
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
        # The backend was BUILT with this flag (it decides what "RF off" sends),
        # so the brain must keep the value it was built with. Reading it live
        # from cfg would let a set_config change the snapshot's idea of the PLL
        # while the hardware still does the old thing. The hardware group
        # therefore takes effect at the next service start.
        self._pll_off_when_off = bool(self.cfg.hardware.pll_off_when_rf_off)
        self._snapshot = self._build_snapshot({}, "", float("nan"))
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Open the backend (which silences both outputs), program the
        start-up values, and start the worker. RF stays OFF."""
        if self._thread is not None:
            return
        self.backend.open()
        self._connected = True
        self._stop.clear()
        self._push_changes()                  # program everything once, here
        self._thread = threading.Thread(target=self._worker, name="synth-worker",
                                        daemon=True)
        self._thread.start()
        self._emit("info", f"connected: {self.backend.idn() or 'SynthHD'}  (RF off)")

    def shutdown(self) -> None:
        """Both outputs off, disconnect. Safe to call more than once / on a crash."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._lock:
            for ch in CHANNELS:
                self._want[ch]["rf_on"] = False
        was_connected = self._connected
        try:
            if self._connected:
                for ch in CHANNELS:
                    self.backend.set_output(_INDEX[ch], False)
        finally:
            try:
                self.backend.close()
            finally:
                self._connected = False
                with self._lock:
                    self._applied = {"a": None, "b": None, "ref": None}
                    self._snapshot = self._build_snapshot({}, "", float("nan"))
                if was_connected:
                    self._emit("info", "disconnected (RF off)")

    # ---- commands (each clamps, stores, wakes the worker) ------------------

    def set_rf(self, ch, on: bool) -> None:
        ch = parse_channel(ch)
        with self._lock:
            self._want[ch]["rf_on"] = bool(on)
            self._gen[ch] += 1
        self._wake.set()
        self._emit("info", f"{ch.upper()}: RF {'ON' if on else 'OFF'}")

    def all_rf_off(self) -> None:
        for ch in CHANNELS:
            self.set_rf(ch, False)

    def set_frequency(self, ch, hz: float) -> None:
        lim = self.cfg.limits
        self._set(ch, "frequency_Hz", float(hz), lim.freq_min_Hz, lim.freq_max_Hz,
                  "frequency", "Hz")

    def set_power(self, ch, dBm: float) -> None:
        lim = self.cfg.limits
        self._set(ch, "power_dBm", float(dBm), lim.power_min_dBm, lim.power_max_dBm,
                  "power", "dBm")

    def set_phase(self, ch, deg: float) -> None:
        lim = self.cfg.limits
        self._set(ch, "phase_deg", float(deg), lim.phase_min_deg, lim.phase_max_deg,
                  "phase", "deg")

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
        """The latest snapshot (a copy). Never touches hardware."""
        with self._lock:
            return dict(self._snapshot)

    # ---- settings (Settings dialog / wire use these) ---------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp the desired state to the (possibly new) limits and pick up
        the reference group. Called after set_config edits self.cfg in place.
        The channel groups are START-UP values and are not re-pushed here."""
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
            self._push_changes()
            for ch in CHANNELS:
                i = _INDEX[ch]
                readings[ch] = {
                    "locked": bool(self.backend.read_locked(i)),
                    "leveled": bool(self.backend.read_leveled(i)),
                    "frequency_actual_Hz": float(self.backend.read_frequency(i)),
                }
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
        """
        for ch in CHANNELS:
            i = _INDEX[ch]
            with self._lock:
                want = dict(self._want[ch])
                gen = self._gen[ch]
            have = self._applied[ch]
            if have is not None and have == want:
                self._applied_gen[ch] = gen
                continue
            have = have or {}
            if have.get("rf_on") and not want["rf_on"]:
                self.backend.set_output(i, False)
            if have.get("frequency_Hz") != want["frequency_Hz"]:
                self.backend.set_frequency(i, want["frequency_Hz"])
            if have.get("power_dBm") != want["power_dBm"]:
                self.backend.set_power(i, want["power_dBm"])
            if have.get("phase_deg") != want["phase_deg"]:
                self.backend.set_phase(i, want["phase_deg"])
            if want["rf_on"] and not have.get("rf_on"):
                self.backend.set_output(i, True)
            elif not want["rf_on"] and "rf_on" not in have:
                self.backend.set_output(i, False)      # first push: make it explicit
            self._applied[ch] = want
            self._applied_gen[ch] = gen
        with self._lock:
            want_ref = self._want_ref
            gen = self._gen["ref"]
        if self._applied["ref"] != want_ref:
            self.backend.set_reference(*want_ref)
            self._applied["ref"] = want_ref
        self._applied_gen["ref"] = gen

    def _build_snapshot(self, readings: dict, error: str, temp: float) -> dict:
        pll_off_when_off = self._pll_off_when_off
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
            pll_powered = bool(shown["rf_on"]) or not pll_off_when_off
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
            snap[f"{ch}_pll_on"] = pll_powered and self._connected
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
