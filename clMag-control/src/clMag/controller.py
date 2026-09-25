"""The Controller: the state machine that runs the magnet.

This is the Python replacement for the big LabVIEW while-loop-with-a-case-
structure. It owns the hardware handles and runs ONE control loop in its own
thread. External callers never touch the hardware directly; they drop commands
into a thread-safe queue (set_current, set_field, demag, calibrate, ...), and
the loop processes them. That same command model is exactly what the ZeroMQ
service will sit on top of in Session 5, so nothing here has to change then.

States
------
IDLE      : holding whatever current we have, not regulating a field.
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
        self.kepco.open()
        if self.aux is not None:
            self.aux.open()
        if not self.acq.is_alive():
            self.acq.start()
        self.ramper.sync_to(self.kepco.read_current())
        self._stop.clear()
        if self._csv_path:
            self._csv = open(self._csv_path, "w", encoding="utf-8")
            self._csv.write("time_s,current_A,field_mT,state\n")
        self._thread = threading.Thread(target=self._run, name="control", daemon=True)
        self._thread.start()
        self._event("info", "controller started")

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

    def set_current(self, amps: float) -> None:
        self._commands.put(("set_current", amps))

    def set_field(self, field_mT: float, use_pid: bool = True) -> None:
        self._commands.put(("set_field", field_mT, use_pid))

    def demag(self, amplitude_A: float) -> None:
        self._commands.put(("demag", amplitude_A))

    def calibrate(self, n_per_leg: int = 50, dwell_s: float = 0.5,
                  on_done: Optional[Callable[[FieldCalibration], None]] = None) -> None:
        self._commands.put(("calibrate", n_per_leg, dwell_s, on_done))

    def set_lock(self, locked: bool) -> None:
        self._commands.put(("lock", locked))

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
        _, field = self.acq.latest.get()
        return Status(
            state=self._state.value,
            setpoint_field_mT=self._setpoint_field,
            measured_field_mT=field,
            current_A=self.ramper.setpoint,
            field_stable=self._field_stable,
            locked=self.locked,
            aux=self.aux_snapshot(),
        )

    # ---- AUX I/O (general-purpose DAQ, independent of the field loop) -----

    def aux_set_ao(self, channel: str, volts: float) -> None:
        if self.aux is None:
            return
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

            self._drain_commands(now)
            _, field = self.acq.latest.get()
            self._tick(now, dt, field)

            # command the supply. While the output is frozen (settling on a
            # field target) we hold EXACTLY the same value so the ramp direction
            # cannot flip; otherwise we advance the ramp one step toward target.
            if self._output_frozen:
                self.kepco.set_current(self._freeze_current)
            else:
                self.ramper.step()
                self.kepco.set_current(self.ramper.setpoint)
            self._log(now, field)

            time.sleep(self.cfg.ramp.delay_s)

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
        # this bounds any overshoot regardless of PI gains.
        if self._approach_sign > 0:
            cmd = min(cmd, self._target_current_est + self._seek_cap_margin_A)
        else:
            cmd = max(cmd, self._target_current_est - self._seek_cap_margin_A)
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
