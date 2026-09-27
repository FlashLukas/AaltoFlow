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
from dataclasses import dataclass

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
        # the DESIRED signal (what we were asked for, after clamping)
        self._freq = float(s.frequency_Hz)
        self._power = float(s.power_dBm)
        self._phase = float(s.phase_deg)
        self._reference = s.reference if s.reference in REFERENCES else "auto"
        self._rf_on = False                 # never on at start-up
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
        """Open the backend, learn what the unit can do, push the start-up
        signal with RF OFF, and start the read-back thread."""
        try:
            self._connect_and_push()
        except Exception:
            # Half-way failures (port opened, then a query timed out) must not
            # leave the COM port / socket held open by a process that is about
            # to exit with a traceback: close it -- close() also sends RF off.
            self._connected = False
            try:
                with self._io:
                    self.backend.close()
            except Exception:
                pass
            raise
        self._emit("info", f"connected: {self._idn or 'SG12000L'} (RF off)")
        self._poll_once()                       # a valid snapshot before anyone asks
        self._stop.clear()
        self._poll_t = threading.Thread(target=self._poller, name="dssg-poll",
                                        daemon=True)
        self._poll_t.start()

    def _connect_and_push(self) -> None:
        with self._io:
            self.backend.open()                 # the backend leaves RF off
            self._connected = True
            self.backend.set_output(False)      # ...and we make sure of it
            self._idn = self.backend.idn()
            try:
                self._unit_freq = tuple(self.backend.freq_range())
                self._unit_power = tuple(self.backend.power_range())
            except Exception as exc:            # keep going on the cfg envelope
                self._emit("warn", f"could not read the unit's range: {exc}")
            self._has_phase = bool(self.backend.has_phase())
            lim = self.limits()
            self._freq = _clamp(self._freq, lim["freq_min_Hz"], lim["freq_max_Hz"])[0]
            self._power = _clamp(self._power, lim["power_min_dBm"], lim["power_max_dBm"])[0]
            self._phase = _clamp(self._phase, lim["phase_min_deg"], lim["phase_max_deg"])[0]
            self.backend.set_frequency(self._freq)
            self.backend.set_power(self._power)
            if self._has_phase:
                self.backend.set_phase(self._phase)
            self.backend.set_reference(self._reference)

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
                    self.backend.close()        # the backend also sends RF off
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
        self._status = st
        for e in errs:
            self._emit("warn", f"instrument error: {e}")

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

    def apply_config(self) -> None:
        """Re-clamp the desired signal to the (possibly new) limits and push it.
        Called after set_config edits self.cfg in place."""
        self.set_frequency(self._freq)
        self.set_power(self._power)
        if self._has_phase or not self._connected:
            self.set_phase(self._phase)
        if not self._connected:
            self._status = self._offline_snapshot()

    # ---- internals -------------------------------------------------------

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
