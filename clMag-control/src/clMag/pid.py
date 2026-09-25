"""Parallel-form PI controller (the seek engine).

Output for a field error `e` (in mT) is a current correction (in amps):

    u = Kc * e  +  (Kc / Ti) * integral(e dt)

There is no derivative term (Td = 0), so this is a PI, not a full PID -- that
was your choice, and it is the right one for a slow, over-damped magnet.

Two features beyond the textbook formula matter here:

  * One-direction clamp. The seek deliberately undershoots the target and then
    approaches from a single side and NEVER overshoots. We pass a `clamp_sign`
    (+1 approaching from below, -1 from above); the correction is not allowed to
    push the other way.

  * Anti-windup. When the output is clamped we must NOT keep accumulating the
    integral, or it "winds up" and causes a lag/overshoot later. We simply undo
    the integration we just did whenever we clamp.
"""

from __future__ import annotations


class PI:
    def __init__(self, Kc_A_per_mT: float, Ti_s: float):
        self.Kc = Kc_A_per_mT
        self.Ti = Ti_s
        self._integral = 0.0

    def reset(self) -> None:
        """Call at the start of every new seek so old error history is dropped."""
        self._integral = 0.0

    def update(self, error_mT: float, dt_s: float, clamp_sign: int = 0) -> float:
        """Return the current correction in amps.

        clamp_sign: 0 = no clamp, +1 = output must be >= 0, -1 = output must be <= 0.
        """
        # integrate first
        self._integral += error_mT * dt_s
        integral_term = (self.Kc / self.Ti) * self._integral if self.Ti > 0 else 0.0
        u = self.Kc * error_mT + integral_term

        # one-direction clamp with anti-windup
        if clamp_sign > 0 and u < 0.0:
            self._integral -= error_mT * dt_s      # undo this step's windup
            return 0.0
        if clamp_sign < 0 and u > 0.0:
            self._integral -= error_mT * dt_s
            return 0.0
        return u
