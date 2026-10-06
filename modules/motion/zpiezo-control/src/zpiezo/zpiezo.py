"""The brain -- a trivial set-and-forget Z piezo (blueprint §5).

Holds the desired voltage, clamps every request to ``cfg.limits``, pushes it to
the backend, and reports a status snapshot.  No control loop.
"""

from __future__ import annotations

import math
import threading
from dataclasses import asdict, dataclass


@dataclass
class ZStatus:
    connected: bool = False
    # The drive voltage READ BACK from the KCube, V.  This is what a scan's
    # `echoes(voltage)` settle waits for, so it is never the commanded value:
    # while reads fail it stays at the last good read-back (NaN if there never
    # was one) and hw_error says why.
    voltage: float = 0.0
    target: float = 0.0        # last commanded (clamped) target, V
    v_min: float = 0.0
    v_max: float = 75.0
    # "" while the KCube answers; the error text while its reads fail.
    # scan-core pauses a scan on a non-empty hw_error.
    hw_error: str = ""


class ZPiezo:
    def __init__(self, backend, cfg=None):
        from .config import Config
        self.backend = backend
        self.cfg = cfg or Config()
        self._lock = threading.RLock()
        self._target = 0.0
        self._connected = False
        # Last good read-back and the running error episode ("" = none);
        # both guarded by self._lock.
        self._last_v = float("nan")
        self._hw_error = ""
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
            self._last_v = v
        self._emit("info", f"adopted drive voltage {v:.3f} V from the instrument")
        lim = self.cfg.limits
        if lim.enforce and not (lim.v_min <= v <= lim.v_max):
            self._emit("warn", f"instrument holds {v:.3f} V, outside the limits "
                               f"{lim.v_min:g}..{lim.v_max:g} V; left as it is")

    def shutdown(self, keep_outputs: bool = False) -> None:
        # Park ONLY a KCube we actually opened.  If open() failed -- e.g. the
        # serial is claimed by another service (hwlock.HardwareBusy) -- that
        # other service owns the focus, and "parking" it at v_min from here
        # would defocus somebody else's measurement.
        # keep_outputs (shutdown{keep_outputs: true}) is a RESTART for a code
        # update: the focus stays where it is (no park), the next start adopts
        # the voltage. There is no motion to stop -- a set is one write.
        if self._connected and not keep_outputs:
            try:
                self.set_voltage(self.cfg.limits.v_min)   # park low = safe
            except Exception:
                pass
        with self._lock:                  # never close under a running read
            try:
                self.backend.close()
            except Exception:
                pass
            self._connected = False

    # -- control ----------------------------------------------------------- #
    def set_voltage(self, volts: float) -> float:
        v = float(volts)
        # NaN is not a voltage, and it slips through the clamp below:
        # min(max(nan, lo), hi) is nan.  (inf is fine -- it clamps.)
        if math.isnan(v):
            raise ValueError("voltage is not a number (nan)")
        lim = self.cfg.limits
        if lim.enforce:
            clamped = min(max(v, lim.v_min), lim.v_max)
            if clamped != v:
                self._emit("warn", f"voltage clamped {v:.3f} -> {clamped:.3f} V")
            v = clamped
        # EVERY backend call goes through self._lock (deep cleaning 2026-09-28).
        # Two threads use this brain at once: the publisher reads the voltage
        # 8x a second for status, the commander sets it on request.  On the
        # KCube each pylablib call is several send/receive pairs on ONE USB
        # serial link, and pylablib has no lock of its own -- two calls at the
        # same time can each receive the other's reply.
        with self._lock:
            self._target = v
            self.backend.set_voltage(v)
        return v

    def read_voltage(self) -> float:
        with self._lock:
            return float(self.backend.read_voltage())

    def status(self) -> ZStatus:
        """Snapshot.  NEVER raises.

        The read-back is taken under self._lock, the same lock set_voltage
        holds for its write, so `voltage` is always what the KCube holds after
        the last write completed -- never ahead of it (there is no stepped
        approach here: set_voltage writes once).

        A failed read (2026-09-28) used to fall back to voltage = TARGET, i.e.
        the COMMANDED value.  A scan waiting for `echoes(voltage)` then settled
        at once, on a voltage nobody had read back.  Now the last GOOD read-back
        stays (NaN if there never was one), hw_error carries the message, and
        one error event goes out per failure episode, not one per frame.
        """
        note = None
        with self._lock:
            try:
                v = float(self.backend.read_voltage())
            except Exception as exc:  # noqa: BLE001 -- status must never raise
                msg = f"{type(exc).__name__}: {exc}"
                if not self._hw_error:
                    note = ("error", f"zpiezo: voltage read failed ({msg}); "
                                     f"showing the last good read-back")
                self._hw_error = msg
                v = self._last_v
            else:
                self._last_v = v
                if self._hw_error:
                    note = ("info", "zpiezo: voltage reads recovered")
                self._hw_error = ""
            target, connected, hw_error = self._target, self._connected, self._hw_error
        if note:
            self._emit(*note)                 # outside the lock
        # The envelope reported is the one the brain CLAMPS to: cfg.limits.
        # It used to come from backend.range(), which is a copy taken when the
        # backend was built -- after a set_config narrowed the limits, status
        # and `info` still announced the old ones (and the camera sizes its
        # autofocus sweep from `info`).
        lim = self.cfg.limits
        return ZStatus(connected=connected, voltage=v, target=target,
                       v_min=float(lim.v_min), v_max=float(lim.v_max),
                       hw_error=hw_error)

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
        """Apply a (partial) config dict -- all of it, or nothing.

        Values are cast to each field's type and the resulting envelope is
        checked BEFORE anything changes (``config.apply_config_dict``); a bad
        request raises and leaves both the config and the focus untouched.
        """
        from .config import apply_config_dict
        with self._lock:
            apply_config_dict(self.cfg, data)
            self.apply_config()

    def _emit(self, level: str, msg: str) -> None:
        try:
            self._on_event(level, msg)
        except Exception:
            pass


def status_to_dict(s: ZStatus) -> dict:
    return asdict(s)
