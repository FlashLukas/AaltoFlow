"""units.py -- the few unit facts scan-core needs, in one place (2026-10-12).

scan-core never hardcodes an instrument, but a few decisions depend on what
KIND of quantity a knob is, and the unit a module declares in `describe` is
the honest way to tell:

* is this a LENGTH (a position a stage moves to)? -- the navigator's stage
  candidates, the scout's "out of focus" warning;
* how does a speed given in "<unit>/s" become the value of a speed control
  declared in another unit (nm/min, K/min, mT/min)? -- the fly scan.

Units are matched as the modules write them (ASCII "um" and the Unicode
micro signs). An unknown unit is never guessed: the functions return None /
False, and the caller says so.
"""

from __future__ import annotations

#: One unit -> its value in the family's base unit, per family. The families
#: are what a speed control is plausibly declared in; a pair from different
#: families never converts.
_FAMILIES = {
    # length in MICROMETRES, so the factors are exact (1 mm = 1000.0 um)
    "length": {"m": 1e6, "cm": 1e4, "mm": 1e3, "um": 1.0, "µm": 1.0,
               "μm": 1.0, "micron": 1.0, "nm": 1e-3, "pm": 1e-6},
    "field": {"T": 1.0, "mT": 1e-3, "uT": 1e-6, "µT": 1e-6, "μT": 1e-6,
              "nT": 1e-9, "G": 1e-4, "mG": 1e-7, "kG": 1e-1},
    "frequency": {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9},
    "voltage": {"V": 1.0, "mV": 1e-3, "uV": 1e-6, "µV": 1e-6, "kV": 1e3},
    "current": {"A": 1.0, "mA": 1e-3, "uA": 1e-6, "µA": 1e-6, "nA": 1e-9},
    "angle": {"deg": 1.0, "°": 1.0, "mdeg": 1e-3, "rad": 57.29577951308232,
              "mrad": 0.05729577951308232},
    "temperature": {"K": 1.0, "mK": 1e-3},
    "power": {"W": 1.0, "mW": 1e-3, "uW": 1e-6, "µW": 1e-6},
}

#: a time unit -> seconds (the denominator of a rate)
_TIME = {"s": 1.0, "sec": 1.0, "ms": 1e-3, "min": 60.0, "h": 3600.0, "hr": 3600.0}


def _clean(unit) -> str:
    return (unit or "").strip() if isinstance(unit, str) else ""


def family_of(unit) -> tuple[str, float] | None:
    """(family, factor to the family's base unit) of `unit`, or None."""
    u = _clean(unit)
    for fam, table in _FAMILIES.items():
        if u in table:
            return fam, table[u]
    return None


def is_length_unit(unit) -> bool:
    """True for a length unit (m, mm, um, nm, ...): a position."""
    f = family_of(unit)
    return f is not None and f[0] == "length"


def length_factor_um(unit) -> float | None:
    """Micrometres per one `unit` (1000 for mm), or None if not a length."""
    f = family_of(unit)
    if f is None or f[0] != "length":
        return None
    return f[1]


def split_rate(unit) -> tuple[str, float] | None:
    """A rate unit "<value unit>/<time unit>" -> (value unit, seconds per
    time unit). "mT/min" -> ("mT", 60.0). None if it is not a rate."""
    u = _clean(unit)
    if "/" not in u:
        return None
    num, den = u.rsplit("/", 1)
    t = _TIME.get(den.strip())
    if t is None:
        return None
    return num.strip(), t


def rate_factor(value_unit, rate_unit) -> float | None:
    """The multiplier from a pace in `value_unit` PER SECOND to a value in
    `rate_unit`, or None when they do not convert.

        rate_factor("um", "um/s")    == 1
        rate_factor("um", "nm/min")  ~= 60000      (1 um/s = 60000 nm/min)
        rate_factor("mT", "mT/min")  == 60
        rate_factor("K",  "K/min")   == 60
        rate_factor("mT", "um/s")    is None       (not a rate of a field)

    A unit outside the known families converts only to itself
    ("steps" -> "steps/min" == 60)."""
    split = split_rate(rate_unit)
    if split is None:
        return None
    num, seconds = split
    vu = _clean(value_unit)
    if num == vu:
        scale = 1.0
    else:
        a, b = family_of(vu), family_of(num)
        if a is None or b is None or a[0] != b[0]:
            return None
        scale = a[1] / b[1]          # one value_unit, in rate_unit's numerator unit
    return scale * seconds


__all__ = ["family_of", "is_length_unit", "length_factor_um", "split_rate", "rate_factor"]
