"""BipolarSupply: the brain between the wire and the hardware.

An ELC ALR3203 (0..32 V, 0..6 A, 96 W, UNIPOLAR) plus a POLARITY RELAY in the
magnet leads makes a bipolar magnet supply. This brain hides that: every
setpoint is SIGNED, and the brain programs |value| and keeps the relay on the
right side. (Built 2026-11-09 for a vacuum chamber with a small magnet; it
replaces the LabVIEW VoltageMagnetSource.vi, minus the field loop -- Lukas:
"no need for any magnetic field feedback ... we just write voltage or current
and we read hall probe". The Hall probe is read by usb6001-control.)

What it does, in the order a physicist would worry about it:

  * CLAMPS every request to the safety envelope in cfg.limits (and says so).
  * RAMPS. A new setpoint is never applied as a step: the worker walks the
    output towards it at `ramp.rate_*` per second. With a coil on the output
    V = L dI/dt, so a current step would ask for an infinite voltage.
  * CHANGES SIGN SAFELY (Lukas, 2026-11-09). A setpoint on the other side of
    zero than the relay: ramp to 0, wait until the MEASURED current is below
    `polarity.zero_current_A` (the output may stay on, no extra wait), flip
    the relay, ramp up on the new side. The relay is never switched under
    current: if the current does not fall in `zero_timeout_s`, the change is
    refused and the output stays at 0. HYSTERESIS: a setpoint on the other
    side smaller than `polarity.deadband_*` is output as 0 without flipping,
    so a setpoint dithering around zero does not keep clicking the relay.
  * SWITCHES MODE WITHOUT A BUMP, even with the output live (Lukas: "when you
    switch it first lowers the previous limit to actual value and only after
    it increases the other value to a limit"). voltage -> current: program
    the current setpoint to the MEASURED current (the supply now sits on the
    CV/CC boundary), then raise the voltage setpoint to its limit -- the coil
    keeps its current, now regulated in CC. current -> voltage likewise.
  * RAMPS TO ZERO BEFORE THE OUTPUT GOES OFF, on shutdown too (a restart for
    a code update included: Lukas 2026-10-11, magnets always stop safely).
  * CHANGES NOTHING AT START. start() reads the setpoints, the output switch,
    what the supply regulates, and the relay, and adopts them.
  * MEASURES V and I at `hardware.poll_hz`, SIGNED by the relay (the supply
    itself only sees magnitudes), and says whether the supply is on its
    96 W envelope (`power_limited`) and what R = V/I is (display only:
    Lukas, "R is just the visualizer").
  * ACQUIRES for a scan: `acquire()` returns a number at once; the worker
    waits `acquisition.settle_s` and averages `acquisition.readings` fresh
    measurements into `sample`.
  * SWEEPS the current for a fly scan (ramp_current): softramp.py walks the
    setpoint; a sweep never crosses zero (that needs the relay).

THREADS (docs gotcha #1). One worker thread owns every hardware call (the
supply's serial link and the relay). Setters only change brain attributes
(under `_lock`); the worker reads them, acts, and REBUILDS the status
snapshot. `status()` returns that snapshot and never talks to the hardware.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .backends.base import PolarityRelay, SupplyBackend
from .backends.sim import NoRelay
from .config import Config, MODES
from .softramp import SoftRamp
from .stream import StreamRecorder

_NAN = float("nan")


@dataclass
class Status:
    """One snapshot of the supply, for status() and the wire. Setpoints and
    measurements are SIGNED (negative = relay inverted)."""

    mode: str = "current"
    output: bool = False             # output switch as commanded to the instrument
    output_request: bool = False     # what the user asked for (may still be ramping)
    current_set_A: float = 0.0       # TARGET in current mode (signed)
    voltage_set_V: float = 0.0       # TARGET in voltage mode (signed)
    current_limit_A: float = 0.0     # the current setpoint while in voltage mode
    voltage_limit_V: float = 0.0     # the voltage setpoint while in current mode
    programmed: float = 0.0          # the mode's quantity as programmed now (signed)
    ramping: bool = False            # True until the output has reached its target
    ramp_enabled: bool = True
    ramp_rate_A_per_s: float = 0.0
    ramp_rate_V_per_s: float = 0.0
    voltage_V: float = _NAN          # measured, live, signed by the relay
    current_A: float = _NAN          # measured, live, signed by the relay
    power_W: float = _NAN            # |V I| (the supply only sources)
    resistance_ohm: float = _NAN     # V / I, display only (NaN below 10 mA)
    regulation: str = ""             # what the supply regulates: voltage/current/off
    at_limit: bool = False           # the OTHER setpoint (or 96 W) holds the output
    power_limited: bool = False      # on the supply's power envelope
    power_max_W: float = 96.0
    # the polarity relay
    bipolar: bool = True             # False: no relay, negative setpoints refused
    relay_inverted: object = None    # True / False / None (unknown)
    polarity_switching: bool = False # waiting for zero current to flip the relay
    relay_error: str = ""
    relay_flips: int = 0             # flips done by this service since start
    acq_id: int = 0
    acquiring: bool = False
    acq_readings: int = 1
    sample: dict = field(default_factory=dict)
    connected: bool = False
    idn: str = ""
    hw_error: str = ""
    sweeping: bool = False
    ramp_id: int = 0
    sweep_target_A: float = _NAN
    sweep_rate_A_per_s: float = _NAN


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    """Return (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def _finite(value, what: str) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return v


def _side(value: float):
    """Which side of zero a value asks for: True = inverted (negative),
    False = normal (positive), None = zero (either side will do)."""
    if value < 0:
        return True
    if value > 0:
        return False
    return None


class BipolarSupply:
    def __init__(self, backend: SupplyBackend, cfg: Config | None = None,
                 relay: PolarityRelay | None = None, clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self.relay = relay if relay is not None else NoRelay()
        self._bipolar = not isinstance(self.relay, NoRelay)
        self._clock = clock
        self._hw = threading.RLock()       # serialises EVERY hardware call
        self._lock = threading.Lock()      # guards the attributes below + snapshot

        o = self.cfg.output
        # ---- what the user asked for (setters write these) -------------------
        self._mode = o.mode if o.mode in MODES else "current"
        self._mode_live: str | None = None  # a live (bumpless) mode switch pending
        self._out_req = False              # replaced by what start() finds
        self._kill = False                 # output_off_now() pending
        # The lost-client watchdog arms only once a client of THIS service has
        # driven the output (an adopted live output is never ramped down by it).
        self._wd_armed = False
        self._i_set = self._v_set = 0.0
        self._i_lim = self._v_lim = 0.0
        # ---- what the worker has applied to the hardware (worker only) --------
        self._mode_hw: str | None = None
        self._out_hw = False
        self._prog = 0.0                   # the mode's quantity as programmed (signed)
        self._lim_hw: float | None = None  # the other setpoint as programmed
        self._relay_hw = None              # True/False/None: the relay as known
        self._trust_unknown = False        # see _adopt
        self._flip_t0: float | None = None  # when waiting for zero current began
        self._relay_retry_t = -1e9
        self._relay_error = ""
        self._flips = 0
        self._t_step: float | None = None
        self._t_meas = -1e9
        self._zero_hold = 0.0
        # ---- measurements + acquisition (under _lock) ------------------------
        self._v_meas = _NAN                # MAGNITUDES as the supply reports them
        self._i_meas = _NAN
        self._regulation = ""
        self._acq_id = 0
        self._acq: dict | None = None
        self._sample: dict = {}
        # ---- bookkeeping ------------------------------------------------------
        self._connected = False
        self._idn = ""
        self._hw_error = ""
        self._last_err_emit = -1e9
        self._last_touch = self._clock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_event = lambda level, msg: None

        # THE CURRENT SWEEP (fly scans): the walk runs on its own thread and
        # calls _sweep_step every 1/ramp.step_hz s.
        self._sweep_last = _NAN
        self._sweep = SoftRamp(self._sweep_step, lambda: self._i_set,
                               limits=self.current_range,
                               dt_s=1.0 / max(1.0, float(self.cfg.ramp.step_hz)),
                               on_done=self._sweep_done, channel="commanded",
                               name="alr3203-sweep")
        self.recorder = StreamRecorder(["current", "voltage", "programmed"])

        # initial setpoints from the config, clamped (quietly: nobody asked yet)
        self._i_set = self._clamp_i(o.current_A)[0]
        self._v_set = self._clamp_v(o.voltage_V)[0]
        self._i_lim = self._clamp_ilim(o.current_limit_A)[0]
        self._v_lim = self._clamp_vlim(o.voltage_limit_V)[0]
        self._sanitise_ramp()
        self._snapshot = self._build_snapshot()

    # ---- lifecycle -----------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Open the supply and the relay, READ their state and ADOPT it, start
        the worker. `poll=False` skips the thread so a test can drive `step()`.

        Nothing is written (Lukas, 2026-09-27: "read the instrument state on
        startup, not change anything") -- the supply may be driving the magnet
        right now, and the relay stays where it is."""
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            try:
                found = self.backend.read_state()
            except Exception as exc:
                # Without the state we cannot know whether the coil is
                # energised: refuse, let go WITHOUT writing.
                try:
                    self.backend.disconnect()
                except Exception:
                    pass
                raise RuntimeError(f"could not read the supply's state at start "
                                   f"({type(exc).__name__}: {exc}); not taking "
                                   f"control of an instrument in an unknown state") from exc
            try:
                self.relay.open()
                relay_state = self.relay.read()
            except Exception as exc:
                relay_state = None
                self._relay_error = f"{type(exc).__name__}: {exc}"
            self._adopt(found, relay_state)
        self._connected = True
        self._last_touch = self._clock()
        self._t_step = None
        with self._lock:
            self._snapshot = self._build_snapshot()
        unit = "A" if self._mode == "current" else "V"
        main = self._i_set if self._mode == "current" else self._v_set
        lim_txt = (f"voltage setpoint {self._v_lim:g} V" if self._mode == "current"
                   else f"current setpoint {self._i_lim:g} A")
        rel = {True: "INVERTED", False: "normal", None: "UNKNOWN"}[self._relay_hw]
        self._emit("info", f"connected: {self._idn or 'ALR3203'}; found output "
                           f"{'ON' if self._out_hw else 'OFF'} ({found.get('regulation', '?')} "
                           f"regulation), taken as {self._mode} mode {main:g} {unit}, "
                           f"{lim_txt}; relay {rel} ({self.relay.describe()}) -- "
                           f"adopted, nothing changed")
        if self._bipolar and self._relay_hw is None:
            self._emit("warn", "polarity relay state unknown"
                               + (f" ({self._relay_error})" if self._relay_error else "")
                               + ": the sign of the output is not known; the relay is "
                                 "set explicitly (at zero current) on the next nonzero setpoint")
        note = self.envelope_note()
        if note:
            self._emit("warn", note)
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="alr3203-worker",
                                            daemon=True)
            self._thread.start()

    def _adopt(self, found: dict, relay_state) -> None:
        """Take the supply's and the relay's state as the brain's own (called
        with _hw held, before the worker exists). After this the worker sees
        nothing to do: its first step writes nothing.

        Which MODE is it? The supply has none -- it regulates whichever
        setpoint it reaches first. With the output live, what it regulates now
        IS the mode. With the output off, the setpoints tell: the one at full
        range is the limit (Lukas's convention), else the config's mode."""
        out = bool(found.get("output", False))
        v = abs(float(found.get("voltage_V", 0.0)))
        i = abs(float(found.get("current_A", 0.0)))
        if not (math.isfinite(v) and math.isfinite(i)):
            raise RuntimeError(f"the supply reported non-finite setpoints "
                               f"(VOLT {v!r}, CURR {i!r})")
        reg = str(found.get("regulation", "")).strip().lower()
        if out and reg in MODES:
            mode = reg
        elif v >= 0.99 * self.voltage_limit_max() and i < 0.99 * self.current_limit_max():
            mode = "current"
        elif i >= 0.99 * self.current_limit_max() and v < 0.99 * self.voltage_limit_max():
            mode = "voltage"
        else:
            mode = self._mode
        inverted = relay_state if self._bipolar else False
        sign = -1.0 if inverted else 1.0
        main, limit = (sign * i, v) if mode == "current" else (sign * v, i)
        o = self.cfg.output
        with self._lock:
            self._mode = mode
            self._out_req = out            # a live output stays live
            if mode == "current":
                self._i_set, self._v_lim = main, limit
            else:
                self._v_set, self._i_lim = main, limit
            self._regulation = reg if out else "off"
        self._mode_hw = mode
        self._out_hw = out
        self._prog = main                  # what IS programmed (output off: not driven)
        self._lim_hw = limit
        self._relay_hw = inverted if self._bipolar else False
        # Relay UNKNOWN (DAQ service down, level never written): the adopted
        # output must not be touched because of that -- the worker would
        # otherwise ramp it to zero at once to "set" the relay. So the unknown
        # relay is trusted as it is until a client changes the setpoint; the
        # first change then sets the relay explicitly, at zero current.
        self._trust_unknown = self._relay_hw is None
        o.mode = mode
        if mode == "current":
            o.current_A, o.voltage_limit_V = main, limit
        else:
            o.voltage_V, o.current_limit_A = main, limit
        hi_main = self.current_limit_max() if mode == "current" else self.voltage_limit_max()
        if abs(main) > hi_main + 1e-9:
            self._emit("warn", f"found setpoint {main:g} outside the envelope; "
                               f"left as it is (the next setpoint is clamped)")

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Ramp to zero, output off, disconnect. Safe to call more than once
        and from a crash handler. The ramp is sped up (never slowed) so it ends
        within `safety.shutdown_ramp_s` (the launcher kills after 8 s).
        The relay is left where it is (nothing flows through it any more).

        keep_outputs=True: disconnect without ramping (the service never asks
        for it -- a restart is a safe stop for a magnet, see net/service.py --
        but a test or script may)."""
        self._sweep.stop()
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        was = self._connected
        try:
            if was and not keep_outputs:
                self._ramp_down_blocking()
        finally:
            try:
                with self._hw:
                    self.backend.close(output_off=not keep_outputs)
            finally:
                try:
                    self.relay.close()
                except Exception:
                    pass
                self._connected = False
                if not keep_outputs:
                    self._out_hw = False
                with self._lock:
                    if not keep_outputs:
                        self._out_req = False
                    self._acq = None
                    self._snapshot = self._build_snapshot()
                if was:
                    self._emit("info", "disconnected, output left as it is"
                               if keep_outputs else "output off, disconnected")

    # ---- limits (live, also used by describe) ---------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def bipolar(self) -> bool:
        return self._bipolar

    def envelope_note(self) -> str:
        """A warning when the current or voltage envelope is not the same on
        both signs, "" otherwise. Asymmetric limits are allowed (min and max
        are two settings on purpose), but a safety cap is usually meant for
        both signs: on the Blue Box (2026-11-10) [limits] current_max_A = 1.0
        alone left -6 A allowed on the negative side, and only a test found it.
        Said once at start, so it cannot pass unnoticed."""
        if not self._bipolar:
            return ""
        lim, out = self.cfg.limits, []
        for name, lo, hi, unit in (("current", lim.current_min_A, lim.current_max_A, "A"),
                                   ("voltage", lim.voltage_min_V, lim.voltage_max_V, "V")):
            if abs(abs(float(lo)) - abs(float(hi))) > 1e-9:
                out.append(f"{name} {float(lo):g} .. +{float(hi):g} {unit}")
        if not out:
            return ""
        return ("the safety envelope is NOT the same on both signs: " + "; ".join(out)
                + " -- set [limits] current_min_A / voltage_min_V too if the cap is "
                  "meant for both directions")

    def current_range(self) -> tuple[float, float]:
        lim = self.cfg.limits
        lo, hi = float(lim.current_min_A), float(lim.current_max_A)
        return (lo if self._bipolar else max(0.0, lo)), hi

    def voltage_range(self) -> tuple[float, float]:
        lim = self.cfg.limits
        lo, hi = float(lim.voltage_min_V), float(lim.voltage_max_V)
        return (lo if self._bipolar else max(0.0, lo)), hi

    def current_limit_max(self) -> float:
        lim = self.cfg.limits
        return max(abs(float(lim.current_min_A)), abs(float(lim.current_max_A)))

    def voltage_limit_max(self) -> float:
        lim = self.cfg.limits
        return max(abs(float(lim.voltage_min_V)), abs(float(lim.voltage_max_V)))

    # ---- setters: clamp, store in brain attributes, mirror into cfg ----------

    def set_mode(self, mode: str, wait_s: float = 1.5) -> None:
        """Voltage or current mode. With the output OFF it just switches. With
        the output LIVE it is a BUMPLESS switch done by the worker (see the
        module docstring); this call waits for it (up to wait_s) so the reply
        means "done" and a set_current right after works."""
        mode = str(mode).strip().lower()
        mode = {"curr": "current", "i": "current", "cc": "current",
                "volt": "voltage", "v": "voltage", "cv": "voltage"}.get(mode, mode)
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        if mode == self._mode and self._mode_live is None:
            return
        self._stop_sweep("a mode change")
        with self._lock:
            live = self._out_req or self._out_hw
            if self._flip_t0 is not None:
                raise ValueError("the polarity relay is being switched: change the "
                                 "mode once the output has settled on its new side")
            if live:
                self._mode_live = mode
            else:
                self._mode = mode
        self.cfg.output.mode = mode
        if not live:
            self._emit("info", f"{mode} mode")
            return
        self._emit("info", f"{mode} mode requested with the output live: switching "
                           f"without a bump")
        if self._thread is None or wait_s <= 0:
            return                          # tests drive step() themselves
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            with self._lock:
                if self._mode_live is None:
                    return
            time.sleep(0.02)
        raise ValueError("the mode switch did not complete in time (no measurement?)")

    def set_output(self, on: bool) -> None:
        on = bool(on)
        if not on:
            self._stop_sweep("output off")
        with self._lock:
            self._out_req = on
            if on:
                self._wd_armed = True
                self._trust_unknown = False
        if on:
            self._emit("info", "output ON requested: ramping to the setpoint")
        else:
            self._emit("info", "output OFF requested: ramping to zero first")

    def output_off(self) -> None:
        """Output OFF the gentle way (ramp to zero, then off): the SAFETY verb
        a viewer may always send."""
        self.set_output(False)

    def output_off_now(self) -> None:
        """EMERGENCY: OUT OFF without ramping. The coil's energy then goes into
        the supply's output stage. Use set_output(False) unless the ramp itself
        is the problem."""
        self._stop_sweep("output off NOW")
        with self._lock:
            self._kill = True
            self._out_req = False
        self._emit("warn", "output OFF NOW (no ramp)")

    def set_current(self, amps: float) -> None:
        """The output current (signed) -- current mode only."""
        amps = _finite(amps, "current")
        if self._mode != "current" or self._mode_live is not None:
            raise ValueError("set_current needs current mode; in voltage mode "
                             "the current is the limit: use set_current_limit")
        self._stop_sweep("a current set")
        value, clamped = self._clamp_i(amps)
        with self._lock:
            self._i_set = value
            self._wd_armed = True
            self._trust_unknown = False   # a client asked: set the relay explicitly
        self.cfg.output.current_A = value
        lo, hi = self.current_range()
        self._report("current", value, "A", clamped, lo, hi)

    def set_voltage(self, volts: float) -> None:
        """The output voltage (signed) -- voltage mode only."""
        volts = _finite(volts, "voltage")
        if self._mode != "voltage" or self._mode_live is not None:
            raise ValueError("set_voltage needs voltage mode; in current mode "
                             "the voltage is the limit: use set_voltage_limit")
        value, clamped = self._clamp_v(volts)
        with self._lock:
            self._v_set = value
            self._wd_armed = True
            self._trust_unknown = False
        self.cfg.output.voltage_V = value
        lo, hi = self.voltage_range()
        self._report("voltage", value, "V", clamped, lo, hi)

    def set_voltage_limit(self, volts: float) -> None:
        """The voltage setpoint while in current mode (a magnitude). Lukas's
        convention: leave it at the full 32 V."""
        value, clamped = self._clamp_vlim(_finite(volts, "voltage limit"))
        with self._lock:
            self._v_lim = value
        self.cfg.output.voltage_limit_V = value
        self._report("voltage limit", value, "V", clamped, 0.0, self.voltage_limit_max())

    def set_current_limit(self, amps: float) -> None:
        """The current setpoint while in voltage mode (a magnitude). Lukas's
        convention: leave it at the full 6 A."""
        value, clamped = self._clamp_ilim(_finite(amps, "current limit"))
        with self._lock:
            self._i_lim = value
        self.cfg.output.current_limit_A = value
        self._report("current limit", value, "A", clamped, 0.0, self.current_limit_max())

    def set_ramp(self, rate_A_per_s: float | None = None,
                 rate_V_per_s: float | None = None,
                 enabled: bool | None = None) -> None:
        r, lim = self.cfg.ramp, self.cfg.limits
        if rate_A_per_s is not None:
            v, c = _clamp(_finite(rate_A_per_s, "rate"), 1e-6, lim.rate_max_A_per_s)
            r.rate_A_per_s = v
            self._report("current ramp rate", v, "A/s", c, 1e-6, lim.rate_max_A_per_s)
        if rate_V_per_s is not None:
            v, c = _clamp(_finite(rate_V_per_s, "rate"), 1e-6, lim.rate_max_V_per_s)
            r.rate_V_per_s = v
            self._report("voltage ramp rate", v, "V/s", c, 1e-6, lim.rate_max_V_per_s)
        if enabled is not None:
            r.enabled = bool(enabled)
            self._emit("warn" if not r.enabled else "info",
                       "ramp " + ("ON" if r.enabled else
                                  "OFF: setpoints are applied as steps"))

    def set_acquisition(self, readings: int) -> None:
        v = int(_clamp(int(readings), 1, 1000)[0])
        self.cfg.acquisition.readings = v
        self._emit("info", f"acquire averages {v} readings")

    def touch(self) -> None:
        """A client is alive (the service calls this on every command)."""
        self._last_touch = self._clock()

    # ---- the current SWEEP (fly scans) ------------------------------------------

    def ramp_current(self, amps: float, rate_A_per_s: float) -> int:
        """SWEEP the current to `amps` at `rate_A_per_s`; returns the sweep's
        number. Current mode only; never switches the output on.

        A sweep NEVER crosses zero: the relay has to switch there, with the
        current stopped, which a continuous sweep cannot do. Sweep to 0, then
        start a new sweep on the other side (its first step flips the relay
        through the ordinary ramp -- set_current a small value first)."""
        amps = _finite(amps, "current")
        rate = abs(_finite(rate_A_per_s, "rate"))
        if not rate > 0:
            raise ValueError("rate must be > 0")
        if self._mode != "current":
            raise ValueError("ramp_current needs current mode")
        lim = self.cfg.limits
        lo_r, hi_r = sorted((float(lim.sweep_rate_min_A_per_s), float(lim.rate_max_A_per_s)))
        r, rclamped = _clamp(rate, lo_r, hi_r)
        value, clamped = self._clamp_i(amps)
        self._sweep.stop()
        with self._hw:
            with self._lock:
                live = self._out_req and self._out_hw and self._mode_hw == "current"
                start = self._prog if live else self._i_set
                side_now = self._relay_hw
                want = _side(value)
                if _side(start) is not None and want is not None and _side(start) != want:
                    raise ValueError(f"a sweep cannot cross zero ({start:g} -> {value:g} A): "
                                     f"sweep to 0 first; the polarity relay switches only "
                                     f"with the current stopped")
                if want is not None and side_now is not want:
                    raise ValueError(f"the polarity relay is on the other side (or unknown): "
                                     f"set_current to a small {'negative' if want else 'positive'} "
                                     f"value first so it switches, then sweep")
                self._sweep_last = start if live else _NAN
                self._wd_armed = True
        rid = self._sweep.start(value, r, start=start)
        if clamped or rclamped:
            self._emit("warn", f"sweep clamped to {value:g} A at {r:g} A/s "
                               f"(limits {self.current_range()[0]:g}..{self.current_range()[1]:g} A, "
                               f"{lo_r:g}..{hi_r:g} A/s)")
        self._emit("info", f"current sweep #{rid}: {start:g} -> {value:g} A at {r:g} A/s"
                   + ("" if live else " (output OFF: only the setpoint walks)"))
        return rid

    def ramp_stop(self) -> bool:
        """End a sweep WHERE IT IS (a scan's Abort; a safety verb)."""
        was = self._sweep.stop()
        if was:
            self._emit("info", f"current sweep stopped at {self._i_set:g} A")
        return was

    def stream_start(self) -> int:
        return self.recorder.start()

    def stream_read(self) -> dict:
        return self.recorder.read()

    def stream_stop(self) -> dict:
        return self.recorder.stop()

    def _stop_sweep(self, why: str) -> None:
        """Called WITHOUT any lock held (stop() waits for the step in progress)."""
        if self._sweep.stop():
            self._emit("info", f"current sweep stopped by {why} at {self._i_set:g} A")

    def _sweep_step(self, amps: float) -> None:
        """One step of the sweep, on the sweep's thread. The setpoint always
        follows; the supply is PROGRAMMED only while the output is live, the
        main channel still holds the value this sweep programmed last, and the
        value is on the relay's side (a sweep never crosses zero)."""
        with self._hw:
            with self._lock:
                if self._mode != "current":
                    raise RuntimeError("the mode is no longer current")
                self._i_set = float(amps)
                live = self._out_req and self._out_hw and self._mode_hw == "current"
            same_side = _side(amps) in (None, self._relay_hw)
            if live and same_side and self._prog == self._sweep_last \
                    and self._flip_t0 is None:
                self._program_main(float(amps))
                self._sweep_last = float(amps)
            else:
                self._sweep_last = _NAN        # not ours any more: never step
            with self._lock:
                self._snapshot = self._build_snapshot()

    def _sweep_done(self, rid: int, reason: str) -> None:
        self.cfg.output.current_A = self._i_set
        if reason == "done":
            self._emit("info", f"current sweep #{rid} done at {self._i_set:g} A")
        elif reason.startswith("error"):
            self._emit("error", f"current sweep #{rid} ended: {reason}")

    # ---- acquisition -----------------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its number at once. Only measurements
        that START `settle_s` after this call count (gotcha #17)."""
        if not self._connected:
            raise ValueError("not connected")
        a = self.cfg.acquisition
        with self._lock:
            self._acq_id += 1
            self._acq = {"id": self._acq_id,
                         "t0": self._clock() + max(0.0, float(a.settle_s)),
                         "want": max(1, int(a.readings)), "v": [], "i": [],
                         "ramping": False}
            return self._acq_id

    def get_sample(self) -> dict:
        with self._lock:
            return dict(self._sample)

    # ---- status ------------------------------------------------------------------

    def status(self) -> Status:
        """The latest snapshot built by the worker. Never touches hardware."""
        r = self._sweep.status()
        with self._lock:
            s = self._snapshot
            st = Status(**{**s.__dict__, "sample": dict(s.sample)})
        st.sweeping = bool(r["ramping"])
        st.ramping = bool(st.ramping or r["ramping"])
        st.ramp_id = int(r["ramp_id"])
        st.sweep_target_A = _NAN if r["ramp_target"] is None else float(r["ramp_target"])
        st.sweep_rate_A_per_s = _NAN if r["ramp_rate"] is None else float(r["ramp_rate"])
        return st

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-read cfg (edited IN PLACE by set_config / the Settings dialog)."""
        o = self.cfg.output
        if o.mode != self._mode:
            try:
                self.set_mode(o.mode)
            except ValueError as exc:
                self._emit("warn", f"mode not changed: {exc}")
                o.mode = self._mode
        self._sanitise_ramp()
        if self._mode_live is None:
            if self._mode == "current":
                self.set_current(o.current_A)
            else:
                self.set_voltage(o.voltage_V)
        with self._lock:
            if self._mode == "current":
                self._v_set = self._clamp_v(o.voltage_V)[0]
            else:
                self._i_set = self._clamp_i(o.current_A)[0]
        self.set_voltage_limit(o.voltage_limit_V)
        self.set_current_limit(o.current_limit_A)

    # ---- the worker ------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            t = self._clock()
            try:
                self.step()
            except Exception as exc:          # never let the worker die
                self._report_hw_error(exc)
            period = 1.0 / max(1.0, float(self.cfg.ramp.step_hz))
            self._stop.wait(max(0.005, period - (self._clock() - t)))

    def step(self, rate_scale: float = 1.0, measure: bool = True,
             dt: float | None = None) -> None:
        """One worker iteration: a pending live mode switch, the limit, output
        changes, the polarity, one ramp step, a measurement if due, and the
        snapshot. Public so tests (and the shutdown ramp) can drive it."""
        now = self._clock()
        if dt is None:
            dt = 0.0 if self._t_step is None else min(0.25, max(0.0, now - self._t_step))
        self._t_step = now

        self._check_watchdog(now)
        with self._lock:
            out_req, kill = self._out_req, self._kill
            self._kill = False
        r = self.cfg.ramp

        try:
            with self._hw:
                with self._lock:
                    mode = self._mode
                    target = self._i_set if mode == "current" else self._v_set
                    limit = self._v_lim if mode == "current" else self._i_lim
                    mode_live = self._mode_live
                if kill and self._out_hw:
                    self.backend.set_output(False)
                    self._out_hw = False
                    self._program_main(0.0)
                    self._zero_hold = 0.0
                    self._flip_t0 = None
                if mode_live is not None:
                    if self._out_hw:
                        self._switch_mode_live(mode_live)
                    else:
                        with self._lock:
                            self._mode, self._mode_live = mode_live, None
                    with self._lock:
                        mode = self._mode
                        target = self._i_set if mode == "current" else self._v_set
                        limit = self._v_lim if mode == "current" else self._i_lim
                if mode != self._mode_hw and not self._out_hw:
                    self._mode_hw = mode
                    self._program_main(0.0)
                    self._lim_hw = None
                if limit != self._lim_hw:
                    self._program_limit(limit)
                if out_req and not self._out_hw:
                    # program 0 first: OUT ON drives whatever is programmed,
                    # which must not be a step for the coil
                    self._program_main(0.0)
                    self.backend.set_output(True)
                    self._out_hw = True
                    self._emit("info", "output ON")

                # -- the goal, with the polarity rules ----------------------------
                goal = target if out_req else 0.0
                goal = self._apply_deadband(goal, mode)
                want = _side(goal)
                if self._relay_hw is None:
                    needs_flip = self._bipolar and want is not None and not self._trust_unknown
                else:
                    needs_flip = self._bipolar and want is not None and want is not self._relay_hw
                ramp_goal = 0.0 if needs_flip else goal
                if not self._bipolar and goal < 0:
                    ramp_goal = 0.0           # cannot happen (clamped), belt and braces

                if self._out_hw:
                    if self._prog != ramp_goal:
                        if not r.enabled:
                            new = ramp_goal
                        else:
                            rate = (r.rate_A_per_s if mode == "current" else r.rate_V_per_s) * rate_scale
                            d = max(0.0, rate) * dt
                            new = ramp_goal if abs(ramp_goal - self._prog) <= d else (
                                self._prog + math.copysign(d, ramp_goal - self._prog))
                        if new != self._prog:
                            self._program_main(new)
                    if not out_req and self._prog == 0.0:
                        # hold at zero for off_hold_s: the coil current lags
                        self._zero_hold += dt
                        if self._zero_hold >= float(r.off_hold_s) - 1e-9:
                            self.backend.set_output(False)
                            self._out_hw = False
                            self._emit("info", "output OFF")
                    else:
                        self._zero_hold = 0.0

                # -- measure (always while a flip waits for zero current) ----------
                v = i = None
                reg = None
                t_meas = now
                hwc = self.cfg.hardware
                fast = self._sweep.running or self.recorder.running
                poll = max(0.1, float(hwc.stream_poll_hz if fast else hwc.poll_hz))
                waiting = needs_flip and self._prog == 0.0
                if self._connected and (measure or waiting) and \
                        (waiting or now - self._t_meas >= 1.0 / poll):
                    t_meas = self._clock()
                    tw0 = time.time()
                    v = float(self.backend.measure_voltage())
                    i = float(self.backend.measure_current())
                    try:
                        reg = self.backend.read_regulation()
                    except Exception:
                        reg = None
                    self._t_meas = t_meas
                    sign = -1.0 if self._relay_hw else 1.0
                    self.recorder.append(0.5 * (tw0 + time.time()),
                                         (sign * i, sign * v, self._prog))

                # -- flip the relay once the current is gone -----------------------
                if needs_flip and self._prog == 0.0:
                    self._try_flip(want, i, now)
                elif self._flip_t0 is not None and not needs_flip:
                    self._flip_t0 = None      # the request went away meanwhile
        except Exception as exc:
            self._report_hw_error(exc)
            with self._lock:
                self._snapshot = self._build_snapshot()
            return

        recovered = False
        with self._lock:
            if v is not None:
                recovered = bool(self._hw_error)
                self._hw_error = ""
                self._v_meas, self._i_meas = v, i
                if reg is not None:
                    self._regulation = reg
                self._advance_acquisition(t_meas, v, i)
            self._snapshot = self._build_snapshot()
            at_limit = self._snapshot.at_limit
        if recovered:
            self._emit("info", "hardware reads recovered")
        if at_limit and self._sweep.running:
            if self._sweep.stop():
                self._emit("warn", "current sweep stopped: the supply is on its voltage "
                                   "limit or its 96 W envelope and the current no longer follows")

    # ---- polarity ------------------------------------------------------------------

    def _apply_deadband(self, goal: float, mode: str) -> float:
        """Hysteresis: with the relay KNOWN on one side, a goal on the other
        side smaller than the deadband is output as 0 and does not flip."""
        if not self._bipolar or goal == 0.0 or self._relay_hw is None:
            return goal
        p = self.cfg.polarity
        band = float(p.deadband_A if mode == "current" else p.deadband_V)
        if _side(goal) is not self._relay_hw and abs(goal) < band:
            return 0.0
        return goal

    def _try_flip(self, want: bool, i_meas, now: float) -> None:
        """Called by the worker (with _hw held) while the output sits at 0 and
        the goal is on the other side. Flips when the measured current is below
        the threshold; gives up after zero_timeout_s."""
        p = self.cfg.polarity
        if self._flip_t0 is None:
            self._flip_t0 = now
            self._emit("info", f"polarity change to {'INVERTED' if want else 'normal'}: "
                               f"at zero, waiting for the current to stop")
        if i_meas is None:
            return
        if abs(i_meas) < float(p.zero_current_A):
            if now < self._relay_retry_t:
                return
            try:
                self.relay.set(want)
            except Exception as exc:
                self._relay_error = f"{type(exc).__name__}: {exc}"
                self._relay_retry_t = now + 1.0
                self._report_relay_error()
                return
            self._relay_hw = want
            self._relay_error = ""
            self._flips += 1
            self._flip_t0 = None
            self._emit("info", f"polarity relay -> {'INVERTED' if want else 'normal'} "
                               f"(at {abs(i_meas) * 1e3:.1f} mA)")
            return
        if now - self._flip_t0 > float(p.zero_timeout_s):
            # refuse: the setpoint goes to 0 on the CURRENT side, the relay stays
            with self._lock:
                if self._mode == "current":
                    self._i_set = 0.0
                    self.cfg.output.current_A = 0.0
                else:
                    self._v_set = 0.0
                    self.cfg.output.voltage_V = 0.0
            self._flip_t0 = None
            self._emit("error", f"polarity change refused: the current stayed at "
                                f"{abs(i_meas) * 1e3:.1f} mA (> {float(p.zero_current_A) * 1e3:g} mA) "
                                f"for {float(p.zero_timeout_s):g} s -- the relay was NOT "
                                f"switched; the setpoint is 0")

    def _report_relay_error(self) -> None:
        now = self._clock()
        if now - self._last_err_emit >= 5.0:
            self._last_err_emit = now
            self._emit("error", f"polarity relay could not be switched: {self._relay_error} "
                                f"(output held at 0; retrying)")

    def _switch_mode_live(self, new_mode: str) -> None:
        """The bumpless mode switch, with _hw held and the output ON.

        voltage -> current: CURR := measured |I| (the supply is now at the
        CV/CC boundary, the coil current unchanged), then VOLT := its limit
        (full range) -- the supply now regulates the current.
        current -> voltage: VOLT := measured |V|, then CURR := its limit."""
        v = float(self.backend.measure_voltage())
        i = float(self.backend.measure_current())
        sign = -1.0 if self._relay_hw else 1.0
        if new_mode == "current":
            amps = min(i, self.current_limit_max())
            self.backend.program_current(amps)
            self.backend.program_voltage(self._v_lim)
            with self._lock:
                self._mode = "current"
                self._i_set = sign * amps
                self._mode_live = None
            self.cfg.output.current_A = sign * amps
            self._prog, self._lim_hw = sign * amps, self._v_lim
            self._emit("info", f"current mode at {sign * amps:g} A (taken over from "
                               f"the measured current; voltage setpoint -> {self._v_lim:g} V)")
        else:
            volts = min(v, self.voltage_limit_max())
            self.backend.program_voltage(volts)
            self.backend.program_current(self._i_lim)
            with self._lock:
                self._mode = "voltage"
                self._v_set = sign * volts
                self._mode_live = None
            self.cfg.output.voltage_V = sign * volts
            self._prog, self._lim_hw = sign * volts, self._i_lim
            self._emit("info", f"voltage mode at {sign * volts:g} V (taken over from "
                               f"the measured voltage; current setpoint -> {self._i_lim:g} A)")
        self._mode_hw = self._mode
        self.cfg.output.mode = self._mode

    # ---- internals ---------------------------------------------------------------

    def _program_main(self, value: float) -> None:
        """Program the mode's quantity. `value` is SIGNED; the supply gets the
        magnitude. A nonzero value on the wrong side of the relay is a bug in
        the step logic -- refuse rather than drive the coil the wrong way."""
        value = float(value)
        side = _side(value)
        if side is not None and self._bipolar and self._relay_hw is not None \
                and side is not self._relay_hw:
            raise RuntimeError(f"internal: {value:g} asked for with the relay on the "
                               f"other side; not programmed")
        if self._mode_hw == "voltage":
            self.backend.program_voltage(abs(value))
        else:
            self.backend.program_current(abs(value))
        self._prog = value

    def _program_limit(self, value: float) -> None:
        if self._mode_hw == "voltage":
            self.backend.program_current(abs(value))
        else:
            self.backend.program_voltage(abs(value))
        self._lim_hw = value

    def _ramp_down_blocking(self) -> None:
        """Shutdown path: ramp to 0 and switch off, in the calling thread."""
        with self._lock:
            self._out_req = False
            mode = self._mode
        r = self.cfg.ramp
        rate = r.rate_A_per_s if mode == "current" else r.rate_V_per_s
        budget = max(0.5, float(self.cfg.safety.shutdown_ramp_s))
        need = abs(self._prog) / max(1e-9, rate)
        scale = max(1.0, need / (0.9 * budget))
        if scale > 1.0 and self._out_hw:
            self._emit("warn", f"shutdown: ramping {scale:.1f}x faster to finish "
                               f"within {budget:g} s")
        period = 1.0 / max(1.0, float(r.step_hz))
        t_end = time.monotonic() + budget + 1.0
        while self._out_hw and time.monotonic() < t_end:
            time.sleep(period)
            self.step(rate_scale=scale, measure=False, dt=period)
        if self._out_hw:
            self._emit("error", "shutdown ramp did not finish; switching off anyway")

    def _check_watchdog(self, now: float) -> None:
        wd = float(self.cfg.safety.watchdog_s)
        if wd <= 0:
            return
        with self._lock:
            live = self._out_req and self._wd_armed
        if live and now - self._last_touch > wd:
            self._sweep.stop()
            with self._lock:
                self._out_req = False
            self._emit("warn", f"no client for {wd:g} s: ramping to zero, output off")

    def _advance_acquisition(self, t_start: float, v: float, i: float) -> None:
        """Called with _lock held. Only readings that STARTED after t0 count.
        v, i are magnitudes; the sample is signed by the relay."""
        a = self._acq
        if a is None or t_start < a["t0"]:
            return
        sign = -1.0 if self._relay_hw else 1.0
        a["v"].append(sign * v)
        a["i"].append(sign * i)
        if self._out_hw and self._prog != (self._i_set if self._mode == "current"
                                           else self._v_set):
            a["ramping"] = True
        if len(a["v"]) < a["want"]:
            return
        n = len(a["v"])
        mv, mi = sum(a["v"]) / n, sum(a["i"]) / n
        sv = math.sqrt(sum((x - mv) ** 2 for x in a["v"]) / (n - 1)) if n > 1 else 0.0
        si = math.sqrt(sum((x - mi) ** 2 for x in a["i"]) / (n - 1)) if n > 1 else 0.0
        self._sample = {"acq_id": a["id"], "voltage_V": mv, "current_A": mi,
                        "voltage_std_V": sv, "current_std_A": si, "n": n,
                        "mode": self._mode, "ramping": a["ramping"],
                        "relay_inverted": self._relay_hw,
                        "time": time.time()}
        self._acq = None

    def _build_snapshot(self) -> Status:
        """Called with _lock held (or before any thread exists). The ONLY place
        a Status is made: setters change attributes, this copies them."""
        v, i = self._v_meas, self._i_meas
        sign = -1.0 if self._relay_hw else 1.0
        mode = self._mode
        r = self.cfg.ramp
        goal = (self._i_set if mode == "current" else self._v_set) if self._out_req else 0.0
        # the EFFECTIVE goal: a setpoint inside the polarity deadband is output
        # as 0 -- at 0 it has arrived (else a scan would wait for it forever)
        goal = self._apply_deadband(goal, mode)
        ramping = ((self._out_req != self._out_hw) or (self._out_hw and self._prog != goal)
                   or self._flip_t0 is not None)
        finite = math.isfinite(v) and math.isfinite(i)
        power = v * i if finite else _NAN
        p_max = float(self.cfg.limits.power_max_W)
        power_limited = bool(self._out_hw and finite and power >= 0.97 * p_max)
        reg = self._regulation if self._out_hw else "off"
        # At the limit = the OTHER quantity has reached its setpoint (measured),
        # or the 96 W envelope holds the output. Deliberately not "reg != mode":
        # while a coil's current slews to a new setpoint the supply sits
        # briefly at its voltage setpoint (CV) on every ramp step -- that is
        # normal, not a limit, and would stop every sweep.
        lim = self._v_lim if mode == "current" else self._i_lim
        other = v if mode == "current" else i
        at_limit = bool(self._out_hw and ((lim > 0 and math.isfinite(other)
                                           and other >= 0.98 * lim) or power_limited))
        return Status(
            mode=mode, output=self._out_hw, output_request=self._out_req,
            current_set_A=self._i_set, voltage_set_V=self._v_set,
            current_limit_A=self._i_lim, voltage_limit_V=self._v_lim,
            programmed=self._prog, ramping=bool(ramping),
            ramp_enabled=bool(r.enabled),
            ramp_rate_A_per_s=float(r.rate_A_per_s),
            ramp_rate_V_per_s=float(r.rate_V_per_s),
            voltage_V=sign * v if math.isfinite(v) else _NAN,
            current_A=sign * i if math.isfinite(i) else _NAN,
            power_W=power,
            resistance_ohm=(v / i) if (finite and i >= 0.01) else _NAN,
            regulation=reg, at_limit=at_limit,
            power_limited=power_limited, power_max_W=p_max,
            bipolar=self._bipolar, relay_inverted=self._relay_hw,
            polarity_switching=self._flip_t0 is not None,
            relay_error=self._relay_error, relay_flips=self._flips,
            acq_id=self._acq_id, acquiring=self._acq is not None,
            acq_readings=int(self.cfg.acquisition.readings),
            sample=dict(self._sample),
            connected=self._connected, idn=self._idn, hw_error=self._hw_error,
        )

    def _clamp_i(self, v):
        return _clamp(float(v), *self.current_range())

    def _clamp_v(self, v):
        return _clamp(float(v), *self.voltage_range())

    def _clamp_ilim(self, v):
        return _clamp(abs(float(v)), 0.0, self.current_limit_max())

    def _clamp_vlim(self, v):
        return _clamp(abs(float(v)), 0.0, self.voltage_limit_max())

    def _sanitise_ramp(self) -> None:
        r, lim = self.cfg.ramp, self.cfg.limits
        r.rate_A_per_s = _clamp(float(r.rate_A_per_s), 1e-6, lim.rate_max_A_per_s)[0]
        r.rate_V_per_s = _clamp(float(r.rate_V_per_s), 1e-6, lim.rate_max_V_per_s)[0]
        # 20 Hz ceiling: ~20 ms per serial command (manual), and every step
        # may also measure V, I and the regulation
        r.step_hz = _clamp(float(r.step_hz), 1.0, 20.0)[0]
        r.off_hold_s = _clamp(float(r.off_hold_s), 0.0, 2.0)[0]

    def _report(self, what, value, unit, clamped, lo, hi) -> None:
        if clamped:
            self._emit("warn", f"{what} clamped to {value:g} {unit} "
                               f"(limit {lo:g}..{hi:g})")
        else:
            self._emit("info", f"{what} = {value:g} {unit}")

    def _report_hw_error(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._hw_error = msg
        now = self._clock()
        if now - self._last_err_emit >= 5.0:
            self._last_err_emit = now
            self._emit("error", f"hardware call failed: {msg}")

    def _emit(self, level: str, msg: str) -> None:
        try:
            self._on_event(level, msg)
        except Exception:
            pass
