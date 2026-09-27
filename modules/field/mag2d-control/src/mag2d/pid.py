"""One axis of the field loop: feed-forward + PI, with output clamp, slew limit
and anti-windup.

    u = B_set / ff  +  Kp * e  +  Ki * integral(e dt)          e = B_set - B_meas  [mT]

then clamped to +-limit volts and slew-limited to +-slew*dt from the previous
output.

Why each part:

  * FEED-FORWARD does most of the work. The magnet is (nearly) linear, so
    B_set / (mT per volt) lands close to the target on its own; the PI only has
    to remove the few percent that is left. Without it the integral would have
    to build up the WHOLE output, which is slow and overshoots.

  * The SLEW LIMIT protects the coils and the amplifier from voltage steps (the
    old VI ramped 0.1 V per 50 ms). It also means a large setpoint change takes
    seconds, during which the error is large -- which is exactly when a naive
    integral winds up.

  * ANTI-WINDUP, by conditional integration: the error is integrated only on a
    tick where the output was NOT limited (by the clamp or the slew). While the
    output is pinned, integrating would store up a correction the loop cannot
    deliver, and it would come out later as overshoot. With this rule the
    integral holds still during a slew and resumes once the output is free.

Unlike clMag's seek, this PI never freezes: Lukas asked for a loop that runs all
the time. The tolerance band is wide compared with the noise (0.5 mT vs
0.05 mT), so a small dither from hysteresis stays well inside it.
"""

from __future__ import annotations


class AxisPI:
    def __init__(self):
        self.integral = 0.0          # mT * s
        self.output = 0.0            # V, the last value returned

    def reset(self, output_V: float = 0.0) -> None:
        self.integral = 0.0
        self.output = float(output_V)

    def bumpless(self, setpoint_mT: float, measured_mT: float, output_V: float,
                 kp: float, ki: float, ff_mT_per_V: float) -> None:
        """Start regulating FROM the present output without a kick.

        Chooses the integral so that the control law would produce exactly the
        output already on the wire. Used when the output is switched on again
        during a ramp down: the loop picks up where the ramp was, instead of
        jumping to wherever a zero integral would put it.
        """
        self.output = float(output_V)
        if ki > 0:
            ff = setpoint_mT / ff_mT_per_V if ff_mT_per_V else 0.0
            e = setpoint_mT - measured_mT
            self.integral = (output_V - ff - kp * e) / ki
        else:
            self.integral = 0.0

    def update(self, setpoint_mT: float, measured_mT: float, dt: float, *,
               kp: float, ki: float, ff_mT_per_V: float,
               limit_V: float, slew_V_per_s: float) -> float:
        e = setpoint_mT - measured_mT
        ff = setpoint_mT / ff_mT_per_V if ff_mT_per_V else 0.0
        candidate = self.integral + e * dt
        wanted = ff + kp * e + ki * candidate

        out = max(-limit_V, min(limit_V, wanted))
        step = max(0.0, slew_V_per_s) * dt
        out = max(self.output - step, min(self.output + step, out))

        if abs(out - wanted) <= 1e-12:
            self.integral = candidate           # free: integrate
        # else: limited -> keep the old integral (anti-windup)
        self.output = out
        return out


def ramp_toward_zero(output_V: float, dt: float, slew_V_per_s: float) -> float:
    """One slew-limited step of a ramp down to 0 V."""
    step = max(0.0, slew_V_per_s) * dt
    if abs(output_V) <= step:
        return 0.0
    return output_V - step if output_V > 0 else output_V + step
