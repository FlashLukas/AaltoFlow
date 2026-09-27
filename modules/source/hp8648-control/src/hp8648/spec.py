"""What the HP 8648D can actually do, as pure functions (no Qt, no hardware).

Source: HP 8648A/B/C/D Operation and Service Guide (08648-90048), chapter 1b
"Operation Reference" (AMPLITUDE table) and chapter 4 "Specifications".

Why a separate file: the maximum output level is not one number. It steps down
with frequency, and option 1EA ("high power") raises it. Both the brain (which
clamps) and `describe` (which publishes the live limit a scan may use) need the
same answer, so it is computed in exactly one place.

The standard 8648B/C/D table is clear in the manual (chapter 4, "Output
Range"):
    <= 2500 MHz : +13 dBm
    <= 4000 MHz : +10 dBm
The option 1EA column ("Maximum Leveled") is badly scanned. Read row by row it
gives, for 1EA alone:  <100 kHz +17 (typical), <=1000 MHz +20, <=1500 MHz +19,
<=2100 MHz +17, <=2500 MHz +15, <=4000 MHz +13 dBm. The neighbouring
"1EA and 1E6" column is exactly 2 dB lower above 100 MHz (+18/+17/+15/+13/+11),
as its footnote says it should be, which is what makes this reading credible.
(The first build of this module had the column shifted by one row.)
# VERIFY the 1EA column against a clean copy of the specifications page.
"""

from __future__ import annotations

FREQ_MIN_HZ = 9e3          # 8648D range, chapter 4 "Frequency Range"
FREQ_MAX_HZ = 4000e6
POWER_MIN_DBM = -136.0     # bottom of the output range, all models

#: (upper band edge in Hz, max level in dBm), standard instrument.
_STANDARD = [
    (2500e6, 13.0),
    (FREQ_MAX_HZ, 10.0),
]

#: The same for option 1EA (high power). # VERIFY (see module docstring)
_OPTION_1EA = [
    (100e3, 17.0),          # typical only below 100 kHz
    (1000e6, 20.0),
    (1500e6, 19.0),
    (2100e6, 17.0),
    (2500e6, 15.0),
    (FREQ_MAX_HZ, 13.0),
]

#: Resolution the instrument keeps. The SCPI table says "up to 9 digits with a
#: maximum of 10 Hz resolution" (4000.00000 MHz is 9 digits), and "maximum
#: resolution of .1 dB" for the level. The simulator rounds to these, and the
#: describe settle tolerances are derived from them so a scan's echo check
#: accepts the instrument's rounding.
FREQ_RESOLUTION_HZ = 10.0     # VERIFY: the spec page quotes 0.001 Hz
POWER_RESOLUTION_DB = 0.1

#: Frequency switching time (chapter 4, "Switching Speed", typical).
def switching_time_s(freq_Hz: float) -> float:
    return 0.075 if freq_Hz < 1001e6 else 0.100


def band_edges(option_1ea: bool = False) -> list[float]:
    """The frequencies (Hz) where the specified maximum level steps, from the
    bottom of the range to the top -- for drawing the ceiling in the GUI from
    the SAME table the brain clamps with."""
    table = _OPTION_1EA if option_1ea else _STANDARD
    return [FREQ_MIN_HZ] + [edge for edge, _ in table]


def spec_max_dBm(freq_Hz: float, option_1ea: bool = False) -> float:
    """The specified maximum output level at `freq_Hz`."""
    table = _OPTION_1EA if option_1ea else _STANDARD
    for edge, level in table:
        if freq_Hz <= edge:
            return level
    return table[-1][1]


def quantize_frequency(freq_Hz: float) -> float:
    return round(freq_Hz / FREQ_RESOLUTION_HZ) * FREQ_RESOLUTION_HZ


def quantize_power(dBm: float) -> float:
    # round() on a float like -12.35 can land either way; that is fine, the
    # instrument's own rounding is what the echo tolerance is sized for.
    return round(round(dBm / POWER_RESOLUTION_DB) * POWER_RESOLUTION_DB, 3)
