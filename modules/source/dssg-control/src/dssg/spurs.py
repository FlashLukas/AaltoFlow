"""Measured harmonic and sub-harmonic lines of the SG12000L, for the GUI.

The spectrum screen in the GUI draws, next to the carrier, the spurious lines
the unit really puts out. The levels are MEASURED, not datasheet values (the
datasheet gives one "typical" harmonic figure for the whole band). The data live
in sg12000l_spurs.json next to this file, written by scripts/extract_spurs.py
from a scan-core harmonics scan (generator -> 30 dB attenuator -> SA124B
analyser, -20 .. +5 dBm x 0.5 .. 12 GHz). To re-measure: run the scan again and
re-run that script; nothing here changes.

What the data say, in physics terms (see docs/sg12000l_spurs.png):
* Below ~1.1 GHz the output comes from a frequency divider (a square wave), so
  the 3rd harmonic is strong (~-11 dBc at 500 MHz) and the dBc levels do not
  depend on the set power: they are made before the output attenuator.
* 3.3 .. 6.1 GHz: a strong 2nd harmonic that grows 2 dB per dB of output, i.e.
  1 dBc per dB -- 2nd-order distortion in the output amplifier. At +5 dBm it
  reaches about -8 dBc at 3.6 .. 4.7 GHz.
* Above ~6 GHz the unit doubles an f/2 oscillator: f/2 leaks out (from ~8 GHz,
  up to -15 dBc near 12 GHz), and 3f/2 is seen at 6.1 .. 6.4 GHz (~-29 dBc).
* Everything else was below the analyser floor (e.g. 2f < ~-40 dBc at 2.2 ..
  3.0 GHz), or not measured at all: carriers below 500 MHz, and any line above
  the analyser's 12.4 GHz (2f of carriers > 6.2 GHz, 3f of carriers > 4.1 GHz).
  None of those are drawn: a missing line means "unknown or below the floor",
  never "clean".

Model used between the measured points: at each measured carrier frequency,
level(P) = dBc_at_ref + slope * (P - ref), with P clamped to the measured power
span (no invented growth beyond +5 dBm); linear interpolation in frequency
between adjacent measured carriers; nothing outside a measured segment.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).with_name("sg12000l_spurs.json")

# line tag -> multiple of the carrier frequency
MULTIPLE = {"2f": 2.0, "3f": 3.0, "f/2": 0.5, "3f/2": 1.5}


@lru_cache(maxsize=1)
def load_table(path: Path = DATA_FILE) -> dict:
    """The measured table. An unreadable file must not take the GUI down: it
    then simply draws no spurs (and says so through `table_ok`)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def table_ok() -> bool:
    return bool(load_table().get("model"))


def _level_dbc(seg: list[dict], f_ghz: float, p_dbm: float, ref: float,
               p_span: tuple[float, float]) -> float | None:
    fs = [r["f_GHz"] for r in seg]
    if not fs[0] <= f_ghz <= fs[-1]:
        return None
    i = min(bisect_right(fs, f_ghz) - 1, len(seg) - 2)
    a, b = seg[i], seg[i + 1]
    t = (f_ghz - a["f_GHz"]) / (b["f_GHz"] - a["f_GHz"])
    p = min(max(p_dbm, p_span[0]), p_span[1])

    def at(r):
        return r["dBc_at_ref"] + r["slope_dBc_per_dB"] * (p - ref)
    return at(a) + t * (at(b) - at(a))


def spur_lines(f_hz: float, p_dbm: float) -> list[tuple[float, float, str]]:
    """Measured spurious lines for a carrier at f_hz with set power p_dbm.

    Returns [(line frequency Hz, level dBm, tag)], only where the line was
    actually seen above the analyser floor. Levels are absolute on the same
    scale as p_dbm (set power + dBc).
    """
    tab = load_table()
    model = tab.get("model", {})
    if not model:
        return []
    ref = float(tab.get("rules", {}).get("ref_power_dBm", 5.0))
    powers = tab.get("source", {}).get("powers_dBm") or [ref]
    p_span = (min(powers), max(powers))
    f_ghz = f_hz / 1e9
    out = []
    for tag, segs in model.items():
        for seg in segs:
            dbc = _level_dbc(seg, f_ghz, p_dbm, ref, p_span)
            if dbc is not None:
                out.append((MULTIPLE[tag] * f_hz, p_dbm + dbc, tag))
                break
    return out


def harmonics_measured(f_hz: float) -> bool:
    """True if the 2nd harmonic of this carrier was inside the measured span
    (carrier inside the sweep AND 2f below the analyser's top frequency). Above
    that the GUI says the harmonics are unknown instead of showing none."""
    src = load_table().get("source", {})
    cars = src.get("carrier_GHz") or [0.0]
    f_ghz = f_hz / 1e9
    sa_top = (src.get("analyser_GHz") or [0.0, 0.0])[1]
    return min(cars) <= f_ghz <= max(cars) and 2.0 * f_ghz <= sa_top
