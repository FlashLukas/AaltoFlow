"""The 7230's discrete settings, as tables. Pure data + lookups, no hardware.

Source: Model 7230 DSP Lock-in Amplifier Instruction Manual (198004-A-MNL-D),
section 6.6.01 (SEN, Table 6-2) and 6.6.03 (TC, Table 6-4; SLOPE; FASTMODE in
6.6.04). The instrument takes an INDEX for each (`TC 12` = 100 ms), so the
index is what goes on the wire and the physical value is what people see.

Both tables are 1-2-5 sequences, so they are GENERATED here rather than typed
out: a typo in a 31-entry hand-written list would never announce itself.
"""

from __future__ import annotations

import math


def _one_two_five(start_exp: int, n: int) -> list[float]:
    """n values of the 1-2-5 sequence starting at 1 x 10**start_exp."""
    out = []
    for k in range(n):
        decade, step = divmod(k, 3)
        out.append((1, 2, 5)[step] * 10.0 ** (start_exp + decade))
    return [float(f"{v:.3g}") for v in out]      # 2e-05, not 2.0000000000000002e-05


#: TC n -> time constant in seconds, n = 0 .. 30 (10 us .. 100 ks).
TIME_CONSTANTS_S = _one_two_five(-5, 31)

#: Fast mode OFF restricts the time constant to 5 ms or more (section 6.6.04).
TC_MIN_NORMAL_S = 5e-3

#: SEN n -> full scale, n = 3 .. 27. Voltage mode: 10 nV .. 1 V.
SEN_FIRST_INDEX = 3
_SEN_VOLTS = _one_two_five(-8, 25)
#: High-bandwidth current mode (IMODE 1): the same index is 10 fA .. 1 uA.
_SEN_AMPS_HB = _one_two_five(-14, 25)
#: Low-noise current mode (IMODE 2): index 7 .. 27 = 2 fA .. 10 nA.
_SEN_AMPS_LN = {i: v for i, v in zip(range(7, 28), _one_two_five(-15, 22)[1:])}


def sensitivity_table(input_mode: str) -> dict[int, float]:
    """{SEN index: full scale} for an input mode (V, or A in current mode)."""
    if input_mode == "I low-noise":
        return dict(_SEN_AMPS_LN)
    vals = _SEN_AMPS_HB if input_mode == "I high-BW" else _SEN_VOLTS
    return {SEN_FIRST_INDEX + k: v for k, v in enumerate(vals)}


def is_current_mode(input_mode: str) -> bool:
    return input_mode.startswith("I ")


def unit_for(input_mode: str) -> str:
    """What X, Y and R are measured in: volts, or amps in current mode."""
    return "A" if is_current_mode(input_mode) else "V"


def allowed_time_constants(fast_mode: bool, tc_min_s: float, tc_max_s: float) -> list[float]:
    """The table entries this mode and the Limits envelope allow, shortest first."""
    lo = max(tc_min_s, 0.0 if fast_mode else TC_MIN_NORMAL_S)
    out = [t for t in TIME_CONSTANTS_S if lo * (1 - 1e-9) <= t <= tc_max_s * (1 + 1e-9)]
    # an envelope narrower than one table step must still leave something
    return out or [min(TIME_CONSTANTS_S, key=lambda t: abs(math.log(t / max(lo, 1e-12))))]


def nearest_tc_index(tc_s: float, allowed: list[float]) -> int:
    """TC index of the allowed table value closest to tc_s, on a LOG scale
    (so 7 ms rounds to 5 ms, not 10: the ratio matters, not the difference)."""
    best = min(allowed, key=lambda t: abs(math.log(t / tc_s)))
    return TIME_CONSTANTS_S.index(best)


def allowed_slopes(fast_mode: bool) -> tuple[int, ...]:
    """Fast mode only offers 6 and 12 dB/octave (section 6.6.03, SLOPE)."""
    return (6, 12) if fast_mode else (6, 12, 18, 24)


# ---- human-readable labels (the enum options a panel shows) ----------------

_PREFIX = ((1e0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f"))


def si_label(value: float, unit: str) -> str:
    """2e-07, 'V' -> '200 nV'.  ASCII 'u' for micro: the label also goes to
    print() through pipes (gotcha #14)."""
    if value >= 1e3:
        return f"{value / 1e3:g} k{unit}"
    for scale, p in _PREFIX:
        if value >= scale * (1 - 1e-9):
            return f"{value / scale:.3g} {p}{unit}"
    return f"{value:g} {unit}"


def sensitivity_label(index: int, input_mode: str) -> str:
    table = sensitivity_table(input_mode)
    if index not in table:
        return "--"
    return si_label(table[index], unit_for(input_mode))


def tc_label(tc_s: float) -> str:
    return si_label(tc_s, "s")


def slope_label(db: int) -> str:
    return f"{int(db)} dB/oct"
