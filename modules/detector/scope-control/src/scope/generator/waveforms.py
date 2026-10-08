# COPIED from afg-control (src/afg/waveforms.py) -- the suite copies shared code
# instead of importing across modules. Channels renamed ch1/ch2 -> w1/w2
# (the Analog Discovery generator outputs W1/W2). Keep the two in step by
# hand when the original changes.
"""Waveform arithmetic shared by the brain, the simulator and the GUI preview.

Nothing here talks to hardware. Two jobs:

1. `value(...)` -- the instantaneous output of a channel at time t, for the
   simulator (and later the scope module's simulator, which watches these two
   outputs) and for the waveform preview drawn in the GUI.

2. `peak(...)` -- the highest |voltage| a setting reaches, which is what the
   safety ceiling `peak_max_V` bounds. For every periodic shape it is
   |offset| + amplitude/2; for DC the amplitude does not exist and the level
   is the offset.

Pure Python (math only): the module has no numpy dependency, and these are
evaluated a few hundred times per GUI frame at most.
"""

from __future__ import annotations

import math
import random


def unit_shape(waveform: str, frac: float, duty_pct: float = 50.0,
               symmetry_pct: float = 50.0) -> float:
    """The waveform's shape between -1 and +1 at `frac` of a period (0..1),
    with phase 0 starting where the instrument starts it:

        sine   -- sin(2 pi frac): starts at 0, rising.
        square -- +1 for the first half, -1 for the second.
        pulse  -- +1 for the first duty_pct of the period, -1 after.
        ramp   -- rises from -1 to +1 over symmetry_pct of the period, then
                  falls back (symmetry 50 = triangle, 100 = rising saw).
        noise  -- uniform random; frac ignored.
        dc     -- 0 (the output is the offset).
        arb    -- unknown to us: drawn as 0.
    """
    frac = frac % 1.0
    if waveform == "sine":
        return math.sin(2.0 * math.pi * frac)
    if waveform == "square":
        return 1.0 if frac < 0.5 else -1.0
    if waveform == "pulse":
        return 1.0 if frac < duty_pct / 100.0 else -1.0
    if waveform == "ramp":
        s = min(max(symmetry_pct / 100.0, 0.0), 1.0)
        if s >= 1.0:
            return -1.0 + 2.0 * frac
        if s <= 0.0:
            return 1.0 - 2.0 * frac
        if frac < s:
            return -1.0 + 2.0 * frac / s
        return 1.0 - 2.0 * (frac - s) / (1.0 - s)
    if waveform == "noise":
        return random.uniform(-1.0, 1.0)
    return 0.0


def value(setting: dict, t: float) -> float:
    """The output voltage (into the declared load) of a channel `setting`
    (keys waveform, frequency_Hz, amplitude_Vpp, offset_V, phase_deg, duty_pct,
    symmetry_pct, output) at time t in seconds. An output that is OFF gives 0."""
    if not setting.get("output", True):
        return 0.0
    wf = setting.get("waveform", "sine")
    off = float(setting.get("offset_V", 0.0))
    if wf == "dc":
        return off
    f = float(setting.get("frequency_Hz", 0.0))
    frac = f * t + float(setting.get("phase_deg", 0.0)) / 360.0
    return off + 0.5 * float(setting.get("amplitude_Vpp", 0.0)) * unit_shape(
        wf, frac, float(setting.get("duty_pct", 50.0)),
        float(setting.get("symmetry_pct", 50.0)))


def peak(waveform: str, amplitude_Vpp: float, offset_V: float) -> float:
    """The largest |voltage| this setting reaches (into the declared load)."""
    if waveform == "dc":
        return abs(offset_V)
    return abs(offset_V) + 0.5 * abs(amplitude_Vpp)


def load_factor(load_ohm: float | None) -> float:
    """The factor by which the instrument's 50-ohm voltage range grows when
    its load setting is `load_ohm`. The output is a 50-ohm source: an internal
    EMF E gives E/2 on a 50-ohm load and E*R/(R+50) on a load R. The range is
    quoted for 50 ohm, so on R it is 2R/(R+50) times larger; high-Z (None)
    gives 2 (the "20 Vpp open circuit" of the datasheet)."""
    if load_ohm is None or load_ohm <= 0:
        return 2.0
    return 2.0 * load_ohm / (load_ohm + 50.0)


def wrap_phase(deg: float) -> float:
    """Any angle -> -180 < deg <= 180 (the instrument's phase range)."""
    d = math.fmod(float(deg), 360.0)
    if d > 180.0:
        d -= 360.0
    elif d <= -180.0:
        d += 360.0
    return d
