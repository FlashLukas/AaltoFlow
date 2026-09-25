"""The acquisition thread: measure the field continuously, never block control.

This is the single most important architectural idea in the whole port. A
precise field reading takes ~100 ms (1000 samples @ 10 kHz). If the control loop
called for a reading and waited, it could only act ~10 times a second, and while
seeking that is far too slow.

So acquisition runs in its OWN thread, in a tight loop, always taking the newest
reading and dropping it into a shared slot. The control loop just glances at that
slot -- it reads the *latest* value instantly and never waits for the DAQ.

Two shared things:
  * `Latest`  -- the most recent (timestamp, field) pair, lock-protected.
  * a ring buffer of recent samples, for the live plot later.

Profiles: the control loop flips the profile to "fast" while seeking (noisier
but ~50 Hz) and back to "precise" when settling/holding (clean, ~10 Hz).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional, Tuple

from .backends.base import FieldSensor
from .config import Acquisition, HallProbe


class Latest:
    """A single (timestamp, field_mT) slot that many threads can read/write."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._t: Optional[float] = None
        self._field_mT: float = 0.0
        self._samples: int = 0

    def set(self, t: float, field_mT: float, samples: int) -> None:
        with self._lock:
            self._t, self._field_mT, self._samples = t, field_mT, samples

    def get(self) -> Tuple[Optional[float], float]:
        with self._lock:
            return self._t, self._field_mT


class AcquisitionThread(threading.Thread):
    def __init__(
        self,
        probe: FieldSensor,
        hall: HallProbe,
        profiles: Acquisition,
        ring_size: int = 2000,
    ) -> None:
        super().__init__(daemon=True, name="acquisition")
        self._probe = probe
        self._hall = hall
        self._profiles = profiles
        self._profile = "precise"
        self._profile_lock = threading.Lock()
        self._stop = threading.Event()
        self.latest = Latest()
        self.ring = deque(maxlen=ring_size)   # (t, field) for plotting/logging

    def set_profile(self, name: str) -> None:
        assert name in ("fast", "precise")
        with self._profile_lock:
            self._profile = name

    def _current_profile(self) -> Tuple[int, float]:
        with self._profile_lock:
            name = self._profile
        if name == "fast":
            return self._profiles.fast_samples, self._profiles.fast_rate_Hz
        return self._profiles.precise_samples, self._profiles.precise_rate_Hz

    def run(self) -> None:
        while not self._stop.is_set():
            samples, rate = self._current_profile()
            volts = self._probe.read_voltage(samples, rate)   # blocks ~samples/rate
            field = self._hall.volts_to_field(volts)
            t = time.monotonic()
            self.latest.set(t, field, samples)
            self.ring.append((t, field))

    def stop(self) -> None:
        self._stop.set()
