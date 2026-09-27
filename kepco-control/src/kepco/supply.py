"""BipolarSupply: the brain between the wire and the backend.

What it does, in the order a physicist would worry about it:

  * CLAMPS every request to the safety envelope in cfg.limits (and says so with
    a warn event -- nothing is silently changed).
  * RAMPS. A new setpoint is never applied as a step: a worker thread walks the
    main channel towards it at `ramp.rate_*` per second, `ramp.step_hz` times a
    second. With a coil on the output, V = L dI/dt, so a current step would ask
    for an infinite voltage.
  * RAMPS TO ZERO BEFORE THE OUTPUT GOES OFF. The BOP's own OUTP OFF programs
    0 V / 0 A in one step (manual B.20). So "output off" means: ramp the main
    channel to 0, THEN send OUTP OFF. The same happens on shutdown (sped up so it
    ends within `safety.shutdown_ramp_s`), on a watchdog timeout, and when the
    service stops after a crash.
  * CHANGES NOTHING AT START. start() reads the mode, the setpoint, the limit
    and the output switch and adopts them (see start()); a live output stays
    live and the ramp continues from where the instrument is.
  * MEASURES V and I at `hardware.poll_hz` and publishes them live.
  * ACQUIRES for a scan: `acquire()` returns a number at once; the worker then
    waits `acquisition.settle_s` (the BIT 4886 averages its last 16 readings,
    valid ~320 ms after a change) and averages `acquisition.readings` fresh
    measurements into `sample`.

THREADS (docs gotcha #1). One worker thread owns every hardware call. Setters
only change brain attributes (under `_lock`); the worker reads them, acts, and
REBUILDS the status snapshot. `status()` returns that snapshot and never talks
to the instrument, so a slow GPIB bus can never block a status request.

Mode rule: the mode (current/voltage) can only be changed with the output OFF.
Switching mode with the output live would make the supply jump from regulating
one quantity to the other -- with a coil attached, exactly the step the ramp
exists to avoid.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .backends.base import BipolarSupplyBackend
from .config import Config, MODES

_NAN = float("nan")


@dataclass
class Status:
    """One snapshot of the supply, for status() and the wire."""

    mode: str = "current"
    output: bool = False             # output switch as commanded to the instrument
    output_request: bool = False     # what the user asked for (may still be ramping)
    current_set_A: float = 0.0       # main-channel TARGET in current mode
    voltage_set_V: float = 0.0       # main-channel TARGET in voltage mode
    current_limit_A: float = 0.0     # limit channel in voltage mode
    voltage_limit_V: float = 0.0     # limit channel (compliance) in current mode
    programmed: float = 0.0          # main channel right now (A or V, per mode)
    ramping: bool = False            # True until the output has reached its target
    ramp_enabled: bool = True
    ramp_rate_A_per_s: float = 0.0
    ramp_rate_V_per_s: float = 0.0
    voltage_V: float = _NAN          # measured, live
    current_A: float = _NAN          # measured, live
    power_W: float = _NAN            # V*I: >0 sourcing, <0 sinking (4 quadrants)
    at_limit: bool = False           # the limit channel has taken over
    acq_id: int = 0
    acquiring: bool = False
    acq_readings: int = 1
    sample: dict = field(default_factory=dict)
    connected: bool = False
    idn: str = ""
    hw_error: str = ""


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


class BipolarSupply:
    def __init__(self, backend: BipolarSupplyBackend, cfg: Config | None = None,
                 clock=time.monotonic):
        self.backend = backend
        self.cfg = cfg or Config()
        self._clock = clock
        self._hw = threading.RLock()       # serialises EVERY backend call
        self._lock = threading.Lock()      # guards the attributes below + snapshot

        o = self.cfg.output
        # ---- what the user asked for (setters write these) -------------------
        self._mode = o.mode if o.mode in MODES else "current"
        self._out_req = False              # replaced by what start() finds
        self._kill = False                 # output_off_now() pending
        # The lost-client watchdog guards an output that a client of THIS
        # service drove. An output ADOPTED live at start was energised by
        # somebody else (a previous session, clMag-control): if the watchdog
        # were armed from the start, a service left running with nobody
        # talking to it would ramp that coil to zero on its own -- a change
        # nobody asked for. So it arms on set_output(True) / set_current /
        # set_voltage, i.e. once a client has taken charge of the output.
        self._wd_armed = False
        self._i_set = self._v_set = 0.0
        self._i_lim = self._v_lim = 0.0
        # ---- what the worker has applied to the instrument (worker only) -----
        self._mode_hw: str | None = None
        self._out_hw = False
        self._prog = 0.0                   # main channel as programmed now
        self._lim_hw: float | None = None  # limit channel as programmed now
        self._t_step: float | None = None
        self._t_meas = -1e9
        self._zero_hold = 0.0              # time spent at 0 before OUTP OFF
        # ---- measurements + acquisition (under _lock) ------------------------
        self._v_meas = _NAN
        self._i_meas = _NAN
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
        # replaced by the service / GUI to forward events; default = no-op
        self._on_event = lambda level, msg: None

        # initial setpoints from the config, clamped (quietly: nobody asked yet)
        self._i_set = self._clamp_i(o.current_A)[0]
        self._v_set = self._clamp_v(o.voltage_V)[0]
        self._i_lim = self._clamp_ilim(o.current_limit_A)[0]
        self._v_lim = self._clamp_vlim(o.voltage_limit_V)[0]
        self._sanitise_ramp()
        self._snapshot = self._build_snapshot()

    # ---- lifecycle -----------------------------------------------------------

    def start(self, poll: bool = True) -> None:
        """Open the instrument, READ its state and ADOPT it, start the worker.
        `poll=False` skips the thread so a test can drive `step()` by hand.

        Nothing is written that changes the BOP (Lukas, 2026-09-27: "read the
        instrument state on startup, not change anything"). This matters here
        more than anywhere: the BOP may be driving a coil at several amperes --
        it is the same physical unit clMag-control drives -- and switching it
        off, re-programming it or changing its mode would put a step on that
        coil. So the mode, the main setpoint, the limit and the output switch
        are taken over exactly as found; a live output stays live, at the value
        it has, and the software ramp continues from there when a user asks
        for a new setpoint."""
        with self._hw:
            self.backend.open()
            self._idn = self.backend.idn()
            try:
                found = self.backend.read_state()
            except Exception as exc:
                # Without the state we cannot know whether a coil is energised,
                # so taking control would be guessing. Refuse, and say why.
                # (backend.close() is NOT called: it sends OUTP OFF.)
                raise RuntimeError(f"could not read the supply's state at start "
                                   f"({type(exc).__name__}: {exc}); not taking "
                                   f"control of an instrument in an unknown state") from exc
            self._adopt(found)
        self._connected = True
        self._last_touch = self._clock()
        self._t_step = None
        with self._lock:
            self._snapshot = self._build_snapshot()
        main = self._prog if self._out_hw else (
            self._i_set if self._mode == "current" else self._v_set)
        unit = "A" if self._mode == "current" else "V"
        lim_txt = (f"voltage limit {self._v_lim:g} V" if self._mode == "current"
                   else f"current limit {self._i_lim:g} A")
        self._emit("info", f"connected: {self._idn or 'Kepco BOP'}; found {self._mode} "
                           f"mode, output {'ON' if self._out_hw else 'OFF'}, "
                           f"setpoint {main:g} {unit}, {lim_txt} -- adopted, "
                           f"nothing changed")
        if poll:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="kepco-worker",
                                            daemon=True)
            self._thread.start()

    def _adopt(self, found: dict) -> None:
        """Take the instrument's state as the brain's own (called with _hw held,
        before the worker exists). After this, the worker sees nothing to do:
        mode, limit and main channel all equal what is already programmed, so
        its first step writes nothing.

        The found values are adopted UNCLAMPED. Clamping them into cfg.limits
        would make the worker ramp the output to the clamped value at once --
        a change nobody asked for. An out-of-envelope value is reported with a
        warn event instead; the next setpoint a user sends is clamped as usual."""
        mode = str(found.get("mode", "")).strip().lower()
        if mode not in MODES:
            raise RuntimeError(f"the supply reported an unknown mode {mode!r}")
        out = bool(found.get("output", False))
        prog_v = float(found.get("voltage_V", 0.0))
        prog_i = float(found.get("current_A", 0.0))
        if not (math.isfinite(prog_v) and math.isfinite(prog_i)):
            raise RuntimeError(f"the supply reported non-finite setpoints "
                               f"(VOLT {prog_v!r}, CURR {prog_i!r})")
        # 4.1.1.1: the mode's own quantity is the main channel; the other one
        # is the limit, used as an ABSOLUTE value
        main, limit = (prog_i, abs(prog_v)) if mode == "current" else (prog_v, abs(prog_i))
        o = self.cfg.output
        with self._lock:
            self._mode = mode
            self._out_req = out            # a live output stays live
            if mode == "current":
                self._i_set, self._v_lim = main, limit
            else:
                self._v_set, self._i_lim = main, limit
        # what is ON the instrument (worker-only attributes; no worker yet)
        self._mode_hw = mode
        self._out_hw = out
        # With the output OFF the BOP drives 0 whatever is programmed (B.20),
        # and our next OUTP ON programs 0 first anyway -- so the ramp starts
        # from 0. With the output ON the ramp continues from the found value.
        self._prog = main if out else 0.0
        self._lim_hw = limit
        # mirror into the config, so get_config / Save config show the truth
        o.mode = mode
        if mode == "current":
            o.current_A, o.voltage_limit_V = main, limit
        else:
            o.voltage_V, o.current_limit_A = main, limit
        # say so if the found state sits outside our safety envelope
        if mode == "current":
            checks = [("current", main, "A", *self.current_range()),
                      ("voltage limit", limit, "V", 0.0, self.voltage_limit_max())]
        else:
            checks = [("voltage", main, "V", *self.voltage_range()),
                      ("current limit", limit, "A", 0.0, self.current_limit_max())]
        for what, val, unit, lo, hi in checks:
            if val < lo or val > hi:
                self._emit("warn", f"found {what} {val:g} {unit} outside the "
                                   f"envelope {lo:g}..{hi:g} {unit}; left as it "
                                   f"is (the next setpoint is clamped)")

    def shutdown(self) -> None:
        """Ramp to zero, output off, disconnect. Safe to call more than once and
        from a crash handler. The ramp is sped up (never slowed) so it ends
        within `safety.shutdown_ramp_s` -- the launcher kills a service that
        takes more than 8 s to exit, and a kill mid-ramp would leave the output
        live."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        was = self._connected
        try:
            if was:
                self._ramp_down_blocking()
        finally:
            try:
                with self._hw:
                    self.backend.close()
            finally:
                self._connected = False
                self._out_hw = False
                with self._lock:
                    self._out_req = False
                    self._acq = None
                    self._snapshot = self._build_snapshot()
                if was:
                    self._emit("info", "output off, disconnected")

    # ---- limits (live, also used by describe) ---------------------------------

    @property
    def mode(self) -> str:
        """The mode as last SET (the snapshot may lag it by one worker step)."""
        return self._mode

    def current_range(self) -> tuple[float, float]:
        lim = self.cfg.limits
        return float(lim.current_min_A), float(lim.current_max_A)

    def voltage_range(self) -> tuple[float, float]:
        lim = self.cfg.limits
        return float(lim.voltage_min_V), float(lim.voltage_max_V)

    def current_limit_max(self) -> float:
        lo, hi = self.current_range()
        return max(abs(lo), abs(hi))

    def voltage_limit_max(self) -> float:
        lo, hi = self.voltage_range()
        return max(abs(lo), abs(hi))

    # ---- setters: clamp, store in brain attributes, mirror into cfg ----------

    def set_mode(self, mode: str) -> None:
        mode = str(mode).strip().lower()
        if mode in ("curr", "i"):
            mode = "current"
        if mode in ("volt", "v"):
            mode = "voltage"
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        with self._lock:
            if mode == self._mode:
                return
            if self._out_req or self._out_hw:
                raise ValueError("switch the output off before changing mode "
                                 "(a live mode change is a step on the load)")
            self._mode = mode
        self.cfg.output.mode = mode
        self._emit("info", f"{mode} mode")

    def set_output(self, on: bool) -> None:
        on = bool(on)
        with self._lock:
            self._out_req = on
            if on:
                self._wd_armed = True      # a client now owns this output
        if on:
            self._emit("info", "output ON requested: ramping to the setpoint")
        else:
            self._emit("info", "output OFF requested: ramping to zero first")

    def output_off_now(self) -> None:
        """EMERGENCY: OUTP OFF without ramping. On the BOP that programs 0 V /
        0 A in one step -- with a coil attached the supply then has to absorb
        the coil's energy. Use the normal `set_output(False)` unless the ramp
        itself is the problem."""
        with self._lock:
            self._kill = True
            self._out_req = False
        self._emit("warn", "output OFF NOW (no ramp)")

    def set_current(self, amps: float) -> None:
        """The output current -- current mode only (in voltage mode the current
        is the LIMIT channel: use set_current_limit)."""
        amps = _finite(amps, "current")
        if self._mode != "current":
            raise ValueError("set_current needs current mode; in voltage mode "
                             "the current is the limit: use set_current_limit")
        value, clamped = self._clamp_i(amps)
        with self._lock:
            self._i_set = value
            self._wd_armed = True          # a client now drives the output
        self.cfg.output.current_A = value
        lo, hi = self.current_range()
        self._report("current", value, "A", clamped, lo, hi)

    def set_voltage(self, volts: float) -> None:
        """The output voltage -- voltage mode only."""
        volts = _finite(volts, "voltage")
        if self._mode != "voltage":
            raise ValueError("set_voltage needs voltage mode; in current mode "
                             "the voltage is the limit: use set_voltage_limit")
        value, clamped = self._clamp_v(volts)
        with self._lock:
            self._v_set = value
            self._wd_armed = True          # a client now drives the output
        self.cfg.output.voltage_V = value
        lo, hi = self.voltage_range()
        self._report("voltage", value, "V", clamped, lo, hi)

    def set_voltage_limit(self, volts: float) -> None:
        """Compliance in current mode: the most voltage the supply may apply to
        push the current (absolute value, both polarities)."""
        value, clamped = self._clamp_vlim(_finite(volts, "voltage limit"))
        with self._lock:
            self._v_lim = value
        self.cfg.output.voltage_limit_V = value
        self._report("voltage limit", value, "V", clamped, 0.0, self.voltage_limit_max())

    def set_current_limit(self, amps: float) -> None:
        """Current limit in voltage mode (absolute value, both polarities)."""
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
        """A client is alive (the service calls this on every command). Feeds
        the lost-client watchdog."""
        self._last_touch = self._clock()

    # ---- acquisition -----------------------------------------------------------

    def acquire(self) -> int:
        """Start an acquisition; returns its number at once. Only measurements
        that START `settle_s` after this call count, so the average never
        includes a reading from before the trigger (gotcha #17)."""
        if not self._connected:
            raise ValueError("not connected")
        a = self.cfg.acquisition
        with self._lock:
            # id and "acquiring" change TOGETHER, under the lock, so no status
            # snapshot can show the new id with a stale "not acquiring".
            self._acq_id += 1
            self._acq = {"id": self._acq_id,
                         "t0": self._clock() + max(0.0, float(a.settle_s)),
                         "want": max(1, int(a.readings)), "v": [], "i": [],
                         "ramping": False}
            # The snapshot is NOT touched here (gotcha #1): the worker's next
            # rebuild (<= 1/step_hz later) shows id + busy together. A scan
            # waits for THIS id (target_key), so it cannot read a stale frame.
            return self._acq_id

    def get_sample(self) -> dict:
        with self._lock:
            return dict(self._sample)

    # ---- status ------------------------------------------------------------------

    def status(self) -> Status:
        """The latest snapshot built by the worker. Never touches hardware."""
        with self._lock:
            s = self._snapshot
            return Status(**{**s.__dict__, "sample": dict(s.sample)})

    def get_config(self) -> Config:
        return self.cfg

    def apply_config(self) -> None:
        """Re-read cfg (edited IN PLACE by set_config / the Settings dialog):
        re-clamp every setpoint to the possibly new limits and adopt it."""
        o = self.cfg.output
        if o.mode != self._mode:
            try:
                self.set_mode(o.mode)
            except ValueError as exc:
                self._emit("warn", f"mode not changed: {exc}")
                o.mode = self._mode
        self._sanitise_ramp()
        if self._mode == "current":
            self.set_current(o.current_A)
        else:
            self.set_voltage(o.voltage_V)
        # the other mode's setpoint is stored (clamped) for when it is selected
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
        """One worker iteration: apply mode / limit / output changes, advance
        the ramp one step, measure if due, rebuild the snapshot. Public so tests
        (and the shutdown ramp, which passes its own `dt`) can drive it."""
        now = self._clock()
        if dt is None:
            dt = 0.0 if self._t_step is None else min(0.25, max(0.0, now - self._t_step))
            # min(0.25): after a stall (debugger, bus timeout) the ramp goes
            # on at its rate instead of catching up in one big step.
        self._t_step = now

        self._check_watchdog(now)
        with self._lock:
            mode, out_req, kill = self._mode, self._out_req, self._kill
            self._kill = False
            target = self._i_set if mode == "current" else self._v_set
            limit = self._v_lim if mode == "current" else self._i_lim
        r = self.cfg.ramp
        rate = (r.rate_A_per_s if mode == "current" else r.rate_V_per_s) * rate_scale

        try:
            with self._hw:
                if kill and self._out_hw:
                    self.backend.set_output(False)
                    self._out_hw = False
                    self._program_main(0.0)
                    self._zero_hold = 0.0   # a later switch-off starts its hold afresh
                if mode != self._mode_hw and not self._out_hw:
                    self._apply_mode_locked(mode)
                if limit != self._lim_hw:
                    self._program_limit(limit)
                if out_req and not self._out_hw:
                    # program 0 first: OUTP ON restores the saved programmed
                    # value (B.20), which must not be a step
                    self._program_main(0.0)
                    self.backend.set_output(True)
                    self._out_hw = True
                    self._emit("info", "output ON")
                if self._out_hw:
                    goal = target if out_req else 0.0
                    if self._prog != goal:
                        if not r.enabled:
                            new = goal
                        else:
                            d = max(0.0, rate) * dt
                            new = goal if abs(goal - self._prog) <= d else (
                                self._prog + math.copysign(d, goal - self._prog))
                        if new != self._prog:
                            self._program_main(new)
                    if not out_req and self._prog == 0.0:
                        # Hold at zero for `ramp.off_hold_s` before OUTP OFF:
                        # the load current lags the programmed value (a coil
                        # by L/R), so give it time to arrive at zero too.
                        self._zero_hold += dt
                        if self._zero_hold >= float(r.off_hold_s) - 1e-9:
                            self.backend.set_output(False)
                            self._out_hw = False
                            self._emit("info", "output OFF")
                    else:
                        self._zero_hold = 0.0
                v = i = None
                t_meas = now
                if measure and self._connected and (
                        now - self._t_meas >= 1.0 / max(0.1, self.cfg.hardware.poll_hz)):
                    t_meas = self._clock()
                    v = self.backend.measure_voltage()
                    i = self.backend.measure_current()
                    self._t_meas = t_meas
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
                self._v_meas, self._i_meas = float(v), float(i)
                self._advance_acquisition(t_meas, float(v), float(i))
            # the busy flag, the id and the sample are published in ONE
            # critical section (gotcha #28)
            self._snapshot = self._build_snapshot()
        if recovered:
            self._emit("info", "hardware reads recovered")

    # ---- internals ---------------------------------------------------------------

    def _apply_mode_locked(self, mode: str) -> None:
        """Called with _hw held and the output OFF: FUNC:MODE, then both
        channels to a known state."""
        self.backend.set_mode(mode)
        self._mode_hw = mode
        self._program_main(0.0)
        self._lim_hw = None           # re-sent by the next step

    def _program_main(self, value: float) -> None:
        if self._mode_hw == "voltage":
            self.backend.program_voltage(value)
        else:
            self.backend.program_current(value)
        self._prog = float(value)

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
        if scale > 1.0:
            self._emit("warn", f"shutdown: ramping {scale:.1f}x faster to finish "
                               f"within {budget:g} s")
        period = 1.0 / max(1.0, float(r.step_hz))
        # Real time, not self._clock: the deadline must hold even under a
        # test's frozen clock. Each step advances the ramp by exactly `period`.
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
        # not armed = an adopted output nobody here has touched: leave it
        if live and now - self._last_touch > wd:
            with self._lock:
                self._out_req = False
            self._emit("warn", f"no client for {wd:g} s: ramping to zero, output off")

    def _advance_acquisition(self, t_start: float, v: float, i: float) -> None:
        """Called with _lock held. Only readings that STARTED after t0 count."""
        a = self._acq
        if a is None or t_start < a["t0"]:
            return
        a["v"].append(v)
        a["i"].append(i)
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
                        "time": time.time()}
        self._acq = None

    def _build_snapshot(self) -> Status:
        """Called with _lock held (or before any thread exists). The ONLY place
        a Status is made: setters change attributes, this copies them."""
        v, i = self._v_meas, self._i_meas
        mode = self._mode
        r = self.cfg.ramp
        goal = (self._i_set if mode == "current" else self._v_set) if self._out_req else 0.0
        ramping = (self._out_req != self._out_hw) or (self._out_hw and self._prog != goal)
        lim = self._v_lim if mode == "current" else self._i_lim
        meas = v if mode == "current" else i
        at_limit = bool(self._out_hw and lim > 0 and math.isfinite(meas)
                        and abs(meas) >= 0.98 * lim)
        return Status(
            mode=mode, output=self._out_hw, output_request=self._out_req,
            current_set_A=self._i_set, voltage_set_V=self._v_set,
            current_limit_A=self._i_lim, voltage_limit_V=self._v_lim,
            programmed=self._prog, ramping=bool(ramping),
            ramp_enabled=bool(r.enabled),
            ramp_rate_A_per_s=float(r.rate_A_per_s),
            ramp_rate_V_per_s=float(r.rate_V_per_s),
            voltage_V=v, current_A=i,
            power_W=(v * i) if (math.isfinite(v) and math.isfinite(i)) else _NAN,
            at_limit=at_limit,
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
        # 40 Hz ceiling: the BIT 4886 manual (sec. 4.1.1.3, software-timed
        # ramp) gives 25 ms as its fastest ramp step; faster writes just queue up.
        r.step_hz = _clamp(float(r.step_hz), 1.0, 40.0)[0]
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
        if now - self._last_err_emit >= 5.0:      # rate-limit: one event per 5 s
            self._last_err_emit = now
            self._emit("error", f"hardware call failed: {msg}")

    def _emit(self, level: str, msg: str) -> None:
        try:
            self._on_event(level, msg)
        except Exception:
            pass
