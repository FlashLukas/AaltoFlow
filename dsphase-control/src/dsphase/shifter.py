"""PhaseShifter: the small "brain" between the wire and the backend.

A phase shifter needs no control loop -- the unit settles in < 0.5 ms. The
brain's whole job is:

  * hold the DESIRED state (phase, attenuation, carrier frequency, output on),
  * CLAMP every request to the safety envelope and announce a clamp as a warn
    event,
  * ROUND the phase to the device step and WRAP it into the device's
    -180..+180 range before sending (see phasemath.py for why),
  * READ THE UNIT BACK on a worker thread and publish a status snapshot.

Threading, because it has bitten this suite before (gotcha #1):
  * status() NEVER touches hardware -- it returns the latest snapshot, which
    only the worker thread builds.
  * setters change brain attributes (and write the unit); they never edit the
    snapshot. The worker copies the attributes into each new snapshot.
  * every backend call happens under ONE lock, and a setter updates its
    attribute inside the same lock section as its write -- so the worker can
    never pair a fresh readback with a stale setpoint (or vice versa).

The reported `phase_deg` is the unit's READBACK, expressed in the caller's
360-degree branch (unwrap_near): ask for 270, the unit holds -90, status says
270. That is what lets a scan 0..360 use a plain "echoes" settle -- and it is
still a real confirmation, since it comes from the unit, not from our memory.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, asdict

from .backends.base import PhaseShifterBackend
from .config import Config
from .phasemath import quantize, wrap, unwrap_near, datasheet_accuracy_deg


@dataclass
class Status:
    """One snapshot of the phase shifter, for status() and the wire."""

    output_on: bool = False
    phase_deg: float = 0.0            # readback, in the commanded 360-deg branch
    phase_set_deg: float = 0.0        # what we asked for, after rounding to the step
    phase_device_deg: float = 0.0     # the raw readback, -180..+180
    attenuation_dB: float = 0.0       # readback
    attenuation_set_dB: float = 0.0
    frequency_MHz: float = 0.0        # the carrier (bookkeeping unless freq_command)
    accuracy_deg: float = 0.0         # datasheet phase accuracy at this carrier/setting
    phase_step_deg: float = 0.5
    model: str = ""
    connected: bool = False
    idn: str = ""
    hw_error: str = ""                # last failed read, "" when healthy


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


class PhaseShifter:
    def __init__(self, backend: PhaseShifterBackend, cfg: Config | None = None):
        self.backend = backend
        self.cfg = cfg or Config()
        s = self.cfg.signal
        # desired state = the config's power-on defaults, clamped and rounded
        self._phase_set = self._round_phase(self._clamped_phase(s.phase_deg)[0])
        self._att_set = self._round_att(self._clamped_att(s.attenuation_dB)[0])
        self._freq = self._clamped_freq(s.frequency_MHz)[0]
        self._output_on = bool(s.output_on)   # False unless the .ini asks for RF at start
        self._connected = False
        self._idn = ""
        # last GOOD readbacks (a failed read keeps these, flagged by hw_error)
        self._rb_phase = wrap(self._phase_set)
        self._rb_att = self._att_set
        self._rb_output = False
        self._hw_error = ""

        self._lock = threading.RLock()        # every backend call + setpoint update
        self._stop = threading.Event()
        self._kick = threading.Event()        # a setter wakes the worker early
        self._worker: threading.Thread | None = None
        self._snap = Status()
        self._snap = self._build_snapshot()   # a sane status even before start()
        # replaced by the service/GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Open the backend, push the start-up state, start the read-back worker."""
        if self._connected:
            return
        with self._lock:
            self.backend.open()
            self._connected = True
            self._idn = self.backend.idn()
            # a safe order: the carrier and the attenuation first, the output last
            self._send_frequency(self._freq)
            self.backend.set_attenuation(self._att_set)
            self.backend.set_phase(wrap(self._phase_set))
            self.backend.set_output(self._output_on)
        self._poll_once()                     # first snapshot = real readbacks
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="dsphase-poll", daemon=True)
        self._worker.start()
        self._emit("info", f"connected: {self._idn or self.cfg.device.model}")
        if self._output_on:
            # Allowed (signal.output_on in the .ini), but never silent: RF appearing
            # on a service (re)start can surprise whoever is at the setup.
            self._emit("warn", "RF output switched ON at start (signal.output_on = True)")

    def shutdown(self) -> None:
        """RF output off, stop the worker, disconnect. Safe to call twice / on a crash."""
        self._stop.set()
        self._kick.set()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=2.0)
        self._worker = None
        was = self._connected
        try:
            with self._lock:
                if self._connected:
                    try:
                        self.backend.set_output(False)
                    finally:
                        self._output_on = False
                        self._rb_output = False
        finally:
            try:
                with self._lock:
                    self.backend.close()
            finally:
                self._connected = False
                self._snap = self._build_snapshot()
                if was:
                    self._emit("info", "disconnected (RF output off)")

    # ---- commands (each clamps, rounds, then writes) -----------------------

    def set_output(self, on: bool) -> None:
        with self._lock:
            self._output_on = bool(on)
            if self._connected:
                self.backend.set_output(self._output_on)
        self._kick.set()
        self._emit("info", f"RF output {'ON' if on else 'OFF'}")

    def set_phase(self, deg: float) -> None:
        value, clamped = self._clamped_phase(deg)
        q = self._round_phase(value)
        with self._lock:
            self._phase_set = q
            if self._connected:
                self.backend.set_phase(wrap(q))
        self._kick.set()
        lim = self.cfg.limits
        if clamped:
            self._emit("warn", f"phase clamped to {q:g} deg "
                               f"(limit {lim.phase_min_deg:g}..{lim.phase_max_deg:g})")
        else:
            self._emit("info", f"phase = {q:g} deg")

    def set_attenuation(self, dB: float) -> None:
        value, clamped = self._clamped_att(dB)
        q = self._round_att(value)
        with self._lock:
            self._att_set = q
            if self._connected:
                self.backend.set_attenuation(q)
        self._kick.set()
        lim = self.cfg.limits
        if clamped:
            self._emit("warn", f"attenuation clamped to {q:g} dB "
                               f"(limit {lim.att_min_dB:g}..{lim.att_max_dB:g})")
        else:
            self._emit("info", f"attenuation = {q:g} dB")

    def set_frequency(self, mhz: float) -> None:
        value, clamped = self._clamped_freq(mhz)
        with self._lock:
            self._freq = value
            if self._connected:
                self._send_frequency(value)
        self._kick.set()
        lim = self.cfg.limits
        if clamped:
            self._emit("warn", f"frequency clamped to {value:g} MHz "
                               f"(limit {lim.freq_min_MHz:g}..{lim.freq_max_MHz:g})")
        else:
            self._emit("info", f"carrier = {value:g} MHz")

    def _send_frequency(self, mhz: float) -> None:
        """Hand the carrier to the backend, with the CURRENT frequency-command
        template. The template lives in cfg.device and can be changed live by
        set_config; a backend that copied it once at construction would keep
        sending (or not sending) the old one. Caller holds self._lock."""
        if hasattr(self.backend, "freq_command"):
            self.backend.freq_command = self.cfg.device.freq_command or ""
        self.backend.set_frequency(mhz)

    # ---- status ----------------------------------------------------------

    def status(self) -> Status:
        """The latest snapshot. Never touches hardware (gotcha #1)."""
        return self._snap

    # ---- settings (Settings dialog / wire use these) ---------------------

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-clamp and re-round the desired state to the (possibly new) limits
        and step, and push it. Called after set_config edits self.cfg IN PLACE."""
        self.set_frequency(self._freq)
        self.set_attenuation(self._att_set)
        self.set_phase(self._phase_set)

    # ---- the worker ------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            period = 1.0 / max(0.5, float(self.cfg.hardware.poll_hz))
            self._kick.wait(period)
            self._kick.clear()
            if self._stop.is_set():
                break
            self._poll_once()

    def _poll_once(self) -> None:
        """Read the unit back and publish ONE new snapshot."""
        with self._lock:
            if self._connected:
                try:
                    ph = float(self.backend.read_phase())
                    att = float(self.backend.read_attenuation())
                    out = bool(self.backend.read_output())
                    self._rb_phase, self._rb_att, self._rb_output = ph, att, out
                    if self._hw_error:
                        self._emit("info", "hardware reads recovered")
                    self._hw_error = ""
                except Exception as exc:          # never let the worker die
                    msg = f"read failed: {exc}"
                    if msg != self._hw_error:
                        self._emit("error", msg)
                    self._hw_error = msg
            # built inside the lock: readbacks and setpoints from one moment
            self._snap = self._build_snapshot()

    def _build_snapshot(self) -> Status:
        dev = self.cfg.device
        return Status(
            output_on=self._rb_output if self._connected else False,
            phase_deg=unwrap_near(self._rb_phase, self._phase_set),
            phase_set_deg=self._phase_set,
            phase_device_deg=self._rb_phase,
            attenuation_dB=self._rb_att,
            attenuation_set_dB=self._att_set,
            frequency_MHz=self._freq,
            accuracy_deg=datasheet_accuracy_deg(self._freq, self._phase_set),
            phase_step_deg=float(dev.phase_step_deg),
            model=dev.model,
            connected=self._connected,
            idn=self._idn if self._connected else "",
            hw_error=self._hw_error,
        )

    # ---- clamps and rounding -------------------------------------------

    def _clamped_phase(self, deg):
        lim = self.cfg.limits
        return _clamp(float(deg), lim.phase_min_deg, lim.phase_max_deg)

    def _clamped_att(self, dB):
        lim = self.cfg.limits
        return _clamp(float(dB), lim.att_min_dB, lim.att_max_dB)

    def _clamped_freq(self, mhz):
        lim = self.cfg.limits
        return _clamp(float(mhz), lim.freq_min_MHz, lim.freq_max_MHz)

    def _round_phase(self, deg: float) -> float:
        # Rounding can step just past a limit (359.9 -> 360.0 is fine; but a
        # limit of 359.8 would round to 360.0): pull it back one step inside.
        lim, step = self.cfg.limits, self.cfg.device.phase_step_deg
        q = quantize(deg, step)
        if q > lim.phase_max_deg:
            q = quantize(q - step, step)
        if q < lim.phase_min_deg:
            q = quantize(q + step, step)
        return q

    def _round_att(self, dB: float) -> float:
        lim, step = self.cfg.limits, self.cfg.device.att_step_dB
        q = quantize(dB, step)
        if q > lim.att_max_dB:
            q = quantize(q - step, step)
        if q < lim.att_min_dB:
            q = quantize(q + step, step)
        return q

    def _emit(self, level: str, msg: str) -> None:
        self._on_event(level, msg)


def status_dict(st: Status) -> dict:
    """Status -> plain dict (used by the wire protocol)."""
    return asdict(st)
