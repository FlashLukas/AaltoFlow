"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object with these
methods counts as a GaussmeterBackend, whether it is the real Lake Shore 455
(through pyvisa) or the simulator. The Gaussmeter brain depends ONLY on this,
so swapping hardware for the simulator changes nothing above this line.

Two rules every backend follows:
  * Every field value crossing this interface is in MILLITESLA. The 455 talks
    in whatever unit its front panel shows; converting is the backend's job.
  * Ranges are addressed by their full scale in mT. The 455 numbers them 1..5
    (lowest first) and what each number MEANS depends on the probe type, so the
    backend also reports the list.

Clamping to safe limits is the brain's job, not the backend's.
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

#: flags `read_field` can return next to the value
FLAG_OK = ""
FLAG_OVERLOAD = "overload"      # field above the present range (manual range too low)
FLAG_NO_PROBE = "no probe"      # the meter cannot see a probe

#: measurement modes this module offers (the 455's third, "peak", is not wired up yet)
MODES = ("dc", "rms")
RMS_BANDS = ("wide", "narrow")
DC_DIGITS = (3, 4, 5)

#: The DC filter per resolution, from the manual's specification table (section
#: 1.2, "DC Measurement") and section 4.6.2:
#:     digits          3        4        5
#:     3 dB bandwidth  100 Hz   10 Hz    1 Hz
#:     time constant   0.01 s   0.1 s    1 s
#:     max rdg rate    30/s     30/s     10/s
#: The two filter numbers do not agree for a simple RC filter (1 Hz would be a
#: 0.16 s time constant), so we take the manual's TIME CONSTANT, the larger
#: and therefore safer one, as what a field step has to wait for. VERIFY with a
#: step on the real meter (see CLAUDE.local.md).
DC_BANDWIDTH_HZ = {3: 100.0, 4: 10.0, 5: 1.0}
DC_TIME_CONSTANT_S = {3: 0.01, 4: 0.1, 5: 1.0}
DC_RATE_HZ = {3: 30.0, 4: 30.0, 5: 10.0}
#: The RMS reading's averaging is not specified as a time constant (it updates
#: at 30 rdg/s); treat it like the 4-digit DC filter. VERIFY on the meter.
RMS_TIME_CONSTANT_S = 0.1

#: front-panel field units and their 455 codes (UNIT command, manual p. 6-36)
UNIT_CODES = {"G": 1, "T": 2, "Oe": 3, "A/m": 4}

#: Full-scale ranges per probe family, in mT, lowest first -- RANGE 1 is the
#: first entry. From the manual's DC measurement table (section 1.2):
#:   HST (high stability):          35 G ... 350 kG
#:   HSE (high sensitivity):       3.5 G ...  35 kG
#:   UHS (ultra-high sensitivity):  35 mG ... 35 G   (only four ranges)
PROBE_RANGES_mT = {
    "HST": [3.5, 35.0, 350.0, 3500.0, 35000.0],
    "HSE": [0.35, 3.5, 35.0, 350.0, 3500.0],
    "UHS": [0.0035, 0.035, 0.35, 3.5],
}

#: TYPE? reply -> probe family (manual p. 6-35; 5x = user-programmable cable)
PROBE_TYPE_CODES = {40: "HSE", 41: "HST", 42: "UHS", 50: "HSE", 51: "HST", 52: "UHS"}

_MU0_mT_per_A_per_m = 4e-4 * math.pi      # mu0 = 4 pi 1e-7 T m/A = 4 pi 1e-4 mT per A/m


def to_mT(value: float, unit: str) -> float:
    """A reading in the meter's display unit -> mT.

    Oersted and A/m are H, not B. In the air gap a Hall probe sits in,
    B = mu0 H, so 1 Oe reads as 1 G = 0.1 mT and 1 A/m as 4 pi 1e-4 mT.
    """
    if unit == "G" or unit == "Oe":
        return value * 0.1
    if unit == "T":
        return value * 1e3
    if unit == "A/m":
        return value * _MU0_mT_per_A_per_m
    raise ValueError(f"unknown field unit {unit!r}")


def from_mT(value_mT: float, unit: str) -> float:
    """mT -> the meter's display unit (for RELSP, which takes present units)."""
    return value_mT / to_mT(1.0, unit)


@runtime_checkable
class GaussmeterBackend(Protocol):
    """A single-channel Hall-probe gaussmeter."""

    def open(self) -> None:
        """Connect. Must not change any setting the brain has not asked for."""

    def close(self) -> None:
        """Disconnect. Safe to call more than once and on a crash."""

    def idn(self) -> str:
        """'LSCI,MODEL455,<serial>,<date>' or '' if unknown."""

    def probe_info(self) -> dict:
        """{'family': 'HST'|'HSE'|'UHS'|'', 'type_code': int, 'serial': str,
        'sensitivity_mV_per_kG': float} -- whatever the probe reports."""

    def ranges_mT(self) -> list[float]:
        """Full-scale ranges of the connected probe, mT, lowest first."""

    # ---- measurement mode ------------------------------------------------
    def set_mode(self, mode: str, dc_digits: int, rms_band: str) -> None: ...
    def get_mode(self) -> tuple[str, int, str]: ...

    # ---- range -----------------------------------------------------------
    def set_auto_range(self, on: bool) -> None: ...
    def get_auto_range(self) -> bool: ...
    def set_range(self, full_scale_mT: float) -> None:
        """Manual range: snaps UP to the smallest range >= the request."""
    def get_range(self) -> float:
        """Full scale of the range in use, mT (also while auto-ranging)."""

    # ---- units / relative -------------------------------------------------
    def set_display_unit(self, unit: str) -> None: ...
    def get_display_unit(self) -> str: ...
    def set_relative(self, on: bool, setpoint_mT: float) -> None: ...

    # ---- the measurement -------------------------------------------------
    def read_field(self) -> tuple[float, str]:
        """One reading, (mT, flag). In rms mode the value is the RMS field."""

    # ---- probe zero (needs the zero-gauss chamber) ------------------------
    def start_zero(self) -> None:
        """Zero the probe: whatever field it sees NOW becomes zero. Only with
        the probe in the zero-gauss chamber."""
    def zero_running(self) -> bool: ...
    def clear_zero(self) -> None:
        """Forget the stored zero (ZCLEAR)."""
