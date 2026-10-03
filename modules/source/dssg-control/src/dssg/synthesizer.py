"""The Synthesizer: the small "brain" between the wire and the backend.

A CW signal generator needs no control loop, so this is a set-and-forget brain:

  * hold the DESIRED signal (frequency, power, phase, reference, RF on/off),
  * CLAMP every request to the intersection of YOUR safety envelope (cfg
    limits) and the UNIT's own range (read at connect), and announce a clamp as
    a warning event, so nothing silently drives the sample harder than asked,
  * push the accepted value to whichever backend is wired in (sim or real),
  * keep a status SNAPSHOT that a worker thread rebuilds from the instrument's
    READ-BACK, so a scan waiting for "echo" waits for the box, not for our
    memory of what we asked.

Start-up ADOPTS (Lukas's rule, 2026-09-27: "all modules should read the
instrument state on startup, not to change anything"). start() reads RF on/off,
frequency, power, phase and reference from the unit and takes them as the
desired values; it writes nothing that changes the unit. If the unit sits
outside your limits it is LEFT there with a warning -- the next setter clamps.
The config's `signal` preset and the buzzer/display preferences are sent only
when they CHANGE (apply_config), never at start. Shutdown still turns RF off.

Threads (gotcha #1 in docs/DEVELOPER_NOTES.md)
----------------------------------------------
Two threads touch this object: the service's command thread (setters) and our
own poll thread (read-back). Rules:
  * every backend call happens under ONE lock (`self._io`), because a serial
    line or TCP socket carries one question and one answer at a time;
  * setters change brain ATTRIBUTES (the desired values) and the hardware;
    they never write into the snapshot;
  * the poll thread builds a NEW Status object each cycle and swaps it in with
    a single assignment, so a reader never sees half an update;
  * status() only returns that snapshot: it never touches the hardware, so a
    slow or unplugged instrument cannot stall the publisher or a GUI.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass

from .backends.base import MicrowaveSource
from .config import Config, REFERENCES


@dataclass
class Status:
    """One snapshot of the generator, for status() and the wire."""

    rf_on: bool = False
    frequency_Hz: float = 0.0
    power_dBm: float = 0.0
    phase_deg: float = 0.0
    reference: str = "auto"
    ext_ref_detected: bool = False
    usb_volts: float = 0.0
    connected: bool = False
    has_phase: bool = False
    idn: str = ""
    hw_error: str = ""
    # the effective (cfg AND instrument) envelope, published so a client can
    # draw the right slider without a second round trip
    freq_min_Hz: float = 0.0
    freq_max_Hz: float = 0.0
    power_min_dBm: float = 0.0
    power_max_dBm: float = 0.0
    polls: int = 0                  # increments every read-back cycle


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


class Synthesizer:
    def __init__(self, backend: MicrowaveSource, cfg: Config | None = None):
        self.backend = backend
        self.cfg = cfg or Config()
        s = self.cfg.signal
        # the DESIRED signal. Before start() these are only placeholders from
        # the preset (shown while offline); start() replaces them with what
        # the unit is actually doing (adopt, never push).
        self._freq = float(s.frequency_Hz)
        self._power = float(s.power_dBm)
        self._phase = float(s.phase_deg)
        self._reference = s.reference if s.reference in REFERENCES else "auto"
        self._rf_on = False                 # adopted at start; never switched ON by us
        # The config values the brain has already acted on. apply_config()
        # compares against this, so only a value the user CHANGED is sent --
        # pressing Apply in Settings (which sends the whole config) must not
        # overwrite the adopted instrument state with the stale preset.
        self._seen = self._cfg_snapshot()
        # what the unit itself can do; unknown (None) until start()
        self._unit_freq: tuple[float, float] | None = None
        self._unit_power: tuple[float, float] | None = None
        self._has_phase = False
        self._idn = ""
        self._connected = False

        self._io = threading.RLock()        # guards every backend call
        self._stop = threading.Event()
        self._poke = threading.Event()      # "read back NOW": set after a command
        self._poll_t: threading.Thread | None = None
        self._status = self._offline_snapshot()
        self._last_err_check = 0.0
        self._last_reopen = -1e9            # see REOPEN_S / _poll_once
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- the effective envelope ------------------------------------------

    def limits(self) -> dict:
        """cfg limits intersected with the unit's own range. Read by describe,
        info, the GUI and every setter -- one definition, no copies."""
        lim = self.cfg.limits
        f_lo, f_hi = lim.freq_min_Hz, lim.freq_max_Hz
        p_lo, p_hi = lim.power_min_dBm, lim.power_max_dBm
        if self._unit_freq:
            f_lo, f_hi = max(f_lo, self._unit_freq[0]), min(f_hi, self._unit_freq[1])
        if self._unit_power:
            p_lo, p_hi = max(p_lo, self._unit_power[0]), min(p_hi, self._unit_power[1])
        # A config that does not overlap the unit at all would give lo > hi;
        # collapse to the lower bound rather than produce an inverted range.
        f_hi, p_hi = max(f_hi, f_lo), max(p_hi, p_lo)
        return {"freq_min_Hz": f_lo, "freq_max_Hz": f_hi,
                "power_min_dBm": p_lo, "power_max_dBm": p_hi,
                "phase_min_deg": lim.phase_min_deg, "phase_max_deg": lim.phase_max_deg}

    def has_phase(self) -> bool:
        return self._has_phase

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Open the backend, learn what the unit can do, ADOPT what it is
        doing (read only -- nothing is changed), start the read-back thread."""
        try:
            self._connect_and_adopt()
        except Exception:
            # Half-way failures (port opened, then a query timed out) must not
            # leave the COM port / socket held open by a process that is about
            # to exit with a traceback: release it. rf_off=False: we never took
            # control, so the unit is left exactly as we found it.
            self._connected = False
            try:
                with self._io:
                    self.backend.close(rf_off=False)
            except Exception:
                pass
            raise
        self._emit("info", f"connected: {self._idn or 'SG12000L'}; adopted "
                           f"RF {'ON' if self._rf_on else 'off'}, "
                           f"{self._freq / 1e6:.6f} MHz, {self._power:g} dBm, "
                           f"ref {self._reference} (nothing changed)")
        self._warn_if_outside_limits()
        self._seen = self._cfg_snapshot()       # the config as it stood at connect
        self._poll_once()                       # a valid snapshot before anyone asks
        self._stop.clear()
        self._poll_t = threading.Thread(target=self._poller, name="dssg-poll",
                                        daemon=True)
        self._poll_t.start()

    def _connect_and_adopt(self) -> None:
        """Queries only. The desired values become what the unit reports, so
        the GUI, describe and a scan's first 'echo' all agree with the box."""
        with self._io:
            b = self.backend
            b.open()                            # connects; changes nothing
            self._connected = True
            self._idn = b.idn()
            try:
                self._unit_freq = tuple(b.freq_range())
                self._unit_power = tuple(b.power_range())
            except Exception as exc:            # keep going on the cfg envelope
                self._emit("warn", f"could not read the unit's range: {exc}")
            self._has_phase = bool(b.has_phase())
            self._rf_on = bool(b.read_output())
            self._freq = float(b.read_frequency())
            self._power = float(b.read_power())
            if self._has_phase:
                self._phase = float(b.read_phase())
            ref = b.read_reference()
            if ref in REFERENCES:
                self._reference = ref

    def _warn_if_outside_limits(self) -> None:
        """The unit may have been left beyond YOUR envelope (e.g. +8 dBm from
        the front panel with a +5 dBm ceiling). Adopting means we do not fix
        that behind your back -- but we say so; the next set_* is clamped."""
        lim = self.limits()
        checks = [("frequency", self._freq / 1e6, lim["freq_min_Hz"] / 1e6,
                   lim["freq_max_Hz"] / 1e6, "MHz"),
                  ("power", self._power, lim["power_min_dBm"],
                   lim["power_max_dBm"], "dBm")]
        if self._has_phase:
            checks.append(("phase", self._phase, lim["phase_min_deg"],
                           lim["phase_max_deg"], "deg"))
        for name, v, lo, hi, unit in checks:
            if not lo <= v <= hi:
                self._emit("warn", f"the unit is at {name} {v:g} {unit}, outside "
                                   f"your limits {lo:g}..{hi:g} {unit}: left as "
                                   f"it is (the next set_{name} is clamped)")

    def shutdown(self) -> None:
        """RF off, disconnect. Safe to call more than once / on a crash."""
        self._stop.set()
        t, self._poll_t = self._poll_t, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        was = self._connected
        try:
            if was:
                with self._io:
                    self.backend.set_output(False)
        except Exception as exc:
            self._emit("error", f"RF off failed on shutdown: {exc}")
        finally:
            self._rf_on = False
            try:
                with self._io:
                    self.backend.close()        # rf_off=True: the backend also sends RF off
            finally:
                self._connected = False
                self._status = self._offline_snapshot()
                if was:
                    self._emit("info", "disconnected (RF off)")

    # ---- commands (each clamps, then pushes) -----------------------------

    def _push(self, fn, *args) -> None:
        """Send one command to the unit (if connected) and ask the poller for
        a fresh read-back right away."""
        if self._connected:
            with self._io:
                fn(*args)
            self._poke.set()

    def set_rf(self, on: bool) -> None:
        self._rf_on = bool(on)
        self._push(self.backend.set_output, self._rf_on)
        self._emit("info", f"RF {'ON' if self._rf_on else 'OFF'}")

    def rf_off(self) -> None:
        """RF off. Same as set_rf(False); a name of its own because over the
        wire it is the SAFETY verb a viewer may always send (net/service.py,
        control)."""
        self.set_rf(False)

    def set_frequency(self, hz: float) -> None:
        lim = self.limits()
        value, clamped = _clamp(float(hz), lim["freq_min_Hz"], lim["freq_max_Hz"])
        self._freq = value
        self._push(self.backend.set_frequency, value)
        if clamped:
            self._emit("warn", f"frequency clamped to {value / 1e6:.6f} MHz "
                               f"(limit {lim['freq_min_Hz'] / 1e6:g}.."
                               f"{lim['freq_max_Hz'] / 1e6:g} MHz)")
        else:
            self._emit("info", f"frequency = {value / 1e6:.6f} MHz")

    def set_power(self, dBm: float) -> None:
        lim = self.limits()
        value, clamped = _clamp(float(dBm), lim["power_min_dBm"], lim["power_max_dBm"])
        self._power = value
        self._push(self.backend.set_power, value)
        if clamped:
            self._emit("warn", f"power clamped to {value:g} dBm "
                               f"(limit {lim['power_min_dBm']:g}..{lim['power_max_dBm']:g})")
        else:
            self._emit("info", f"power = {value:g} dBm")

    def set_phase(self, deg: float) -> None:
        if not self._has_phase and self._connected:
            # A refusal, not a silent no-op: a scan over phase on a unit that
            # cannot do it must fail loudly, not measure 100 identical points.
            raise ValueError("this unit has no phase control")
        lim = self.limits()
        value, clamped = _clamp(float(deg), lim["phase_min_deg"], lim["phase_max_deg"])
        self._phase = value
        self._push(self.backend.set_phase, value)
        if clamped:
            self._emit("warn", f"phase clamped to {value:g} deg "
                               f"(limit {lim['phase_min_deg']:g}..{lim['phase_max_deg']:g})")
        else:
            self._emit("info", f"phase = {value:g} deg")

    def set_reference(self, mode: str) -> None:
        mode = str(mode).strip().lower()
        if mode not in REFERENCES:
            raise ValueError(f"reference must be one of {', '.join(REFERENCES)}")
        self._reference = mode
        self._push(self.backend.set_reference, mode)
        self._emit("info", f"10 MHz reference = {mode}")

    # ---- status ----------------------------------------------------------

    def status(self) -> Status:
        """The latest snapshot. Never touches the hardware (see module doc)."""
        return self._status

    def _offline_snapshot(self) -> Status:
        lim = self.limits()
        return Status(rf_on=False, frequency_Hz=self._freq, power_dBm=self._power,
                      phase_deg=self._phase, reference=self._reference,
                      connected=False, has_phase=self._has_phase,
                      freq_min_Hz=lim["freq_min_Hz"], freq_max_Hz=lim["freq_max_Hz"],
                      power_min_dBm=lim["power_min_dBm"],
                      power_max_dBm=lim["power_max_dBm"])

    def _poll_once(self) -> None:
        """Read the unit back and publish a NEW snapshot (one assignment)."""
        if not self._connected:
            return
        prev = self._status
        lim = self.limits()
        try:
            with self._io:
                b = self.backend
                st = Status(
                    rf_on=bool(b.read_output()),
                    frequency_Hz=float(b.read_frequency()),
                    power_dBm=float(b.read_power()),
                    phase_deg=float(b.read_phase()) if self._has_phase else 0.0,
                    reference=b.read_reference(),
                    ext_ref_detected=bool(b.external_ref_detected()),
                    usb_volts=float(b.usb_volts()),
                    connected=True, has_phase=self._has_phase, idn=self._idn,
                    freq_min_Hz=lim["freq_min_Hz"], freq_max_Hz=lim["freq_max_Hz"],
                    power_min_dBm=lim["power_min_dBm"],
                    power_max_dBm=lim["power_max_dBm"],
                    polls=prev.polls + 1)
                errs = []
                now = time.monotonic()
                if now - self._last_err_check >= 1.0:      # the queue, once a second
                    self._last_err_check = now
                    errs = b.errors()
        except Exception as exc:
            # Keep the last good values, but say loudly that they are stale.
            msg = f"{type(exc).__name__}: {exc}"
            if msg != prev.hw_error:
                self._emit("error", f"read-back failed: {msg}")
            st = Status(**{**prev.__dict__, "hw_error": msg, "polls": prev.polls + 1})
            errs = []
            self._try_reopen()
        else:
            if prev.hw_error:
                self._emit("info", "hardware link recovered")
        self._status = st
        for e in errs:
            self._emit("warn", f"instrument error: {e}")

    #: while reads fail, re-open the link at most this often (s)
    REOPEN_S = 3.0

    def _try_reopen(self) -> None:
        """A dead link heals itself once the unit is back.

        Found on the office PC (2026-10-01): Windows Update replaced the FTDI
        driver of the generator's USB adapter while the service ran; the COM
        port vanished and came back, and every write then failed with "Access
        is denied" -- for good, because the open port handle belonged to the
        old device. Now the brain re-opens the link (the backend's ``reopen``:
        drop the dead handle without sending, open again, read *IDN? -- it
        changes nothing on the unit) at most every REOPEN_S while reads fail.
        A failed attempt is quiet; the read-back error already says it all.
        """
        reopen = getattr(self.backend, "reopen", None)
        now = time.monotonic()
        if reopen is None or now - self._last_reopen < self.REOPEN_S:
            return
        self._last_reopen = now
        try:
            with self._io:
                reopen()
            self._emit("info", "link to the unit re-opened")
        except Exception:
            pass

    def _poller(self) -> None:
        """Read back at `poll_hz`, or at once after a command (the poke).

        Scheduled on deadlines with short time.sleep() slices rather than
        Event.wait(timeout): on Windows a timed wait sleeps at least one
        15.6 ms tick (gotcha #34)."""
        period = 1.0 / max(0.5, float(self.cfg.hardware.poll_hz))
        next_t = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_t or self._poke.is_set():
                self._poke.clear()
                self._poll_once()
                next_t = time.monotonic() + period
            time.sleep(0.005)

    # ---- settings (Settings dialog / wire use these) ---------------------

    def get_config(self) -> Config:
        return self.cfg

    def _cfg_snapshot(self) -> dict:
        """The config values whose CHANGE means "send this to the unit"."""
        hw = self.cfg.hardware
        return {"signal": asdict(self.cfg.signal),
                "limits": asdict(self.cfg.limits),
                "mute_buzzer": bool(hw.mute_buzzer),
                "display_off": bool(hw.display_off)}

    def apply_config(self) -> None:
        """Act on what CHANGED in the config. Called after set_config (or the
        Settings dialog) edited self.cfg in place.

        Only a change is an instruction. The Settings dialog and set_config
        send the WHOLE config, so treating every field as "push this" would
        overwrite the adopted instrument state with a stale preset every time
        someone changed the theme.
          * a changed `signal` preset field -> set that value (clamped),
          * changed `limits` -> re-clamp the desired signal and push any value
            that moved (a narrower ceiling must bite at once),
          * changed mute_buzzer / display_off -> *BUZZER / *DISPLAY.
        """
        now, prev = self._cfg_snapshot(), self._seen
        self._seen = now
        sig, old = now["signal"], prev["signal"]
        lim = self.limits()
        if sig["frequency_Hz"] != old["frequency_Hz"]:
            self.set_frequency(sig["frequency_Hz"])
        elif now["limits"] != prev["limits"]:
            v = _clamp(self._freq, lim["freq_min_Hz"], lim["freq_max_Hz"])[0]
            if v != self._freq:
                self.set_frequency(v)
        if sig["power_dBm"] != old["power_dBm"]:
            self.set_power(sig["power_dBm"])
        elif now["limits"] != prev["limits"]:
            v = _clamp(self._power, lim["power_min_dBm"], lim["power_max_dBm"])[0]
            if v != self._power:
                self.set_power(v)
        if self._has_phase or not self._connected:
            if sig["phase_deg"] != old["phase_deg"]:
                self.set_phase(sig["phase_deg"])
            elif now["limits"] != prev["limits"]:
                v = _clamp(self._phase, lim["phase_min_deg"], lim["phase_max_deg"])[0]
                if v != self._phase:
                    self.set_phase(v)
        if sig["reference"] != old["reference"] and sig["reference"] in REFERENCES:
            self.set_reference(sig["reference"])
        if now["mute_buzzer"] != prev["mute_buzzer"]:
            self._push(self.backend.set_buzzer, not now["mute_buzzer"])
            self._emit("info", f"buzzer {'muted' if now['mute_buzzer'] else 'on'}")
        if now["display_off"] != prev["display_off"]:
            self._push(self.backend.set_display, not now["display_off"])
            self._emit("info", f"display {'off' if now['display_off'] else 'on'}")
        if not self._connected:
            self._status = self._offline_snapshot()

    # ---- internals -------------------------------------------------------

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
