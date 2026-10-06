"""PhaseShifter: the small "brain" between the wire and the backend.

A phase shifter needs no control loop -- the unit settles in < 0.5 ms. The
brain's whole job is:

  * ADOPT the unit's state at start: open() and start() only QUERY the unit
    (Lukas's rule, 2026-09-27: "read the instrument state on startup, not
    change anything"). Whatever phase / attenuation / RF on-off the box holds
    becomes the brain's setpoint, so the GUI, status and describe show what the
    instrument is really doing, and a service (re)start never changes the RF
    going into the experiment,
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
    adopted: bool = False             # True once the unit's own state has been read


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
        # The desired state is NOT taken from the config any more: it is READ
        # from the unit at start (_adopt). Until then these are placeholders,
        # and the names in _to_adopt say which of them are still unknown -- a
        # placeholder is never written to the unit (see apply_config).
        self._phase_set = 0.0
        self._att_set = 0.0
        self._output_on = False
        self._to_adopt = {"phase", "att", "output"}
        # The carrier has no query on the PS6000L (it is bookkeeping unless
        # device.freq_command is set), so it starts from the config value and
        # is NOT sent at start: nothing to read, and nothing written either.
        self._freq = self._clamped_freq(s.frequency_MHz)[0]
        self._connected = False
        self._idn = ""
        # last GOOD readbacks (a failed read keeps these, flagged by hw_error)
        self._rb_phase = 0.0
        self._rb_att = 0.0
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
        """Open the backend, READ (never write) the unit's state and adopt it as
        the setpoint, start the read-back worker.

        Why no write: a service restart must not disturb an experiment that is
        running on the RF path (Lukas, 2026-09-27). The RF output in particular
        stays exactly as found -- ON if someone left it on. Only the user (a
        setter or set_config) changes the unit. Shutdown still switches the
        output off; that is a separate, deliberate rule."""
        if self._connected:
            return
        with self._lock:
            self.backend.open()               # queries only (*PING?, *IDN?)
            self._connected = True
            self._to_adopt = {"phase", "att", "output"}
            self._idn = self.backend.idn()
        self._poll_once()                     # first snapshot = real readbacks, adopted
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="dsphase-poll", daemon=True)
        self._worker.start()
        self._emit("info", f"connected: {self._idn or self.cfg.device.model}")
        if self._to_adopt:
            # The reads failed: we do NOT know what the unit holds. The worker
            # adopts it at the first good read; nothing is written meanwhile.
            self._emit("warn", "could not read the unit's state at start; it will be "
                               "adopted at the first good read (nothing was written)")

    def _adopt(self, ph: float, att: float, out: bool) -> None:
        """Make the unit's own state the desired state -- for every quantity the
        user has not set in the meantime. Caller holds self._lock.

        No rounding and no clamping: the adopted value is what the box HOLDS, so
        a scan's echo check agrees with it at once. A value outside the safety
        envelope is only announced; it is changed only when the user asks."""
        lim = self.cfg.limits
        found = []
        if "phase" in self._to_adopt:
            # The unit reports -180..+180. If the envelope is e.g. 0..360,
            # express the SAME physical phase in that branch (no write needed).
            p = ph
            if p < lim.phase_min_deg and p + 360.0 <= lim.phase_max_deg:
                p += 360.0
            elif p > lim.phase_max_deg and p - 360.0 >= lim.phase_min_deg:
                p -= 360.0
            self._phase_set = p
            found.append(f"phase {p:g} deg")
            if not (lim.phase_min_deg <= p <= lim.phase_max_deg):
                self._emit("warn", f"the unit holds phase {ph:g} deg, outside the limits "
                                   f"{lim.phase_min_deg:g}..{lim.phase_max_deg:g} (left as is)")
        if "att" in self._to_adopt:
            self._att_set = att
            found.append(f"attenuation {att:g} dB")
            if not (lim.att_min_dB <= att <= lim.att_max_dB):
                self._emit("warn", f"the unit holds attenuation {att:g} dB, outside the limits "
                                   f"{lim.att_min_dB:g}..{lim.att_max_dB:g} (left as is)")
        if "output" in self._to_adopt:
            self._output_on = bool(out)
            found.append(f"RF output {'ON' if out else 'off'}")
            if out:
                # never silent: RF is live on the downstream path
                self._emit("warn", "RF output is ON on the unit (adopted, not changed)")
        self._to_adopt.clear()
        if found:
            self._emit("info", "adopted from the unit: " + ", ".join(found))

    def shutdown(self, keep_outputs: bool = False) -> None:
        """RF output off, stop the worker, disconnect. Safe to call twice / on a crash.

        keep_outputs=True is a RESTART for a code update (Lukas 2026-10-06):
        disconnect and release the port the same, but leave the RF output as
        it is -- the next start adopts it."""
        self._stop.set()
        self._kick.set()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=2.0)
        self._worker = None
        was = self._connected
        try:
            with self._lock:
                if self._connected and not keep_outputs:
                    try:
                        self.backend.set_output(False)
                    finally:
                        self._output_on = False
                        self._rb_output = False
        finally:
            try:
                with self._lock:
                    self.backend.close(rf_off=not keep_outputs)
            finally:
                self._connected = False
                self._snap = self._build_snapshot()
                if was:
                    self._emit("info", "disconnected (RF output left as it is)"
                               if keep_outputs else "disconnected (RF output off)")

    # ---- commands (each clamps, rounds, then writes) -----------------------

    def set_output(self, on: bool) -> None:
        with self._lock:
            self._output_on = bool(on)
            self._to_adopt.discard("output")  # the user decided; never overwrite it
            if self._connected:
                self.backend.set_output(self._output_on)
        self._kick.set()
        self._emit("info", f"RF output {'ON' if on else 'OFF'}")

    def output_off(self) -> None:
        """RF output OFF. Same as set_output(False); a name of its own because
        over the wire it is the SAFETY verb a viewer may always send
        (net/service.py, control) -- set_output can also switch the RF ON."""
        self.set_output(False)

    def set_phase(self, deg: float) -> None:
        value, clamped = self._clamped_phase(deg)
        q = self._round_phase(value)
        with self._lock:
            self._phase_set = q
            self._to_adopt.discard("phase")
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
            self._to_adopt.discard("att")
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
        and step, and push it. Called after set_config edits self.cfg IN PLACE --
        i.e. only on an explicit request from the user or a coordinator.

        A quantity whose value is still UNKNOWN (the start-up read failed) is
        not pushed: its placeholder (0 deg, 0 dB = full power) was never
        anyone's choice and must not reach the unit."""
        with self._lock:
            unknown = set(self._to_adopt)
        self.set_frequency(self._freq)
        if "att" not in unknown:
            self.set_attenuation(self._att_set)
        if "phase" not in unknown:
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
                    if self._to_adopt:        # first good read since start
                        self._adopt(ph, att, out)
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
            adopted=self._connected and not self._to_adopt,
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
