"""Phase arithmetic, in one small place because it is easy to get subtly wrong.

Three operations, and why each exists:

* quantize(): the unit only holds multiples of its step (0.5 deg on a PS6000L).
  We round BEFORE sending, so the number we report is the number the unit
  holds -- not a value it silently rounded behind our back.
* wrap(): the unit's range is -180..+180 deg. Phase is periodic, so 270 deg
  and -90 deg are the same setting; we send the wrapped one.
* unwrap_near(): the unit reads back -90 after we asked for 270. To report it
  in the caller's branch (so a scan 0..360 sees its own number echoed back),
  add whole turns until the readback is as close as possible to what was
  asked. The unit's truth is kept -- only the 360-degree bookkeeping changes.
"""

from __future__ import annotations

import math


def quantize(value: float, step: float) -> float:
    """Round to the nearest multiple of `step` (step <= 0 means no rounding).

    Python's round() rounds halves to even, which would make +0.25 and -0.25
    behave differently from +0.75; floor(x + 0.5) rounds halves consistently up.
    """
    if step <= 0:
        return float(value)
    q = math.floor(value / step + 0.5) * step
    # tidy away float fuzz like 44.50000000001 so echoes compare cleanly
    return round(q, 9)


def wrap(deg: float) -> float:
    """Wrap into the device range (-180, +180]. +180 stays +180 (the datasheet
    range includes it); -180 becomes +180, the same physical setting."""
    w = math.fmod(deg, 360.0)
    if w <= -180.0:
        w += 360.0
    elif w > 180.0:
        w -= 360.0
    return round(w, 9)


def unwrap_near(readback: float, reference: float) -> float:
    """`readback` plus the whole number of turns that brings it closest to
    `reference`."""
    turns = round((reference - readback) / 360.0)
    return round(readback + 360.0 * turns, 9)


def decimals_for(step: float) -> int:
    """Enough decimals to show a step exactly: 0.5 -> 1, 0.25 -> 2, 5.625 -> 3.
    Used for the spin boxes and the describe `decimals` hint alike."""
    for n in range(0, 7):
        if abs(round(step, n) - step) < 1e-9:
            return n
    return 6


def datasheet_accuracy_deg(freq_MHz: float, phase_deg: float) -> float:
    """PS6000L datasheet V3.1 phase accuracy for this carrier and setting.

    +-2 deg (400-4500 MHz, within +-90 deg), +-3 deg (400-4500, up to +-180),
    +-4 / +-8 deg above 4500 MHz. Shown in the GUI and published so a data file
    records how far to trust the phase axis; the SIMULATOR is ideal and does
    not add this error.
    """
    big = abs(wrap(phase_deg)) > 90.0
    if freq_MHz <= 4500.0:
        return 3.0 if big else 2.0
    return 8.0 if big else 4.0
