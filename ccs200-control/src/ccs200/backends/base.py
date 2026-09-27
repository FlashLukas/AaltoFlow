"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object with these
methods counts as a SpectrometerBackend. There are two: the simulator
(`sim.py`) and the real Thorlabs CCS200 (`tlccs.py`); nothing above this file
knows which one it has, except through the `simulated` flag.

A scan is split in three on purpose, because that is how the CCS is driven:
`start_scan` starts ONE exposure and returns at once, `scan_ready` says whether
the data has arrived, and `read_scan` fetches it. The brain waits in between
WITHOUT holding the hardware lock, so a setter or a new trigger is never stuck
behind a 60-second exposure. `abort_scan` forgets a started scan, and `busy` says when an
abandoned one has really finished (a CCD cannot stop an exposure half-way).

Averaging and dark subtraction are the brain's, not the backend's: it asks for
single scans, so "N averages" and "dark-subtracted" mean the same thing on both.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class SpectrometerBackend(Protocol):
    """A linear-CCD spectrometer."""

    simulated: bool

    def open(self) -> None:
        """Connect. Must not change any setting the brain has not asked for."""

    def close(self) -> None:
        """Disconnect. Safe to call more than once and on a crash."""

    def idn(self) -> str:
        """'Vendor Model S/N fw', or '' if unknown."""

    def wavelengths(self) -> np.ndarray:
        """nm of every pixel (the instrument's calibration). Read at open()."""

    def start_scan(self, integration_s: float) -> None:
        """Apply the integration time (if changed) and start ONE scan."""

    def scan_ready(self) -> bool:
        """True once the started scan's data can be read."""

    def read_scan(self) -> np.ndarray:
        """The started scan, one value per pixel in full-scale units (1.0 = saturated)."""

    def abort_scan(self) -> None:
        """Forget a started scan without reading it. Safe with none pending."""

    def busy(self) -> bool:
        """True while an abandoned scan is still exposing, i.e. the device
        cannot start a clean new one yet. The brain waits for False before
        `start_scan`, polling WITHOUT the hardware lock."""
