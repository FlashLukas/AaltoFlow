"""One axis of the field seek: a calibrated jump, a one-way PI trim, a freeze.

This replaces the always-on PI of mag2d-control. The difference is the whole
point of this module, so it is worth stating plainly.

THE PROBLEM. An electromagnet's iron has hysteresis: the field depends not only
on the drive voltage but on which way the voltage was last moving. Model it as
"play", like backlash in a gearbox -- the field only follows once the drive has
pushed past a dead band of half-width h. Now give that plant a PI that never
stops correcting:

    error still positive  ->  integral grows  ->  drive rises, field does not
    (we are crossing the dead band)
    drive breaks through  ->  field moves fast, sails past the setpoint
    error now negative    ->  integral unwinds, drive falls, field does not
    ... and round again.

That is a limit cycle of roughly 2h peak to peak, and it never settles. clMag hit
exactly this on the 1-axis magnet (docs/DEVELOPER_NOTES.md gotcha #11), and
it is why the old LabVIEW specification said "PID stops".

THE ANSWER, in three parts:

  * JUMP. The measured calibration says which voltage gives the wanted field on
    the leg we are approaching from, so one slew-limited move gets within a
    couple of millitesla without any feedback at all.

  * SETTLE. The coils lag the drive, so when the jump's ramp stops the field is
    still moving. A short wait at the jump voltage lets it arrive before any
    feedback looks at it -- otherwise the trim would chase a field that is
    already coming toward it and sail straight past the target.

  * ONE-WAY TRIM. The jump deliberately stops SHORT (`field_step_mT`), and the
    PI correction is clamped to the direction of approach. The field therefore
    walks onto the target from one side only: it stays on one hysteresis branch
    and cannot overshoot into the other.

  * FREEZE. Inside tolerance/2 the output is held EXACTLY still. No nudges, no
    direction flips, so the play operator never re-engages and the field simply
    stays where it was left. The loop resumes only if the field genuinely leaves
    the full tolerance band.

Everything below is per axis; the controller owns two of these and decides the
setpoints. It is clock-free: the caller passes dt and calls update() once a tick.
"""

from __future__ import annotations

IDLE, JUMP, SETTLE, TRIM, FROZEN = "idle", "jump", "settle", "trim", "frozen"


def step_toward(value: float, target: float, max_step: float) -> float:
    """Move `value` at most `max_step` toward `target` (the slew limit)."""
    delta = target - value
    if abs(delta) <= max_step:
        return target
    return value + (max_step if delta > 0 else -max_step)


def ramp_toward_zero(output_V: float, dt: float, slew_V_per_s: float) -> float:
    """One slew-limited step of a ramp down to 0 V."""
    return step_toward(output_V, 0.0, max(0.0, slew_V_per_s) * dt)


class AxisSeek:
    """The state of one axis's approach to its setpoint.

    `output` is the voltage on the wire; the controller writes it to the DAQ
    after every tick. `phase` is one of idle / jump / trim / frozen.
    """

    def __init__(self):
        self.output = 0.0          # V, the last value returned
        self.integral = 0.0        # mT * s
        self.phase = IDLE
        self.approach = 1          # +1 = the field must rise, -1 = fall
        self.jump_V = 0.0          # calibrated volts for the undershot target
        self.target_V = 0.0        # calibrated volts for the target itself
        self.settle_left = 0.0     # seconds of post-jump wait still to serve
        # The settle now ends when the FIELD stops moving, with settle_left only
        # as the cap (see update()). These carry the little rate estimator.
        self._settle_prev = None   # field at the last window boundary, mT
        self._settle_dt = 0.0      # seconds accumulated in the current window

    # ---- lifecycle ---------------------------------------------------------

    def begin(self, jump_V: float, target_V: float, approach: int,
              settle_s: float = 0.0, output_V: float | None = None) -> None:
        """Start a new approach. Drops the old integral: it belongs to the
        previous setpoint, and carrying it over would bias the first trim."""
        if output_V is not None:
            self.output = float(output_V)
        self.jump_V = float(jump_V)
        self.target_V = float(target_V)
        self.approach = 1 if approach >= 0 else -1
        self.integral = 0.0
        self.settle_left = max(0.0, float(settle_s))
        self._settle_prev = None
        self._settle_dt = 0.0
        self.phase = JUMP

    def reset(self, output_V: float = 0.0) -> None:
        self.output = float(output_V)
        self.integral = 0.0
        self.settle_left = 0.0
        self._settle_prev = None
        self._settle_dt = 0.0
        self.phase = IDLE

    @property
    def frozen(self) -> bool:
        return self.phase == FROZEN

    def nudge(self, dV: float, limit_V: float) -> float:
        """Move the output by hand, ignoring the freeze.

        Used ONLY by the long-term stabilizer, which is allowed to correct slow
        drift while the loop is frozen. It keeps the phase as it is, so a
        stabilized axis stays frozen and the PI stays quiet.
        """
        self.output = max(-limit_V, min(limit_V, self.output + dV))
        return self.output

    # ---- one tick ----------------------------------------------------------

    def update(self, error_mT: float, dt: float, *, kp: float, ki: float,
               limit_V: float, slew_V_per_s: float, tolerance_mT: float,
               trim_slew_V_per_s: float | None = None,
               margin_V: float = 0.1, margin_frac: float = 0.05,
               freeze: bool = True, one_way: bool = True,
               measured_mT: float | None = None,
               settle_rate_mT_per_s: float = 0.0,
               settle_window_s: float = 0.1) -> float:
        """Advance one control tick and return the new output voltage.

        `error_mT` is setpoint - measured on THIS axis. `freeze=False` turns the
        whole mechanism off and leaves an ordinary bidirectional PI around the
        calibrated jump -- that is mag2d-control's behaviour, kept so the two can
        be compared on the same plant (tests/test_freeze.py).

        `measured_mT` + `settle_rate_mT_per_s` make the post-jump wait ADAPTIVE:
        the settle ends when the field itself has stopped moving, and
        `settle_left` is only a cap. A fixed wait cannot be right for both ends
        of the range -- it has to cover the coil's arrival after a 100 mT jump,
        and that same wait is then spent doing nothing after a 1 mT one. Worse,
        a fixed wait that is too SHORT for a fast jump hands the trim a field
        that is still rising, and the trim pushes it straight past the target
        (measured: 3.6 mT of overshoot at a 10 V/s jump with a 0.15 s wait).
        """
        tol = abs(tolerance_mT)

        if self.phase == FROZEN:
            if abs(error_mT) <= tol:
                return self.output          # THE FREEZE: do not touch the wire
            # Genuinely outside the band (drift, cross-talk from the other axis,
            # someone moved the sample): resume the trim, keeping the integral
            # so we do not start from scratch.
            self.phase = TRIM

        step = max(0.0, slew_V_per_s) * max(0.0, dt)

        if self.phase == JUMP:
            self.output = step_toward(self.output, self.jump_V, step)
            if self.output == self.jump_V:
                self.phase = SETTLE if self.settle_left > 0.0 else TRIM
            return self.output              # no feedback while jumping
        if self.phase == SETTLE:
            # Hold still and let the coil arrive. No freeze either: the field is
            # still moving, so "inside tolerance" right now means nothing.
            self.settle_left -= max(0.0, dt)
            if self._field_arrived(measured_mT, dt, settle_rate_mT_per_s,
                                   settle_window_s) or self.settle_left <= 0.0:
                self.phase = TRIM
            return self.output

        if self.phase == IDLE:
            self.phase = TRIM
        # The trim gets its OWN, much slower rate limit. Why it must be slower
        # than the jump's: the coils lag the drive, so when the freeze latches
        # (error inside tolerance/2) the field is still coasting by about
        # rate * tau. Move the trim at 40 mT/s and that coast is metres past the
        # target; move it at 1.6 mT/s and the coast is a tenth of a millitesla.
        # It costs a second per point and it is why the trim lands instead of
        # skidding.
        if trim_slew_V_per_s is not None:
            step = max(0.0, trim_slew_V_per_s) * max(0.0, dt)
        self.output = self._trim(error_mT, dt, kp, ki, limit_V, step,
                                 margin_V, margin_frac, one_way)

        # Close enough: stop, and stay stopped. Checked AFTER the move, so the
        # voltage we hold is the one that produced this reading.
        if freeze and abs(error_mT) <= tol / 2.0:
            self.phase = FROZEN
        return self.output

    def _field_arrived(self, measured_mT, dt: float, rate_mT_per_s: float,
                       window_s: float) -> bool:
        """Has the field stopped moving since the jump ended?

        Measured over a WINDOW rather than tick to tick: at 50 Hz a 0.05 mT rms
        probe reading differentiates to ~2.5 mT/s of pure noise, which would
        never fall below any sensible threshold. Over 0.1 s the same noise is
        ~0.7 mT/s, so a threshold of a couple of mT/s means the coil, not the
        probe. Returns False when the caller passes no field (then the timed
        wait applies, exactly as before).
        """
        if measured_mT is None or rate_mT_per_s <= 0.0:
            return False
        if self._settle_prev is None:            # first tick of this settle
            self._settle_prev = float(measured_mT)
            self._settle_dt = 0.0
            return False
        self._settle_dt += max(0.0, dt)
        if self._settle_dt < max(1e-9, window_s):
            return False
        rate = abs(float(measured_mT) - self._settle_prev) / self._settle_dt
        self._settle_prev = float(measured_mT)
        self._settle_dt = 0.0
        return rate <= abs(rate_mT_per_s)

    def _trim(self, error_mT: float, dt: float, kp: float, ki: float,
              limit_V: float, step: float, margin_V: float, margin_frac: float,
              one_way: bool) -> float:
        candidate = self.integral + error_mT * dt
        correction = kp * error_mT + ki * candidate

        # One-way clamp: never push back the way we came.
        clamped = False
        if one_way:
            if self.approach > 0 and correction < 0.0:
                correction, clamped = 0.0, True
            elif self.approach < 0 and correction > 0.0:
                correction, clamped = 0.0, True

        wanted = self.jump_V + correction
        # Never command past what the calibration says the target needs, plus a
        # margin for the calibration's own error: a fixed floor plus a fraction
        # of the voltage itself, because the error of a curve (or of the
        # straight-line fallback) grows with the field, not with nothing. This
        # bounds the overshoot whatever the gains are -- the jump did the work,
        # the trim only tidies.
        if one_way:
            allowance = abs(margin_V) + abs(margin_frac) * abs(self.target_V)
            cap = self.target_V + self.approach * allowance
            wanted = min(wanted, cap) if self.approach > 0 else max(wanted, cap)

        wanted = max(-limit_V, min(limit_V, wanted))
        out = max(self.output - step, min(self.output + step, wanted))

        # Anti-windup by conditional integration: only store the new integral if
        # nothing limited the output this tick. While the output is pinned (by
        # the slew, the rail or the cap) integrating would bank a correction the
        # loop cannot deliver, and it would come back out later as overshoot.
        if not clamped and abs(out - wanted) <= 1e-12:
            self.integral = candidate
        return out
