"""The brain -- a trivial set-and-forget Z piezo (blueprint §5).

Holds the desired voltage, clamps every request to ``cfg.limits``, pushes it to
the backend, and reports a status snapshot.  No control loop.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass


@dataclass
class ZStatus:
    connected: bool = False
    voltage: float = 0.0       # measured/commanded drive voltage, V
    target: float = 0.0        # last commanded (clamped) target, V
    v_min: float = 0.0
    v_max: float = 75.0


class ZPiezo:
    def __init__(self, backend, cfg=None):
        from .config import Config
        self.backend = backend
        self.cfg = cfg or Config()
        self._lock = threading.RLock()
        self._target = 0.0
        self._connected = False
        self._on_event = lambda level, msg: None

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        self.backend.open()
        self._connected = True
        self._emit("info", f"z piezo started ({self.backend.idn()})")

    def shutdown(self) -> None:
        try:
            self.set_voltage(self.cfg.limits.v_min)   # park low = safe
        except Exception:
            pass
        try:
            self.backend.close()
        except Exception:
            pass
        self._connected = False

    # -- control ----------------------------------------------------------- #
    def set_voltage(self, volts: float) -> float:
        v = float(volts)
        lim = self.cfg.limits
        if lim.enforce:
            clamped = min(max(v, lim.v_min), lim.v_max)
            if clamped != v:
                self._emit("warn", f"voltage clamped {v:.3f} -> {clamped:.3f} V")
            v = clamped
        self._target = v
        self.backend.set_voltage(v)
        return v

    def read_voltage(self) -> float:
        return float(self.backend.read_voltage())

    def status(self) -> ZStatus:
        try:
            v = self.backend.read_voltage()
            lo, hi = self.backend.range()
        except Exception:
            v, lo, hi = self._target, self.cfg.limits.v_min, self.cfg.limits.v_max
        return ZStatus(connected=self._connected, voltage=v, target=self._target,
                       v_min=lo, v_max=hi)

    # -- config ------------------------------------------------------------ #
    def get_config(self):
        return self.cfg

    def apply_config(self) -> None:
        # re-clamp the current target into the (possibly changed) envelope
        self.set_voltage(self._target)

    def set_config(self, data: dict) -> None:
        groups = {"limits": self.cfg.limits, "hardware": self.cfg.hardware}
        for gname, values in (data or {}).items():
            obj = groups.get(gname)
            if obj is None or not isinstance(values, dict):
                continue
            for k, val in values.items():
                if hasattr(obj, k):
                    setattr(obj, k, val)
        self.apply_config()

    def _emit(self, level: str, msg: str) -> None:
        try:
            self._on_event(level, msg)
        except Exception:
            pass


def status_to_dict(s: ZStatus) -> dict:
    return asdict(s)
