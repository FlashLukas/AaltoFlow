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
STABLE    : field reached and verified; the slow long-term stabilizer trims
            drift (PI does NOT re-engage on drift). field_stable is True while
            the field stays within tolerance and DROPS if it drifts out for
            stable_time (Lukas, 2026-09-28) -- the state stays STABLE, the
            flag says whether a measurement may trust the field right now.
HOLD      : holding a field set by calibration only (no PI verification);
            the stabilizer trims here too, field_stable stays False.
SWEEP     : the field SWEEPS at a set pace (mT/s) towards a target, for a fly
            scan (ramp_field, 2026-10-09). The setpoint moves along a straight
            line in time; the current follows it through the calibration
            (feed-forward) plus a PI on the MEASURED field, and never steps
            back against the sweep direction (the hysteresis rule of gotcha
            #11: a current that dithers flips the iron's branch). No freeze
            while sweeping -- a frozen output cannot follow a moving setpoint.
            At the target the usual endgame takes over: freeze within tol/2,
            or a one-way PI seek for the rest, -> STABLE as for set_field.
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
from .stream import StreamRecorder


class State(str, Enum):
    IDLE = "IDLE"
    RAMPING = "RAMPING"
    SEEK = "SEEK"
    STABLE = "STABLE"
    HOLD = "HOLD"
    DEMAG = "DEMAG"
    CALIBRATE = "CALIBRATE"
    SWEEP = "SWEEP"


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
    # The suite convention (2026-09-28): the text of a hardware failure that
    # is happening NOW -- the Hall probe cannot be read, or the supply does
    # not take the current -- and "" when everything answers. While it is
    # set, measured_field_mT / current_A are the LAST GOOD values, not live
    # ones; scan-core pauses a scan on it.
    hw_error: str = ""
    # The last internal control-loop error (the loop then HOLDS the current
    # and goes IDLE, see _on_loop_error). Sticky until the next command, so a
    # one-off failure is still visible after the loop has recovered.
    loop_error: str = ""
    stabilizer: bool = True           # long-term stabilizer switched on
    stabilizer_trim_A: float = 0.0    # current it has added since STABLE/HOLD
    # The field SWEEP (ramp_field). `ramp_id` = the number of the newest sweep
    # the control thread has TAKEN UP (ramp_field returns its own number), so
    # "ramp_id >= mine and not ramping" is the honest "my sweep is over" --
    # a ramping=False frame from before the sweep began cannot pass for it.
    ramping: bool = False
    ramp_id: int = 0
    ramp_target_mT: Optional[float] = None
    ramp_rate_mT_per_s: Optional[float] = None


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

        # field_stable drop filter (STABLE only): when the field first went
        # out of tolerance, and how many distinct readings were out since.
        self._out_since: Optional[float] = None
        self._out_readings = 0
        self._out_last_t: Optional[float] = None

        # Long-term stabilizer (integrating; see config.Stabilizer and
        # _tick_stabilizer). Plain attributes, copied into status (gotcha #1).
        self._stab_base = 0.0          # current when STABLE/HOLD was entered
        self._stab_trim = 0.0          # accumulated correction on top of it
        self._stab_dir = 1             # approach direction = hysteresis branch
        self._stab_window_t = 0.0      # collect readings from this time on
        self._stab_samples: List[float] = []
        self._stab_last_t: Optional[float] = None
        self._stab_pending: Optional[float] = None   # target after a back-step
        self._stab_at_limit = False

        # Direction of the last change of the commanded current (+1 / -1).
        # The iron's hysteresis branch follows it, so the stabilizer needs it.
        self._drive_dir = 0

        # error reporting (see Status.hw_error / loop_error)
        self._supply_error = ""
        self._loop_error = ""

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

        # the field SWEEP (state SWEEP; see the module doc). Plain attributes
        # the control thread owns; status() copies them (gotcha #1).
        self._ramp_seq = 0             # handed out by ramp_field (caller thread)
        self._ramp_taken = 0           # newest sweep the control thread took up
        self._sweep_from = 0.0
        self._sweep_to = 0.0
        self._sweep_rate = 0.0
        self._sweep_sign = 1
        self._sweep_t0 = 0.0
        self._sweep_off = 0.0          # supply current minus calibration at the start
        self._ramp_target: Optional[float] = None
        self._ramp_rate: Optional[float] = None

        # THE STREAM (fly scans): every Hall-probe reading with its time,
        # the setpoint and the current at that moment. The acquisition thread
        # appends to it (cheap: one lock, one deque append); the service's
        # stream verbs hand it out. Recording only while a stream is started.
        self.recorder = StreamRecorder(["field", "setpoint", "current"])
        self.acq.on_reading = self._record_reading

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

    def shutdown(self, keep_outputs: bool = False) -> None:
        """Safe stop: ramp to zero, output off, threads down. Safe to call on a
        crash or a lost client.

        keep_outputs=True is a RESTART for a code update (Lukas, 2026-10-06):
        the threads stop and the instruments are closed exactly the same, but
        the current is NOT ramped down and the output stays on -- the magnet
        keeps its field, and the next start adopts the supply's state. Only the
        `shutdown{keep_outputs: true}` verb asks for this; every other path
        (window close, Ctrl-C, crash) keeps the safe default."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        try:
            if not keep_outputs:
                self._ramp_to_zero_blocking()
        finally:
            if keep_outputs:
                # disconnect WITHOUT the OUTP OFF a normal close sends
                self.kepco.close(output_off=False)
            else:
                self.kepco.close()
            if self.aux is not None:
                self.aux.close()          # never writes an AO/DO value
            self.acq.stop()
            if self._csv:
                self._csv.close()
                self._csv = None
        self._event("info", "controller shut down (output left as it is)"
                    if keep_outputs else "controller shut down (output off)")

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

    def ramp_to_zero(self) -> int:
        """Ramp the current to 0 A (the GUI's "Ramp to Zero & Stop"). Same as
        set_current(0); a name of its own because over the wire it is the
        SAFETY verb a viewer may always send (net/service.py, control)."""
        return self.set_current(0.0)

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

    def ramp_field(self, field_mT: float, rate_mT_per_s: float) -> int:
        """SWEEP the field to `field_mT` at `rate_mT_per_s`; returns the sweep's
        number (status `ramp_id` reaches it when the control thread has taken
        it up, and `ramping` goes False when the setpoint has arrived).

        Checked HERE, in the caller's thread, so the service can answer
        ok:false: no calibration, or a target outside the calibrated range,
        is REFUSED (a sweep cannot be asked to go where set_field may not).
        A rate outside the configured limits is clamped, with a warning,
        like every other setter of this module."""
        field_mT = _finite("field_mT", field_mT)
        rate = abs(_finite("rate_mT_per_s", rate_mT_per_s))
        cal = self.calibration
        if cal is None or not cal.currents_A:
            raise ValueError("no calibration loaded; cannot sweep the field")
        lo, hi = cal.range_mT
        if not lo <= field_mT <= hi:
            raise ValueError(f"field {field_mT:.3f} mT is outside the calibrated "
                             f"range {lo:.3f}..{hi:.3f} mT")
        lim = self.cfg.limits
        r = max(lim.sweep_rate_min_mT_per_s, min(lim.sweep_rate_max_mT_per_s, rate))
        if r != rate:
            self._event("warn", f"sweep rate {rate:g} mT/s clamped to {r:g} mT/s "
                                f"(limits {lim.sweep_rate_min_mT_per_s:g}.."
                                f"{lim.sweep_rate_max_mT_per_s:g})")
        with self._cmd_seq_lock:
            self._ramp_seq += 1
            rid = self._ramp_seq
        self._enqueue("ramp", rid, field_mT, r)
        return rid

    def ramp_stop(self) -> int:
        """End a sweep WHERE IT IS: the current is held, the state goes IDLE
        (a scan's Abort). Does nothing when no sweep runs."""
        return self._enqueue("ramp_stop")

    # the stream verbs (fly scans): see self.recorder
    def stream_start(self) -> int:
        return self.recorder.start()

    def stream_read(self) -> dict:
        return self.recorder.read()

    def stream_stop(self) -> dict:
        return self.recorder.stop()

    def _record_reading(self, t_wall: float, field: float) -> None:
        """The acquisition thread's hook: one reading into the stream. Reads
        two floats the control thread writes -- each read is atomic, and a
        setpoint one tick (10 ms) old is as good as the reading itself."""
        sp = self._setpoint_field
        self.recorder.append(t_wall, (field, float("nan") if sp is None else sp,
                                      self.ramper.setpoint))

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
        # the same for the sweep number: the control thread enters SWEEP and
        # only THEN publishes the number it took up
        ramp_taken = self._ramp_taken
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
            hw_error=self._hw_error_text(),
            loop_error=self._loop_error,
            stabilizer=bool(self.stabilizer_enabled),
            stabilizer_trim_A=self._stab_trim,
            ramping=self._state == State.SWEEP,
            ramp_id=ramp_taken,
            ramp_target_mT=self._ramp_target,
            ramp_rate_mT_per_s=self._ramp_rate,
        )

    def _hw_error_text(self) -> str:
        parts = []
        acq_err = getattr(self.acq, "hw_error", "")
        if acq_err:
            parts.append(f"Hall probe: {acq_err}")
        if self._supply_error:
            parts.append(f"supply: {self._supply_error}")
        return "; ".join(parts)

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
            # a new command starts with a clean slate: the last loop error
            # stays visible in status until the operator does something else.
            self._loop_error = ""
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
        elif kind == "ramp":
            try:
                self._begin_sweep(cmd[2], cmd[3], now)
            finally:
                # published even if refused (the calibration went away since
                # ramp_field checked): a waiting client must see it "over"
                self._ramp_taken = cmd[1]
        elif kind == "ramp_stop":
            self._stop_sweep()

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
            self._write_supply(self._freeze_current)
        else:
            prev = self.ramper.setpoint
            self.ramper.step()
            try:
                self._write_supply(self.ramper.setpoint)
            except Exception:
                # The step did NOT reach the supply, so take it back: the
                # ramper must describe what the magnet really has, or the
                # "hold where we are" of _on_loop_error would hold one ramp
                # step (0.05 A) further than the magnet ever went -- and the
                # next good write would then move the magnet there AFTER
                # the error. (Found 2026-09-28.)
                self.ramper.sync_to(prev)
                raise
            if self.ramper.setpoint != prev:
                self._drive_dir = 1 if self.ramper.setpoint > prev else -1
        self._log(now, field)

    def _write_supply(self, amps: float) -> None:
        """Send the current to the supply; a failure is a HARDWARE error
        (status hw_error) until the next write goes through."""
        try:
            self.kepco.set_current(amps)
        except Exception as exc:                          # noqa: BLE001
            self._supply_error = f"{type(exc).__name__}: {exc}"
            raise
        self._supply_error = ""

    def _on_loop_error(self, exc: Exception, now: float) -> None:
        """One control tick raised. Stop WHERE WE ARE (no further ramping, no
        seek, not stable), report it, and keep the loop alive so the next
        command -- and the shutdown ramp to zero -- still work.

        Why HOLD and not ramp to zero (Lukas, 2026-09-28): the error may be a
        one-off (a GPIB timeout), and dropping the field would destroy the
        sample's magnetic state for nothing; ramping down through a supply
        that just failed to answer is not safer either. The current stays
        where it last got to, the state goes IDLE (a scan waiting on
        field_stable waits), and the error is published as `loop_error` (and
        as `hw_error` while the supply keeps failing) plus a red event."""
        self._state = State.IDLE
        self._setpoint_field = None
        self._field_stable = False
        self._settling = False
        self._output_frozen = False
        self.ramper.go_to(self.ramper.setpoint)
        # A dead instrument fails EVERY tick: report the first failure and then
        # at most every 2 s, or the event log becomes a 100-lines/s flood.
        msg = f"control loop error: {type(exc).__name__}: {exc}"
        self._loop_error = msg + " -- current held, IDLE"
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
            self._tick_hold(now, field)
        elif st == State.DEMAG:
            self._tick_demag()
        elif st == State.CALIBRATE:
            self._tick_calibrate(now, field)
        elif st == State.SWEEP:
            self._tick_sweep(now, dt, field)

    # ---- the field SWEEP (ramp_field) --------------------------------------

    def _begin_sweep(self, to: float, rate: float, now: float) -> None:
        if not self._require_calibration():
            return
        cal = self.calibration
        # Start from the field we HAVE: the setpoint when it was reached (a
        # STABLE/HOLD field is the setpoint, without the probe's noise), the
        # measured field otherwise.
        _, measured = self.acq.latest.get()
        if self._state in (State.STABLE, State.HOLD) and self._setpoint_field is not None:
            start = self._setpoint_field
        else:
            start = measured
        self._take_control()
        self._sweep_from, self._sweep_to, self._sweep_rate = start, to, rate
        self._sweep_sign = 1 if to >= start else -1
        self._sweep_t0 = now
        # The calibration is the AVERAGE of the up and down legs; the iron is
        # on one branch of the loop, off that average by a few tenths of a mT
        # in current terms. Keep the present difference as an offset, so the
        # feed-forward starts where the magnet really is (the PI trims the
        # rest, including the branch change if the sweep reverses direction).
        self._sweep_off = self.ramper.setpoint - cal.current_for_field(start)
        self.pi.reset()
        self._setpoint_field = start
        self._field_stable = False
        self._settling = False
        self._output_frozen = False
        self._approach_sign = self._sweep_sign
        self._ramp_target, self._ramp_rate = to, rate
        # fast readings: a fly scan bins by them, and 4 ms readings follow a
        # moving field far better than 100 ms means
        self.acq.set_profile("fast")
        self._state = State.SWEEP
        self._event("info", f"field sweep {start:.3f} -> {to:.3f} mT at {rate:g} mT/s")

    def _tick_sweep(self, now: float, dt: float, field: float) -> None:
        span = abs(self._sweep_to - self._sweep_from)
        travelled = self._sweep_rate * (now - self._sweep_t0)
        arrived = travelled >= span
        b_cmd = (self._sweep_to if arrived
                 else self._sweep_from + self._sweep_sign * travelled)
        self._setpoint_field = b_cmd
        error = b_cmd - field
        corr = self.pi.update(error, dt)       # two-sided while the setpoint moves
        amps = self.calibration.current_for_field(b_cmd) + self._sweep_off + corr
        # NEVER step back against the sweep (gotcha #11): a current that
        # reverses, even by a few mA, flips the iron onto the other branch of
        # its loop and the field jumps by 2h. So the commanded current only
        # moves the sweep's way -- if the field runs ahead, the current waits
        # for the setpoint to catch up. While it waits the integral must not
        # keep growing (anti-windup), or the field would lag behind afterwards.
        last = self.ramper.target
        if (amps - last) * self._sweep_sign < 0:
            amps = last
            self.pi.unwind(error, dt)
        lim = self.cfg.limits.current_max_A
        amps = max(-lim, min(lim, amps))       # quiet: the target is in range
        self.ramper.go_to(amps)
        if arrived:
            self._end_sweep(now, field)

    def _end_sweep(self, now: float, field: float) -> None:
        """The setpoint has reached the target: settle there exactly as a
        set_field would end, without going back."""
        to, sign = self._sweep_to, self._sweep_sign
        tol = self.cfg.limits.field_tolerance_mT
        error = to - field
        self._setpoint_field = to
        self._approach_sign = sign
        self._event("info", f"field sweep reached {to:.3f} mT; settling")
        if abs(error) <= tol / 2:
            # already there: freeze now (the field approached from the sweep's
            # side, so the branch is the right one)
            self.ramper.go_to(self.ramper.setpoint)
            self._settling = True
            self._output_frozen = True
            self._freeze_current = self.ramper.setpoint
            self._stable_since = now
            self.acq.set_profile("precise")
            self._state = State.SEEK
        elif error * sign > 0:
            # behind (the coil lags): a one-way PI seek for the rest, starting
            # from the present current. Its overshoot cap must not lie BELOW
            # the current already flowing, or the seek would pull it back.
            here = self.ramper.setpoint
            est = self.calibration.current_for_field(to) + self._sweep_off
            self._jump_current = here
            self._target_current_est = max(est, here) if sign > 0 else min(est, here)
            self.pi.reset()
            self._stable_since = None
            self._state = State.SEEK
        else:
            # ran past the target by more than tol/2: the ordinary set_field
            # (undershoot, then a one-way seek) brings it back properly
            self._event("info", f"sweep overshot ({field:.3f} mT); re-seeking")
            self._begin_set_field(to, True, now)

    def _stop_sweep(self) -> None:
        if self._state != State.SWEEP:
            return
        _, field = self.acq.latest.get()
        self._state = State.IDLE
        self._setpoint_field = None
        self._field_stable = False
        self.ramper.go_to(self.ramper.setpoint)     # hold the current we have
        self.acq.set_profile("precise")
        self._event("info", f"field sweep stopped at {field:.3f} mT "
                            f"({self.ramper.setpoint:.3f} A held, IDLE)")

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
            self._stab_reset(now, self._drive_dir or self._approach_sign or 1)
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
        self._out_since = None
        self._out_readings = 0
        # the seek only ever pushed in the approach direction, so that is the
        # branch the iron sits on now
        self._stab_reset(time.monotonic(), self._approach_sign or self._drive_dir or 1)
        self._state = State.STABLE
        self.acq.set_profile("precise")
        self._event("info", f"field stable at {self._setpoint_field:.3f} mT")

    def _tick_hold(self, now: float, field: float) -> None:
        if self._state == State.STABLE and self._setpoint_field is not None:
            self._track_stable_flag(now, field)
        if self.stabilizer_enabled and self._setpoint_field is not None:
            self._tick_stabilizer(now)

    def _track_stable_flag(self, now: float, field: float) -> None:
        """Keep field_stable honest while STABLE (Lukas, 2026-09-28).

        It used to stay True however far the field drifted, so a scan went on
        measuring at a field that was no longer there. Same tolerance and same
        stable_time as on the way in, used both ways: the flag drops once the
        field has been OUT of tolerance for stable_time AND over at least two
        separate readings (a single noisy precise reading lasts ~100 ms and
        must not flicker it), and comes back once it has been within
        tolerance for stable_time again."""
        tol = self.cfg.limits.field_tolerance_mT
        hold_s = self.cfg.limits.stable_time_s
        t_read, _ = self.acq.latest.get()
        if abs(self._setpoint_field - field) > tol:
            self._stable_since = None
            if self._out_since is None:
                self._out_since = now
                self._out_readings = 0
                self._out_last_t = None
            if t_read is not None and t_read != self._out_last_t:
                self._out_last_t = t_read
                self._out_readings += 1
            if (self._field_stable and now - self._out_since >= hold_s
                    and self._out_readings >= 2):
                self._field_stable = False
                self._event("warn", f"field drifted out of tolerance: "
                                    f"{field:.3f} mT for setpoint "
                                    f"{self._setpoint_field:.3f} mT -- not stable")
        else:
            self._out_since = None
            if not self._field_stable:
                if self._stable_since is None:
                    self._stable_since = now
                elif now - self._stable_since >= hold_s:
                    self._field_stable = True
                    self._event("info", f"field back within tolerance at "
                                        f"{self._setpoint_field:.3f} mT")

    # ---- the long-term stabilizer ------------------------------------------

    def _stab_reset(self, now: float, branch_dir: int) -> None:
        """Start a fresh stabilizer on entering STABLE / HOLD: no trim yet,
        the present current is the base, and `branch_dir` is the direction
        the field was approached from (the hysteresis branch it sits on)."""
        self._stab_base = self.ramper.setpoint
        self._stab_trim = 0.0
        self._stab_dir = 1 if branch_dir >= 0 else -1
        self._stab_window_t = now + self.cfg.stabilizer.settle_s
        self._stab_samples = []
        self._stab_last_t = None
        self._stab_pending = None
        self._stab_at_limit = False

    def _local_slope(self) -> Optional[float]:
        """dB/dI (mT/A) of the calibration at the present current, or None."""
        cal = self.calibration
        if cal is None or not getattr(cal, "currents_A", None):
            return None
        I, d = self.ramper.setpoint, 0.05
        slope = (cal.field_for_current(I + d) - cal.field_for_current(I - d)) / (2 * d)
        return slope if abs(slope) > 1e-6 else None

    def _tick_stabilizer(self, now: float) -> None:
        """An integrating, average-then-correct, one-sided trim.

        Why each piece is there (the hard freeze of gotcha #11 solved a limit
        cycle; this must not bring it back):
          * INTEGRATING: each correction is ADDED to `_stab_trim`, so a drift is
            followed until it is gone -- no standing error. (The old trim was
            gain * error on a fixed base: 0.001 A/mT left a 0.5 mT drift
            almost entirely in place.)
          * AVERAGE, THEN CORRECT, THEN WAIT: the error is the mean of every
            reading over `period_s`, taken only after `settle_s` has passed
            since the last move, and one correction follows. Correcting on
            every noisy reading while the field is still responding is exactly
            what made the camera stabiliser limit-cycle.
          * DEADBAND tolerance/2: the seek freezes within tol/2, so a freshly
            reached field never triggers it; below it the current is not
            touched at all (between corrections the output is as still as the
            freeze).
          * ONE-SIDED ENDING: a correction against the approach direction
            steps `backlash_A` further back and then comes forward, so the iron
            always ends on the branch it was approached on and the field
            moves smoothly with the trim instead of jumping by 2h.
          * CAPS: `max_step_A` per correction, `max_trim_A` in total (the
            integrator stops there: anti-windup)."""
        sc = self.cfg.stabilizer
        # finish a back-step: once the ramp has reached it, come forward
        if self._stab_pending is not None:
            if self.ramper.done:
                self.ramper.go_to(self._stab_pending)
                self._stab_pending = None
                self._stab_window_t = now + sc.settle_s
                self._stab_samples = []
            return
        if not self.ramper.done or now < self._stab_window_t:
            return
        t_read, f_read = self.acq.latest.get()
        if (t_read is not None and t_read != self._stab_last_t
                and t_read >= self._stab_window_t):
            self._stab_last_t = t_read
            self._stab_samples.append(f_read)
        if now - self._stab_window_t < sc.period_s or len(self._stab_samples) < 3:
            return
        error = self._setpoint_field - sum(self._stab_samples) / len(self._stab_samples)
        self._stab_samples = []
        self._stab_window_t = now          # next window; no move -> no settle
        if abs(error) <= self.cfg.limits.field_tolerance_mT / 2:
            return

        slope = self._local_slope()
        if slope is not None:
            d_I = sc.fraction * error / slope
        else:
            d_I = sc.fraction * error * sc.gain_A_per_mT
        d_I = max(-sc.max_step_A, min(sc.max_step_A, d_I))
        new_trim = max(-sc.max_trim_A, min(sc.max_trim_A, self._stab_trim + d_I))
        at_limit = abs(new_trim) >= sc.max_trim_A - 1e-12
        if at_limit and not self._stab_at_limit:
            self._event("warn", f"stabilizer at its authority limit "
                                f"({new_trim:+.3f} A): the drift is larger than "
                                f"it may correct")
        self._stab_at_limit = at_limit
        d_I = new_trim - self._stab_trim
        if abs(d_I) < 1e-9:
            return                           # pinned at the limit: nothing to do
        self._stab_trim = new_trim
        target = self._clamp_current(self._stab_base + new_trim)
        if d_I * self._stab_dir < 0 and sc.backlash_A > 0:
            # against the branch: overshoot backwards, the return comes next
            self.ramper.go_to(self._clamp_current(target - self._stab_dir * sc.backlash_A))
            self._stab_pending = target
        else:
            self.ramper.go_to(target)
        self._stab_window_t = now + sc.settle_s

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
        elif self._phase == "zero":
            # the sweep ends at -I_max; IDLE only once the current is back at
            # zero, so a scan whose settle rule is "state IDLE" (describe)
            # cannot start its next step with the magnet at full current.
            if self.ramper.done:
                self._state = State.IDLE
                self._event("info", "calibration: current back at zero")

    def _finish_calibrate(self) -> None:
        hall = self.cfg.hall
        cal = FieldCalibration.from_sweep(self._cal_points, hall=hall, subtract_remanence=True)
        self.calibration = cal
        lo, hi = cal.range_mT
        self._event("info", f"calibration done: {len(cal.currents_A)} pts, "
                            f"{lo:.1f}..{hi:.1f} mT")
        if self._cal_after:
            self._cal_after(cal)
        # Lukas, 2026-09-28: do not leave the magnet at -I_max after a
        # calibration. Ramp to zero (normal ramp rate); the state stays
        # CALIBRATE until the "zero" phase has arrived.
        self.ramper.go_to(0.0)
        self._phase = "zero"

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
