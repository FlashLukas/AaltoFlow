"""The Amplifier: the brain between the wire and the backend.

Its jobs:

  * hold the DESIRED state (gain setting, stage on/off) and the operating point
    you told it about (signal frequency, input level);
  * CLAMP every gain request to the safety envelope -- the intersection of what
    the device accepts and your `limits` -- and QUANTISE it to the device step
    (0.5 dB), announcing a clamp as a warn event;
  * ADOPT the amplifier's state at start -- read the gain and the on/off state
    the device already holds and write NOTHING (Lukas, 2026-09-27: "all modules
    should read the instrument state on startup, not to change anything"); and
    switch it OFF (and to minimum gain) on shutdown, crash or Ctrl-C;
  * own the ONE poll thread that reads the hardware, and publish a status
    snapshot built from those reads.

Threads, and why status() never touches the hardware (docs/DEVELOPER_NOTES.md
gotcha #1): a serial round trip takes milliseconds and can hang for the whole
timeout if the cable is pulled. If status() read the device, every GUI poll and
every published frame would stall with it. So a worker thread reads the device
at `hardware.poll_hz`, BUILDS a new Status object each cycle and swaps it in;
status() just returns the latest one. Setters change brain attributes (under the
hardware lock, because they also talk to the device) and never touch the
snapshot -- the next poll copies them in. A write to a snapshot that is about to
be replaced is exactly the lost-update race gotcha #1 describes.

Consequence for callers: right after set_gain(10) the status can still show the
old gain for up to one poll period. That is the suite's contract ("a reply means
accepted, not done"), and `describe` declares an `echoes` settle so scan-core
waits for the readback.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace

from . import model
from .backends.base import AmpBackend
from .config import Config


@dataclass
class Status:
    """One snapshot of the amplifier, for status() and the wire."""

    amp_on: bool = False             # stage on (READ BACK from the device)
    gain_dB: float = 0.0             # gain setting, READ BACK from the device
    gain_set_dB: float = 0.0         # gain setting we last commanded (after clamp + step)
    gain_min_dB: float = 0.0         # the live envelope (device range AND limits)
    gain_max_dB: float = 0.0
    frequency_Hz: float = 0.0        # operating point (bookkeeping)
    input_dBm: float = 0.0
    est_gain_dB: float = 0.0         # model.py estimate at frequency_Hz
    est_output_dBm: float = 0.0      # input + estimated gain, soft-compressed
    compression_dB: float = 0.0      # how far the estimate is into compression
    output_warning: bool = False     # est_output above limits.output_warn_dBm
    temperature_C: float = 0.0
    supply_V: float = 0.0
    connected: bool = False
    idn: str = ""
    hw_error: str = ""               # last failed hardware read, '' when healthy


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


class Amplifier:
    def __init__(self, backend: AmpBackend, cfg: Config | None = None):
        self.backend = backend
        self.cfg = cfg or Config()
        a = self.cfg.amp
        # desired state -- the ONLY things setters touch
        # Until start() has READ the device these are placeholders only: the
        # minimum gain, stage off. start() replaces them with what the amplifier
        # really holds (adoption); nothing here is ever written to it at start.
        self._gain_set = float(self.cfg.hardware.gain_min_dB)
        self._amp_on = False
        self._adopted = False                    # True once the device state was read
        self._adopt_failed = False               # the "could not read" event is sent once
        self._freq = float(a.frequency_Hz)
        self._input = float(a.input_dBm)
        self._connected = False
        self._idn = ""
        self._warned_output = False
        # one lock around EVERY backend call: the poll thread and the setters
        # (which run on the service's command thread) must not interleave
        # half-written serial commands with half-read replies
        self._hw = threading.RLock()
        self._snap = Status()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_err = ""
        # replaced by the service to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- the live envelope -------------------------------------------------

    def gain_range(self) -> tuple[float, float]:
        """(lowest, highest) gain setting allowed right now: the INTERSECTION of
        the device's range and the safety limits. If someone sets the limits
        outside the device range the result collapses onto the device edge
        rather than inverting."""
        hw, lim = self.cfg.hardware, self.cfg.limits
        lo = max(hw.gain_min_dB, lim.gain_min_dB)
        hi = min(hw.gain_max_dB, lim.gain_max_dB)
        if hi < lo:
            hi = lo
        return lo, hi

    def _quantise(self, dB: float) -> float:
        """Snap to the device step INSIDE the envelope. Rounding to the nearest
        step could land just above the ceiling (ceiling 10.2, step 0.5 -> 10.0 is
        fine, but ceiling 10.3 with 10.3 requested -> 10.5 is not), so we step
        back inside if that happens."""
        step = self.cfg.hardware.gain_step_dB
        lo, hi = self.gain_range()
        if step <= 0:
            return dB
        base = self.cfg.hardware.gain_min_dB        # steps count from the device's zero
        q = base + round((dB - base) / step) * step
        while q > hi + 1e-9:
            q -= step
        while q < lo - 1e-9:
            q += step
        return round(q, 6)

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Open the backend, ADOPT what the amplifier is doing, start polling.

        Queries only. Why no writes: the amplifier may be in use (driven from the
        front panel, or left running by a measurement) when the service starts,
        and a restart of the software must not change the experiment. So the
        stage stays on if it was on, and the gain stays where it was -- even
        above your safety ceiling: the ceiling limits what THIS module sets, it
        is not a reason to change the device behind your back. It is enforced
        on the next set_gain (or set_config), and a warn event says so now.
        """
        with self._hw:
            self.backend.open()
            self._connected = True
            self._idn = self.backend.idn()
        self._adopt()
        self.poll_once()                             # a real snapshot before anyone asks
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="dsamp-poll",
                                        daemon=True)
        self._thread.start()

    def _adopt(self) -> bool:
        """Read the on/off state and the gain from the device and take them as
        the brain's own (desired = actual). Returns False if the read failed;
        poll_once() then retries on every cycle until it works, so a device that
        answers late is still adopted, never overwritten with a guess."""
        try:
            with self._hw:
                on = bool(self.backend.read_output())
                gain = float(self.backend.read_gain())
                self._amp_on = on
                self._gain_set = gain
                self._adopted = True
        except Exception as exc:
            if self._adopt_failed:                   # said once; poll_once shows hw_error
                return False
            self._adopt_failed = True
            self._emit("error", f"could not read the amplifier state at start "
                                f"({type(exc).__name__}: {exc}); nothing was written, "
                                f"will retry")
            return False
        self._emit("info", f"connected: {self._idn or 'amplifier'} -- adopted its "
                           f"state: stage {'ON' if on else 'OFF'}, gain {gain:g} dB "
                           f"(nothing written)")
        lo, hi = self.gain_range()
        if gain > hi + 1e-9 or gain < lo - 1e-9:
            self._emit("warn", f"the amplifier holds {gain:g} dB, outside the allowed "
                               f"{lo:g}..{hi:g} dB -- left as found; the next gain "
                               f"setting will be clamped")
        if on:
            self._emit("warn", "the amplifier stage was already ON -- left on "
                               "(the output must be terminated, 50 ohm)")
            self._check_output_level()
        return True

    def shutdown(self) -> None:
        """Stage OFF, gain to minimum, disconnect. Safe to call twice / on a crash."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        try:
            with self._hw:
                if self._connected:
                    try:
                        self.backend.set_output(False)
                        self.backend.set_gain(self.gain_range()[0])
                    finally:
                        self._amp_on = False
                        self._gain_set = self.gain_range()[0]
        finally:
            try:
                with self._hw:
                    self.backend.close()
            finally:
                was = self._connected
                self._connected = False
                self._snap = self._offline_status()
                if was:
                    self._emit("info", "disconnected (stage OFF)")

    # ---- commands ------------------------------------------------------------

    def set_amp(self, on: bool) -> None:
        """Switch the amplifier stage on or off."""
        on = bool(on)
        with self._hw:
            self._amp_on = on
            if self._connected:
                self.backend.set_output(on)
        if on:
            # [UM] step 2: a power amplifier driving an open port reflects its own
            # power back into the output stage and can die "within seconds".
            self._emit("warn", "amplifier ON -- the output must be terminated (50 ohm)")
            self._check_output_level()
        else:
            self._emit("info", "amplifier OFF")

    def amp_off(self) -> None:
        """The panic button: stage off. Same as set_amp(False), kept as its own
        verb so a routine or a control panel can fire it with no argument."""
        self.set_amp(False)

    def set_gain(self, dB: float) -> None:
        lo, hi = self.gain_range()
        value, clamped = _clamp(float(dB), lo, hi)
        value = self._quantise(value)
        with self._hw:
            self._gain_set = value
            if self._connected:
                self.backend.set_gain(value)
        if clamped:
            self._emit("warn", f"gain clamped to {value:g} dB "
                               f"(allowed {lo:g}..{hi:g} dB; raise limits.gain_max_dB "
                               f"deliberately if you need more)")
        elif abs(value - float(dB)) > 1e-9:
            self._emit("info", f"gain = {value:g} dB (snapped to the "
                               f"{self.cfg.hardware.gain_step_dB:g} dB step)")
        else:
            self._emit("info", f"gain = {value:g} dB")
        self._check_output_level()

    def set_frequency(self, hz: float) -> None:
        """Tell the module which frequency goes through the amp (for the estimate)."""
        lim = self.cfg.limits
        value, clamped = _clamp(float(hz), lim.freq_min_Hz, lim.freq_max_Hz)
        self._freq = value
        if clamped:
            self._emit("warn", f"frequency clamped to {value/1e6:g} MHz "
                               f"(band {lim.freq_min_Hz/1e6:g}..{lim.freq_max_Hz/1e6:g} MHz)")
        else:
            self._emit("info", f"operating frequency = {value/1e6:g} MHz")
        self._check_output_level()

    def set_input_power(self, dBm: float) -> None:
        """Tell the module the input level (for the output estimate)."""
        lim = self.cfg.limits
        value, clamped = _clamp(float(dBm), lim.input_min_dBm, lim.input_max_dBm)
        self._input = value
        if clamped:
            self._emit("warn", f"input level clamped to {value:g} dBm "
                               f"(limit {lim.input_min_dBm:g}..{lim.input_max_dBm:g}; "
                               f"the amplifier's absolute maximum input is +10 dBm)")
        else:
            self._emit("info", f"input level = {value:g} dBm")
        self._check_output_level()

    # ---- status ----------------------------------------------------------------

    def status(self) -> Status:
        """The latest snapshot. Never touches the hardware (see the module doc)."""
        if not self._connected:
            return self._offline_status()
        return self._snap

    def poll_once(self) -> None:
        """Read the device once and swap in a NEW snapshot. The poll thread calls
        this; tests may call it to avoid waiting a poll period."""
        if not self._connected:
            return
        if not self._adopted:                           # start-up read failed: retry
            self._adopt()
        err = ""
        try:
            with self._hw:
                on = self.backend.read_output()
                gain = self.backend.read_gain()
                temp = self.backend.read_temperature()
                volts = self.backend.read_supply()
        except Exception as exc:                        # a failed read is a status, not a crash
            err = f"{type(exc).__name__}: {exc}"
            prev = self._snap
            on, gain, temp, volts = prev.amp_on, prev.gain_dB, prev.temperature_C, prev.supply_V
        if err and err != self._last_err:
            self._emit("error", f"hardware read failed: {err}")
        elif not err and self._last_err:
            self._emit("info", "hardware reads recovered")
        self._last_err = err
        self._snap = self._build(on, gain, temp, volts, connected=True, hw_error=err)

    # ---- settings (Settings dialog / wire use these) -------------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp the gain to the (possibly new) envelope and push it; re-clamp
        the operating point. Called after set_config edits self.cfg in place."""
        self.set_gain(self._gain_set)
        self.set_frequency(self._freq)
        self.set_input_power(self._input)

    # ---- internals -------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            self.poll_once()
            period = 1.0 / max(0.2, float(self.cfg.hardware.poll_hz))
            # time.sleep in small slices, so shutdown is not delayed a whole period
            while not self._stop.is_set() and time.monotonic() - t0 < period:
                time.sleep(0.02)

    def _build(self, on, gain, temp, volts, connected, hw_error="") -> Status:
        lo, hi = self.gain_range()
        # the estimate uses the READ-BACK gain: what the device really holds
        g, out, comp = model.estimate(gain, self._freq, self._input,
                                      self.cfg.hardware.p1db_dBm)
        return Status(
            amp_on=bool(on), gain_dB=float(gain), gain_set_dB=self._gain_set,
            gain_min_dB=lo, gain_max_dB=hi,
            frequency_Hz=self._freq, input_dBm=self._input,
            est_gain_dB=round(g, 3), est_output_dBm=round(out, 3),
            compression_dB=round(comp, 3),
            output_warning=bool(on) and out > self.cfg.limits.output_warn_dBm,
            temperature_C=float(temp), supply_V=float(volts),
            connected=connected, idn=self._idn, hw_error=hw_error,
        )

    def _offline_status(self) -> Status:
        """What we report when not connected: the desired values, no readbacks."""
        return replace(self._build(False, self._gain_set, 0.0, 0.0, connected=False),
                       idn="")

    def _check_output_level(self) -> None:
        """Warn (once per crossing) when the ESTIMATED output would exceed the
        warning level. An estimate, not a measurement -- hence a warning, not a
        refusal; the hard protection is the gain ceiling."""
        _, out, comp = model.estimate(self._gain_set, self._freq, self._input,
                                      self.cfg.hardware.p1db_dBm)
        over = self._amp_on and out > self.cfg.limits.output_warn_dBm
        if over and not self._warned_output:
            self._emit("warn", f"estimated output {out:.1f} dBm exceeds the warning "
                               f"level {self.cfg.limits.output_warn_dBm:g} dBm"
                               + (f" ({comp:.1f} dB into compression)" if comp > 0.5 else ""))
        self._warned_output = over

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)
