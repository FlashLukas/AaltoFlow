"""The SR830's discrete settings, as tables. Pure data, no hardware.

Unlike a Zurich lock-in, where the time constant is a number you type, the
SR830 offers FIXED steps: 20 time constants (10 us .. 30 ks) and 27
sensitivities (2 nV .. 1 V), each selected by an index over GPIB (OFLT i,
SENS i). This file is the one place those steps are written down, so the
brain, the simulator, the real driver, `describe` and the GUI all agree.

Source: SR830 manual, chapter 5, "GAIN and TIME CONSTANT COMMANDS" (p. 5-6).

Everything here works in both directions: index <-> label <-> number. A label
is what a person reads ("30 ms", "10 mV"); a number is what arithmetic needs
(0.03 s, 0.01 V). Numbers given by a caller are SNAPPED to a step: a time
constant to the nearest one (on a log scale, because the steps are 1-3-10
decades), so 0.025 s gets 30 ms; a sensitivity to the next one UP, so the
signal the caller expects still fits (3 mV gets the 5 mV range).
"""

from __future__ import annotations

import math

# ---- time constant (OFLT 0..19): 1-3-10 steps from 10 us to 30 ks ------------------

TC_SECONDS: tuple[float, ...] = tuple(
    m * 10.0 ** e for e in range(-5, 5) for m in (1.0, 3.0))       # 10 us .. 30 ks
assert len(TC_SECONDS) == 20


def _fmt_time(s: float) -> str:
    for scale, unit in ((1e3, "ks"), (1.0, "s"), (1e-3, "ms"), (1e-6, "us")):
        if s >= scale * 0.999:
            return f"{s / scale:.0f} {unit}"
    return f"{s * 1e6:.0f} us"


TC_LABELS: tuple[str, ...] = tuple(_fmt_time(s) for s in TC_SECONDS)

#: Above this detection frequency (harmonic x reference) the SR830 refuses time
#: constants longer than 30 s (manual p. 5-6: "Time constants greater than 30s
#: may NOT be set if the detection frequency exceeds 200 Hz").
TC_LONG_MAX_FREQ_HZ = 200.0
TC_LONG_FIRST_INDEX = 14            # 100 s; index 13 = 30 s is still allowed

# ---- sensitivity (SENS 0..26): 1-2-5 steps from 2 nV to 1 V ------------------------

SENS_VOLTS: tuple[float, ...] = tuple(
    m * 10.0 ** e for e in range(-9, 1) for m in (1.0, 2.0, 5.0))[1:28]  # 2 nV .. 1 V
assert len(SENS_VOLTS) == 27 and abs(SENS_VOLTS[0] - 2e-9) < 1e-20

#: In current mode (ISRC 2/3) the same index means a full scale 10^6 times
#: smaller, in amps: index 0 is "2 nV/fA" in the manual, i.e. 2 fA.
#: VERIFY on the unit: that the same factor holds for the 100 Mohm input (I100M)
#: -- compare the front-panel sensitivity display in both current modes.
CURRENT_PER_VOLT = 1e-6


def _fmt_value(v: float, unit: str) -> str:
    prefixes = ((1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p"),
                (1e-15, "f"))
    for scale, p in prefixes:
        if v >= scale * 0.999:
            return f"{v / scale:.0f} {p}{unit}"
    return f"{v / 1e-15:.0f} f{unit}"


SENS_LABELS_V: tuple[str, ...] = tuple(_fmt_value(v, "V") for v in SENS_VOLTS)
SENS_LABELS_A: tuple[str, ...] = tuple(_fmt_value(v * CURRENT_PER_VOLT, "A")
                                       for v in SENS_VOLTS)

# ---- the small enumerations: the index IS the GPIB parameter ------------------------

SLOPES = ("6 dB/oct", "12 dB/oct", "18 dB/oct", "24 dB/oct")    # OFSL 0..3 -> order 1..4
RESERVES = ("high", "normal", "low_noise")                       # RMOD 0..2
INPUT_SOURCES = ("A", "A-B", "I1M", "I100M")                     # ISRC 0..3
GROUNDS = ("float", "ground")                                    # IGND 0..1
COUPLINGS = ("AC", "DC")                                         # ICPL 0..1
LINE_FILTERS = ("off", "line", "2xline", "both")                 # ILIN 0..3
TRIGGERS = ("sine", "ttl_rising", "ttl_falling")                 # RSLP 0..2
REF_SOURCES = ("external", "internal")                           # FMOD 0..1 (0 = external!)

#: Rough dynamic reserve per RMOD setting, in dB. The real reserve depends on
#: the sensitivity too (front-panel table in the manual, chapter 3); this is
#: only used by the SIMULATOR to decide when the input overloads.
RESERVE_DB = {"high": 60.0, "normal": 40.0, "low_noise": 20.0}


def is_current(source: str) -> bool:
    """True for the current inputs (1 Mohm / 100 Mohm transimpedance)."""
    return source in ("I1M", "I100M")


def unit_for(source: str) -> str:
    """Unit of X, Y and R for an input source: volts, or amps in current mode."""
    return "A" if is_current(source) else "V"


def slope_order(slope: str) -> int:
    """'24 dB/oct' -> 4: each 6 dB/oct is one RC stage (filters.py)."""
    return SLOPES.index(slope) + 1


# ---- label / number <-> index --------------------------------------------------------

def _nearest_log(value: float, steps) -> int:
    if not (isinstance(value, (int, float)) and math.isfinite(value)) or value <= 0:
        raise ValueError(f"must be a positive finite number, got {value!r}")
    lv = math.log(value)
    return min(range(len(steps)), key=lambda i: abs(math.log(steps[i]) - lv))


def _parse_quantity(text: str) -> tuple[float, str]:
    """'30 ms' -> (0.03, 's'), '10 nA' -> (1e-8, 'A'), '1 V' -> (1.0, 'V')."""
    t = str(text).strip().replace("µ", "u").replace(" ", "")
    for unit in ("s", "V", "A"):
        if t.endswith(unit):
            num, prefix = t[:-len(unit)], ""
            if num and num[-1].isalpha():
                num, prefix = num[:-1], num[-1]
            scale = {"": 1.0, "k": 1e3, "m": 1e-3, "u": 1e-6, "n": 1e-9,
                     "p": 1e-12, "f": 1e-15}.get(prefix)
            if scale is None:
                break
            return float(num) * scale, unit
    raise ValueError(f"cannot read {text!r} (expected e.g. '30 ms', '10 mV', '5 nA')")


def tc_index(value) -> int:
    """Index 0..19 from an index-free description: a label ('30 ms') or seconds (0.03)."""
    if isinstance(value, str):
        if value in TC_LABELS:
            return TC_LABELS.index(value)
        seconds, unit = _parse_quantity(value)
        if unit != "s":
            raise ValueError(f"a time constant needs a time unit, got {value!r}")
        value = seconds
    return _nearest_log(float(value), TC_SECONDS)


def sens_index(value) -> int:
    """Index 0..26 from a label ('10 mV' or its current twin '10 nA') or a full
    scale in volts. A label in amps is converted back with CURRENT_PER_VOLT."""
    if isinstance(value, str):
        if value in SENS_LABELS_V:
            return SENS_LABELS_V.index(value)
        if value in SENS_LABELS_A:
            return SENS_LABELS_A.index(value)
        v, unit = _parse_quantity(value)
        if unit == "A":
            v /= CURRENT_PER_VOLT
        elif unit != "V":
            raise ValueError(f"a sensitivity needs V or A, got {value!r}")
        value = v
    # Snap UP, not to the nearest: a caller asking for a 3 mV full scale has a
    # signal of up to 3 mV, and the 2 mV range would overload on it. 5 mV is the
    # honest answer. (1e-6 relative slack so '10 mV' typed as 0.01 still hits 10 mV.)
    v = float(value)
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"sensitivity must be a positive finite number, got {value!r}")
    for i, s in enumerate(SENS_VOLTS):
        if s >= v * (1 - 1e-6):
            return i
    return len(SENS_VOLTS) - 1


def sens_full_scale(index: int, source: str) -> float:
    """Full scale of sensitivity `index` in the unit of `source` (V or A)."""
    v = SENS_VOLTS[int(index)]
    return v * CURRENT_PER_VOLT if is_current(source) else v


def sens_label(index: int, source: str) -> str:
    return (SENS_LABELS_A if is_current(source) else SENS_LABELS_V)[int(index)]


def choice(value, options, what: str) -> str:
    """Normalise an enum value: accepts the option itself (any case) or its index."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be one of {options}, got {value!r}")
    if isinstance(value, int) and 0 <= value < len(options):
        return options[value]
    s = str(value).strip()
    for o in options:
        if s.lower() == o.lower():
            return o
    raise ValueError(f"{what} must be one of {options}, got {value!r}")
