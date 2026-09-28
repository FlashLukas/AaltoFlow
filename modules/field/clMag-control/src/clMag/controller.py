"""The Controller: the state machine that runs the magnet.

This is the Python replacement for the big LabVIEW while-loop-with-a-case-
structure. It owns the hardware handles and runs ONE control loop in its own
thread. External callers never touch the hardware directly; they drop commands
into a thread-safe queue (set_current, set_field, demag, calibrate, ...), and
the loop processes them. That same command model is exactly what the ZeroMQ
service will sit on top of in Session 5, so nothing here has to change then.

States
------
IDLE      : holding whatever current we have, not regulating a field. At start
            this is whatever the supply was ALREADY doing: the controller
            reads the output state and current and adopts them, and writes
            nothing to the supply until the first command that moves the
            current (adopt-on-start rule, Lukas 2026-09-27).
RAMPING   : moving current toward a target (direct set, or a calibration jump);
            when the ramp finishes we go to whatever `_after_ramp` says.
SEEK      : PI is actively closing the last couple of mT to a field setpoint,
            approaching from one side and never overshooting. When the error
            enters tolerance/2 the PI freezes and we start the stability dwell;
            once the field holds within full tolerance for `stable_time`, -> STABLE.
STABLE    : field reached and verified; the "stable" indicator is on; the slow
            long-term stabilizer trims drift. (PI does NOT re-engage on drift.)
HOLD      : holding a field set by calibration only (no PI verification).
DEMAG     : walking exponentially decaying +/- current steps down to zero.
CALIBRATE : sweeping current, dwelling, measuring, building a fresh B(I) curve.

The control loop calls `ramper.step()` once per tick and pushes the resulting
setpoint to the supply, so current physically never jumps.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Tuple

from .acquisition import AcquisitionThread
from .backends.base import CurrentSource
from .calibration import FieldCalibration
from .config import Config
from .pid import PI
from .ramp import Ramper


class State(str, Enum):
    IDLE = "IDLE"
    RAMPING = "RAMPING"
    SEEK = "SEEK"
    STABLE = "STABLE"
    HOLD = "HOLD"
    DEMAG = "DEMAG"
    CALIBRATE = "CALIBRATE"


@dataclass
class Status:
    state: str
    setpoint_field_mT: Optional[float]
    measured_field_mT: float
    current_A: float
    field_stable: bool
    locked: bool
    aux: Optional[dict] = None    # {"ao": {...}, "ai": {...}, "do": {...}}
    output_on: bool = False       # the supply's output switch, as read back
    # How many queued commands the control thread has FINISHED taking up
    # (deep cleaning 2026-09-28). Every command call returns its own number;
    # "cmd_done >= my number" is the only way a client can tell that a status
    # frame already reflects its command -- the state or the setpoint alone
    # can look identical before and after (gotcha #2).
    cmd_done: int = 0


def _finite(name: str, value) -> float:
    """Refuse NaN / inf before it reaches the control loop.

    Why this matters: every safety clamp here is a comparison (`amps > lim`),
    and EVERY comparison with NaN is False -- so a NaN walked straight through
    the +-3 A clamp and the ramp then climbed 0.05 A per tick for ever; a NaN
    demag amplitude made the step-list loop never end; a NaN AO voltage came
    out as +10 V. JSON happily carries NaN, so this was reachable over the
    wire. Raising here (in the CALLER's thread) makes the service answer
    ok:false instead."""
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return v


class Controller:
    def __init__(
        self,
        cfg: Config,
        kepco: CurrentSource,
        acq: AcquisitionThread,
        calibration: Optional[FieldCalibration] = None,
        on_event: Optional[Callable[[str, str], None]] = None,
        csv_path: Optional[str] = None,
        aux=None,
    ) -> None:
        self.cfg = cfg
        self.kepco = kepco
        self.acq = acq
        self.calibration = calibration
        self.aux = aux                       # general-purpose DAQ I/O (AUX panel)
        self._aux_lock = threading.Lock()

        self.ramper = Ramper(cfg.ramp.increment_A, cfg.ramp.delay_s)
        self.pi = PI(cfg.pid.Kc_A_per_mT, cfg.pid.Ti_s)

        self._state = State.IDLE
        self._commands: "queue.Queue[Tuple]" = queue.Queue()
        # command numbering (see Status.cmd_done): _cmd_seq is handed out under
        # a lock in the callers' threads; _cmd_done is advanced only by the
        # control thread, after it has taken a command up.
        self._cmd_seq = 0
        self._cmd_seq_lock = threading.Lock()
        self._cmd_done = 0
        # throttle for repeated loop errors (a dead GPIB would fail every tick)
        self._last_err = ("", 0.0)
        self._acq_errors_seen = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # regulation bookkeeping
        self._setpoint_field: Optional[float] = None
        self._jump_current = 0.0
        self._target_current_est = 0.0   # calibration's current for the target field
        # how far past that estimate the PI may push while seeking, so it can
        # trim out the calibration residual but can never overshoot wildly.
        self._seek_cap_margin_A = 0.08
        self._approach_sign = 0
        self._settling = False
        self._stable_since: Optional[float] = None
        self._field_stable = False
        self._hold_current = 0.0
        # when True the loop commands exactly _freeze_current every tick, so the
        # ramp direction never flips and the magnet's hysteresis stays put.
        self._output_frozen = False
        self._freeze_current = 0.0
        self.stabilizer_enabled = True
        self.locked = False

        # ADOPT-ON-START. `_driving` stays False until a command that moves the
        # current begins; until then the loop sends NOTHING to the supply, so
        # starting (or restarting) the service cannot change what the magnet
        # is doing. `_output_on` mirrors the supply's output switch (read at
        # start, then updated when we switch it on). Both are plain attributes
        # that status() copies (gotcha #1), never written into a snapshot.
        self._driving = False
        self._output_on = False

        # sub-state for DEMAG / CALIBRATE
        self._seq: List[float] = []
        self._seq_i = 0
        self._phase = ""
        self._phase_t = 0.0
        self._cal_points: List[Tuple[float, float]] = []
        self._cal_dwell_s = 0.5
        self._cal_after: Optional[Callable[[FieldCalibration], None]] = None

        # logging
        self._on_event = on_event or (lambda level, msg: None)
        self._csv_path = csv_path
        self._csv = None

    # ------------------------------------------------------------------ API
    # These just enqueue; the loop does the real work on its own thread.

    def start(self) -> None:
        # open() only connects and queries (see backends/base.py): the supply
        # keeps whatever output and current it had, and so do the AUX outputs.
        self.kepco.open()
        if self.aux is not None:
            self.aux.open()
        if not self.acq.is_alive():
            self.acq.start()
        # Adopt: the ramp starts from the current actually flowing, so the
        # first command ramps FROM there instead of jumping.
        self._output_on = bool(self.kepco.read_output())
        adopted = self.kepco.read_current()
        self.ramper.sync_to(adopted)
        self._hold_current = adopted
        self._driving = False
        self._stop.clear()
        if self._csv_path:
            self._csv = open(self._csv_path, "w", encoding="utf-8")
            self._csv.write("time_s,current_A,field_mT,state\n")
        self._thread = threading.Thread(target=self._run, name="control", daemon=True)
        self._thread.start()
        self._event("info", f"controller started; adopted supply state: output "
                            f"{'ON' if self._output_on else 'OFF'}, {adopted:.3f} A")

    def shutdown(self) -> None:
        """Safe stop: ramp to zero, output off, threads down. Safe to call on a
        crash or a lost client."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        try:
            self._ramp_to_zero_blocking()
        finally:
            self.kepco.close()
            if self.aux is not None:
                self.aux.close()
            self.acq.stop()
            if self._csv:
                self._csv.close()
                self._csv = None
        self._event("info", "controller shut down (output off)")

    # Each command returns its sequence number (see Status.cmd_done). Values
    # are checked HERE, in the caller's thread, so a bad value is refused with
    # a ValueError instead of reaching (and possibly killing) the control loop.

    def _enqueue(self, *cmd) -> int:
        with self._cmd_seq_lock:
            self._cmd_seq += 1
            seq = self._cmd_seq
            self._commands.put((seq,) + cmd)
        return seq

    def set_current(self, amps: float) -> int:
        return self._enqueue("set_current", _finite("current_A", amps))

    def set_field(self, field_mT: float, use_pid: bool = True) -> int:
        return self._enqueue("set_field", _finite("field_mT", field_mT), use_pid)

    def demag(self, amplitude_A: float) -> int:
        return self._enqueue("demag", _finite("amplitude_A", amplitude_A))

    def calibrate(self, n_per_leg: int = 50, dwell_s: float = 0.5,
                  on_done: Optional[Callable[[FieldCalibration], None]] = None) -> int:
        # The sweep grid divides by (n_per_leg - 1): 1 point was a
        # ZeroDivisionError and 0 an IndexError -- both on the control thread,
        # which they killed.
        n_per_leg = int(n_per_leg)
        if n_per_leg < 2:
            raise ValueError(f"n_per_leg must be >= 2, got {n_per_leg}")
        dwell_s = _finite("dwell_s", dwell_s)
        if dwell_s < 0:
            raise ValueError(f"dwell_s must be >= 0, got {dwell_s}")
        return self._enqueue("calibrate", n_per_leg, dwell_s, on_done)

    def set_lock(self, locked: bool) -> int:
        return self._enqueue("lock", locked)

    def apply_config(self) -> None:
        """Re-read tunables that were baked into derived objects at construction.

        Most parameters (limits, stabilizer gain, Hall conversion, acquisition
        profiles) are read from cfg live on every tick, so editing cfg in place
        is enough. The PI gains and the ramp increment live inside their own
        objects, so we copy them across here. Call after editing Settings."""
        self.pi.Kc = self.cfg.pid.Kc_A_per_mT
        self.pi.Ti = self.cfg.pid.Ti_s
        self.ramper.increment = abs(self.cfg.ramp.increment_A)
        self.ramper.delay_s = self.cfg.ramp.delay_s
        self._event("info", "settings applied")

    # Uniform accessors so a GUI can treat a Controller and a ClMagClient the same.
    def get_config(self) -> Config:
        return self.cfg

    def get_calibration(self):
        return self.calibration

    def set_calibration(self, cal) -> None:
        self.calibration = cal
        if cal is not None:
            self._event("info", f"calibration set ({len(cal.currents_A)} points)")

    def status(self) -> Status:
        # Read cmd_done FIRST: the control thread changes the state and only
        # THEN advances cmd_done, so a count read before the state can never
        # be newer than the state published with it.
        cmd_done = self._cmd_done
        _, field = self.acq.latest.get()
        return Status(
            state=self._state.value,
            setpoint_field_mT=self._setpoint_field,
            measured_field_mT=field,
            current_A=self.ramper.setpoint,
            field_stable=self._field_stable,
            locked=self.locked,
            aux=self.aux_snapshot(),
            output_on=self._output_on,
            cmd_done=cmd_done,
        )

    # ---- AUX I/O (general-purpose DAQ, independent of the field loop) -----

    def aux_set_ao(self, channel: str, volts: float) -> None:
        if self.aux is None:
            return
        volts = _finite("volts", volts)      # NaN used to come out as +v_max
        lo, hi = self.cfg.aux.v_min, self.cfg.aux.v_max
        clamped = max(lo, min(hi, volts))
        with self._aux_lock:
            self.aux.set_ao(channel, clamped)
        if clamped != volts:
            self._event("error", f"AO {channel} {volts:.3f} V out of ±{hi} V -> clamped")
        else:
            self._event("info", f"AO {channel} -> {clamped:.3f} V")

    def aux_set_do(self, line: str, state: bool) -> None:
        if self.aux is None:
            return
        with self._aux_lock:
            self.aux.set_do(line, bool(state))
        self._event("info", f"DO {line} -> {'HIGH' if state else 'LOW'}")

    def aux_read_ai(self, channel: str) -> float:
        if self.aux is None:
            return 0.0
        with self._aux_lock:
            return self.aux.read_ai(channel)

    def aux_snapshot(self) -> Optional[dict]:
        """Current AUX state: commanded AOs, live AI reads, DO states."""
        if self.aux is None:
            return None
        ax = self.cfg.aux
        with self._aux_lock:
            return {
                "ao": {ch: self.aux.read_ao(ch) for ch in ax.ao_list()},
                "ai": {ch: self.aux.read_ai(ch) for ch in ax.ai_list()},
                "do": {ln: self.aux.read_do(ln) for ln in ax.do_list()},
            }

    # --------------------------------------------------------------- helpers

    def _event(self, level: str, msg: str) -> None:
        self._on_event(level, msg)

    def _clamp_current(self, amps: float) -> float:
        lim = self.cfg.limits.current_max_A
        if amps > lim:
            self._event("error", f"current {amps:.3f} A over +{lim} A limit -> clamped")
            return lim
        if amps < -lim:
            self._event("error", f"current {amps:.3f} A over -{lim} A limit -> clamped")
            return -lim
        return amps

    def _take_control(self) -> None:
        """Called (on the control thread) when a command starts to move the
        current. From here on the loop commands the supply every tick.

        If the output was off, first program the present ramp value (the
        current actually flowing = 0 A) and only THEN switch the output on:
        the supply may still hold an old programmed current, and OUTP ON would
        otherwise step the magnet straight to it.

        Open point for the real driver (# VERIFY): if the supply is found with
        its output ON but in VOLTAGE mode (FUNC:MODE? = VOLT), the CURR values
        sent below would only move the current LIMIT. The driver's open()
        should query FUNC:MODE? so this case can be refused or reported rather
        than silently mis-driven; switching mode with the output on is a step."""
        if not self._output_on:
            self.kepco.set_current(self.ramper.setpoint)
            self.kepco.enable_output()
            self._output_on = True
            self._event("info", "supply output switched ON")
        self._driving = True

    def _require_calibration(self) -> bool:
        if self.calibration is None or not self.calibration.currents_A:
            self._event("error", "no calibration loaded; cannot set a field")
            return False
        return True

    # ---------------------------------------------------------- command drain

    def _drain_commands(self, now: float) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            seq, cmd = cmd[0], cmd[1:]
            try:
                self._do_command(cmd, now)
            finally:
                # advanced even if the command was refused or raised: "done"
                # means "taken up", and a waiting client must not hang on it.
                self._cmd_done = seq

    def _do_command(self, cmd, now: float) -> None:
        kind = cmd[0]
        if kind == "lock":
            self.locked = cmd[1]
            self._event("info", f"external lock {'engaged' if cmd[1] else 'released'}")
        elif kind == "set_current":
            self._begin_set_current(cmd[1])
        elif kind == "set_field":
            self._begin_set_field(cmd[1], cmd[2], now)
        elif kind == "demag":
            self._begin_demag(cmd[1])
        elif kind == "calibrate":
            self._begin_calibrate(cmd[1], cmd[2], cmd[3], now)

    # ----------------------------------------------------- command beginnings

    def _begin_set_current(self, amps: float) -> None:
        amps = self._clamp_current(amps)
        self._take_control()
        self._setpoint_field = None
        self._field_stable = False
        self._settling = False
        self._output_frozen = False
        self.ramper.go_to(amps)
        self._after_ramp = State.IDLE
        self._state = State.RAMPING
        self._event("info", f"set current -> {amps:.3f} A")

    def _begin_set_field(self, field_mT: float, use_pid: bool, now: float) -> None:
        if not self._require_calibration():
            return
        # A target outside the calibrated range cannot be reached (the range
        # ends at +-current_max), so it is refused like "no calibration": no
        # adopted setpoint, one red event. It used to be accepted, jump to the
        # full 3 A and sit in SEEK for ever. describe advertises exactly this
        # range as the field's min/max.
        lo, hi = self.calibration.range_mT
        if not (lo <= field_mT <= hi):
            self._event("error", f"field {field_mT:.3f} mT is outside the calibrated "
                                 f"range {lo:.3f}..{hi:.3f} mT; refused")
            return
        # Asking again for the field we are ALREADY stable at (and still within
        # tolerance of) is a no-op. It used to start a whole new seek: jump
        # field_step BELOW the target and climb back. Meanwhile a client still
        # saw the previous frame -- same setpoint, field_stable True -- and read
        # its detector while the magnet was being pulled 2 mT away. That is
        # gotcha #2 in a form the setpoint-adoption guard cannot catch, because
        # the old and the new setpoint are the same number.
        if (use_pid and self._state == State.STABLE and self._field_stable
                and self._setpoint_field == field_mT):
            _, measured = self.acq.latest.get()
            if abs(measured - field_mT) <= self.cfg.limits.field_tolerance_mT:
                self._event("info", f"already stable at {field_mT:.3f} mT")
                return
        self._take_control()
        self._setpoint_field = field_mT
        self._field_stable = False
        self._settling = False
        self._output_frozen = False
        _, measured = self.acq.latest.get()
        self._approach_sign = 1 if field_mT >= measured else -1

        if use_pid:
            # deliberately undershoot by field_step on the approach side, so the
            # PI only ever pushes one way and cannot overshoot.
            detuned = field_mT - self._approach_sign * self.cfg.limits.field_step_mT
            self._jump_current = self._clamp_current(self.calibration.current_for_field(detuned))
            self._target_current_est = self._clamp_current(self.calibration.current_for_field(field_mT))
            self.pi.reset()
            self._settling = False
            self._stable_since = None
            self.ramper.go_to(self._jump_current)
            self._after_ramp = State.SEEK
            self._state = State.RAMPING
            self._event("info", f"set field -> {field_mT:.3f} mT (seek; jump to "
                                f"{detuned:.2f} mT / {self._jump_current:.3f} A)")
        else:
            target_I = self._clamp_current(self.calibration.current_for_field(field_mT))
            self.ramper.go_to(target_I)
            self._after_ramp = State.HOLD
            self._state = State.RAMPING
            self._event("info", f"set field -> {field_mT:.3f} mT (calibration only, "
                                f"{target_I:.3f} A)")

    def _begin_demag(self, amplitude_A: float) -> None:
        amplitude_A = min(abs(amplitude_A), self.cfg.limits.current_max_A)
        # exponentially/linearly decaying alternating steps down to zero
        seq: List[float] = []
        k = 0
        while True:
            mag = amplitude_A * (1.0 - 0.05 * k)
            if mag <= 1e-6:
                break
            seq.append(mag if k % 2 == 0 else -mag)
            k += 1
        seq.append(0.0)
        self._take_control()
        self._seq = seq
        self._seq_i = 0
        self._setpoint_field = None
        self._field_stable = False
        self._settling = False
        self._output_frozen = False
        self.ramper.go_to(seq[0])
        self._state = State.DEMAG
        self._event("info", f"demag start, amplitude {amplitude_A:.3f} A, {len(seq)} steps")

    def _begin_calibrate(self, n_per_leg: int, dwell_s: float,
                         on_done, now: float) -> None:
        self._take_control()
        I_max = self.cfg.limits.current_max_A
        up = [(-I_max) + (2 * I_max) * i / (n_per_leg - 1) for i in range(n_per_leg)]
        self._seq = up + list(reversed(up))
        self._seq_i = 0
        self._cal_points = []
        self._cal_dwell_s = dwell_s
        self._cal_after = on_done
        self._setpoint_field = None
        self._field_stable = False
        self._settling = False
        self._output_frozen = False
        self.acq.set_profile("precise")
        self.ramper.go_to(self._seq[0])
        self._phase = "ramp"
        self._state = State.CALIBRATE
        self._event("info", f"calibration start, {len(self._seq)} points")

    # ------------------------------------------------------------ the run loop

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            dt = now - last
            last = now

            # The loop must NEVER die (the same rule as the service's command
            # loop). A dead control thread used to leave the magnet at whatever
            # current it had, while every later command was still answered
            # "ok" by the service and then silently never executed.
            try:
                self._iteration(now, dt)
            except Exception as exc:                     # noqa: BLE001
                self._on_loop_error(exc, now)
            time.sleep(max(0.0, self.cfg.ramp.delay_s))

    def _iteration(self, now: float, dt: float) -> None:
        self._drain_commands(now)
        self._check_acquisition()
        _, field = self.acq.latest.get()
        self._tick(now, dt, field)

        # command the supply. While the output is frozen (settling on a
        # field target) we hold EXACTLY the same value so the ramp direction
        # cannot flip; otherwise we advance the ramp one step toward target.
        # Before the first command (_driving False) nothing is sent at all:
        # the supply keeps the state it was found in.
        if not self._driving:
            pass
        elif self._output_frozen:
            self.kepco.set_current(self._freeze_current)
        else:
            self.ramper.step()
            self.kepco.set_current(self.ramper.setpoint)
        self._log(now, field)

    def _on_loop_error(self, exc: Exception, now: float) -> None:
        """One control tick raised. Stop WHERE WE ARE (no further ramping, no
        seek, not stable), report it, and keep the loop alive so the next
        command -- and the shutdown ramp to zero -- still work."""
        self._state = State.IDLE
        self._setpoint_field = None
        self._field_stable = False
        self._settling = False
        self._output_frozen = False
        self.ramper.go_to(self.ramper.setpoint)
        # A dead instrument fails EVERY tick: report the first failure and then
        # at most every 2 s, or the event log becomes a 100-lines/s flood.
        msg = f"control loop error: {type(exc).__name__}: {exc}"
        last_msg, last_t = self._last_err
        if msg != last_msg or now - last_t >= 2.0:
            self._last_err = (msg, now)
            self._event("error", msg + " -- stopped where it was (IDLE)")

    def _check_acquisition(self) -> None:
        """Report failed Hall-probe reads. The acquisition thread survives them
        but has no event channel of its own, and while reads fail the measured
        field is the LAST GOOD one -- the operator has to be told."""
        n = getattr(self.acq, "errors", 0)
        if n != self._acq_errors_seen:
            self._acq_errors_seen = n
            self._event("error", f"Hall probe read failed ({n} so far): "
                                 f"{getattr(self.acq, 'last_error', '')}")

    def _tick(self, now: float, dt: float, field: float) -> None:
        st = self._state
        if st == State.IDLE:
            pass
        elif st == State.RAMPING:
            self._tick_ramping(now)
        elif st == State.SEEK:
            self._tick_seek(now, dt, field)
        elif st in (State.STABLE, State.HOLD):
            self._tick_hold(field)
        elif st == State.DEMAG:
            self._tick_demag()
        elif st == State.CALIBRATE:
            self._tick_calibrate(now, field)

    def _tick_ramping(self, now: float) -> None:
        if not self.ramper.done:
            return
        nxt = self._after_ramp
        if nxt == State.SEEK:
            self.acq.set_profile("fast")
            self._state = State.SEEK
            self._event("info", "jump complete -> PI seek")
        elif nxt == State.HOLD:
            self._hold_current = self.ramper.setpoint
            self._state = State.HOLD
            self._event("info", "field set (calibration only), holding")
        else:
            self._state = State.IDLE

    def _tick_seek(self, now: float, dt: float, field: float) -> None:
        error = self._setpoint_field - field
        tol = self.cfg.limits.field_tolerance_mT

        if self._settling:
            # PI is frozen and the current is held (the main loop keeps commanding
            # the same setpoint, so the ramp direction never flips and hysteresis
            # stays put). We just confirm the field holds within tolerance.
            if abs(error) <= tol:
                if self._stable_since is None:
                    self._stable_since = now
                elif now - self._stable_since >= self.cfg.limits.stable_time_s:
                    self._enter_stable()
            else:
                # a clean (precise) reading says we are genuinely off -> unfreeze
                # and resume the push, but KEEP the integral so we do not re-jump.
                self._settling = False
                self._output_frozen = False
                self._stable_since = None
                self.acq.set_profile("fast")
            return

        # active seek: PI drives the correction, clamped to one direction so we
        # only ever approach from the detuned side and never reverse.
        correction = self.pi.update(error, dt, clamp_sign=self._approach_sign)
        cmd = self._jump_current + correction
        # never command current past the calibration's target estimate (+margin):
        # this bounds any overshoot regardless of PI gains. The cap itself is
        # kept inside +-current_max: near the top of the range est + 0.08 A lies
        # beyond 3 A, and _clamp_current then reported "over limit" in red on
        # EVERY 10 ms tick of a seek that could not arrive (100 lines/s).
        lim = self.cfg.limits.current_max_A
        if self._approach_sign > 0:
            cmd = min(cmd, self._target_current_est + self._seek_cap_margin_A, lim)
        else:
            cmd = max(cmd, self._target_current_est - self._seek_cap_margin_A, -lim)
        self.ramper.go_to(self._clamp_current(cmd))

        if abs(error) <= tol / 2:
            # close enough: hard-freeze the output at the present current, switch
            # to the clean profile, and start the stability dwell.
            self._settling = True
            self._output_frozen = True
            self._freeze_current = self.ramper.setpoint
            self._stable_since = now
            self.acq.set_profile("precise")

    def _enter_stable(self) -> None:
        self._field_stable = True
        self._settling = False
        self._hold_current = self._freeze_current
        # sync the ramper to the held value and release the hard freeze, so the
        # long-term stabilizer (deadbanded) can make slow trims from here.
        self.ramper.sync_to(self._freeze_current)
        self._output_frozen = False
        self._state = State.STABLE
        self.acq.set_profile("precise")
        self._event("info", f"field stable at {self._setpoint_field:.3f} mT")

    def _tick_hold(self, field: float) -> None:
        # long-term stabilizer: a slow proportional trim against drift, only
        # while idle-holding (never during an active seek). Deadband inside the
        # tolerance so we do NOT dither the current (and flip hysteresis) while
        # the field is already good enough.
        if self.stabilizer_enabled and self._setpoint_field is not None:
            error = self._setpoint_field - field
            if abs(error) > self.cfg.limits.field_tolerance_mT:
                trim = error * self.cfg.stabilizer.gain_A_per_mT
                self.ramper.go_to(self._clamp_current(self._hold_current + trim))

    def _tick_demag(self) -> None:
        if not self.ramper.done:
            return
        # current reached this step; advance (no dwell) to the next
        self._seq_i += 1
        if self._seq_i >= len(self._seq):
            self._state = State.IDLE
            self._event("info", "demag complete (current at zero)")
            return
        self.ramper.go_to(self._seq[self._seq_i])

    def _tick_calibrate(self, now: float, field: float) -> None:
        if self._phase == "ramp":
            if self.ramper.done:
                self._phase = "dwell"
                self._phase_t = now
        elif self._phase == "dwell":
            if now - self._phase_t >= self._cal_dwell_s:
                # measure: the acquisition thread is on the precise profile, and
                # we have dwelled at least one full precise read, so latest is fresh
                self._cal_points.append((self.ramper.setpoint, field))
                self._seq_i += 1
                if self._seq_i >= len(self._seq):
                    self._finish_calibrate()
                    return
                self.ramper.go_to(self._seq[self._seq_i])
                self._phase = "ramp"

    def _finish_calibrate(self) -> None:
        hall = self.cfg.hall
        cal = FieldCalibration.from_sweep(self._cal_points, hall=hall, subtract_remanence=True)
        self.calibration = cal
        lo, hi = cal.range_mT
        self._event("info", f"calibration done: {len(cal.currents_A)} pts, "
                            f"{lo:.1f}..{hi:.1f} mT")
        if self._cal_after:
            self._cal_after(cal)
        self._state = State.IDLE

    # ------------------------------------------------------------- shutdown ramp

    def _ramp_to_zero_blocking(self) -> None:
        self.ramper.go_to(0.0)
        while not self.ramper.done:
            self.ramper.step()
            self.kepco.set_current(self.ramper.setpoint)
            time.sleep(self.cfg.ramp.delay_s)

    def _log(self, now: float, field: float) -> None:
        if self._csv:
            self._csv.write(f"{now:.4f},{self.ramper.setpoint:.5f},"
                            f"{field:.5f},{self._state.value}\n")
