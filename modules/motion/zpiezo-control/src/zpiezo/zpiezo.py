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
        """Open the KCube and ADOPT the voltage it is already holding.

        Lukas's rule (2026-09-27): a module reads the instrument's state at
        start and changes nothing.  For a focus piezo this matters directly --
        whatever voltage the KCube holds IS the current focus, and writing
        anything here (e.g. the old default target 0 V) would throw the sample
        out of focus the moment the service starts.  So: query only.  The read
        voltage becomes the target, which is also what a later ``set_config``
        re-clamp starts from.  A voltage outside the configured envelope is
        adopted AS IS (with a warning) -- it is not moved into range; the next
        explicit ``set_voltage`` is clamped as usual.
        """
        self.backend.open()
        self._connected = True
        self._emit("info", f"z piezo started ({self.backend.idn()})")
        try:
            v = float(self.backend.read_voltage())
        except Exception as exc:
            # Cannot read it -> we do not know the focus.  Still write nothing.
            self._emit("warn", f"could not read the drive voltage at start "
                               f"({exc}); target unknown, nothing was written")
            return
        with self._lock:
            self._target = v
        self._emit("info", f"adopted drive voltage {v:.3f} V from the instrument")
        lim = self.cfg.limits
        if lim.enforce and not (lim.v_min <= v <= lim.v_max):
            self._emit("warn", f"instrument holds {v:.3f} V, outside the limits "
                               f"{lim.v_min:g}..{lim.v_max:g} V; left as it is")

    def shutdown(self) -> None:
        # Park ONLY a KCube we actually opened.  If open() failed -- e.g. the
        # serial is claimed by another service (hwlock.HardwareBusy) -- that
        # other service owns the focus, and "parking" it at v_min from here
        # would defocus somebody else's measurement.
        if self._connected:
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
        """Re-clamp the target into a (possibly changed) envelope.

        Writes to the KCube ONLY if the new limits actually move the target;
        a ``set_config`` that changes, say, the jog step must not re-send the
        voltage (it used to, every time -- harmless once the target was
        adopted, but a write the user did not ask for).
        """
        lim = self.cfg.limits
        if lim.enforce and not (lim.v_min <= self._target <= lim.v_max):
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
