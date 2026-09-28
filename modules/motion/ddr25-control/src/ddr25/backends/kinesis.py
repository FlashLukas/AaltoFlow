"""The REAL backend: DDR25/M on a K-Cube brushless controller, via pylablib.

This is the ONLY file that touches the hardware driver, and it imports
pylablib **lazily inside** :meth:`open`, never at module top, so the package
imports and the simulator runs on a PC without pylablib / Kinesis. pylablib
is in the optional extra ``real`` (``.\\dev.ps1 sync --extra gui --extra real``).

NEVER RUN ON THE INSTRUMENT YET. Sources used:

* pylablib ``pylablib/devices/Thorlabs/kinesis.py`` (main branch, read
  2026-09-27): ``KinesisMotor(conn, scale="step", default_channel=1)``;
  ``scale`` may be a stage NAME, and for ``"DDR25"`` pylablib uses
  1 440 000 counts / 360 deg and reports degrees; ``home(sync, force,
  channel, timeout)``, ``is_homed()``, ``is_moving()``, ``move_to(position)``,
  ``stop(immediate, sync)``, ``get_position()``, ``setup_velocity(min_velocity,
  acceleration, max_velocity)``, ``get_velocity_parameters()`` ->
  ``TVelocityParams(min_velocity, acceleration, max_velocity)``,
  ``get_status()`` -> strings such as "moving_fw", "homing", "homed",
  "enabled"; ``_enable_channel`` exists but is private on KinesisMotor.
  The KBD101 branch of pylablib's velocity/acceleration scaling uses the
  brushless time base (102.4 us), so deg/s come out right -- IF the controller
  identifies itself as "KBD101".
* pylablib's stage AUTODETECTION has no KBD101 branch: ``scale="stage"`` would
  fall back to raw counts. Hence the config names the stage explicitly.
* Checked line by line against that pylablib source by the reviewer
  (2026-09-27): ``_get_step_scale("DDR25")`` = 1440000/360 counts/deg,
  units "deg"; ``_calculate_scale`` uses time_conv = 102.4e-6 s for
  "KBD101" (velocity scale = counts * t * 2^16, acceleration
  counts * t^2 * 2^16 -> ONE internal acceleration unit is ~0.36 deg/s^2,
  so an acceleration read back differs from the one set by up to that);
  ``_home(sync, force, channel, timeout)``; ``_stop(immediate, sync,
  channel, timeout)``; ``_setup_velocity(min_velocity, acceleration,
  max_velocity, channel, scale)``; ``get_device_info()`` -> TDeviceInfo with
  ``model_no`` / ``fw_ver``; status bits include "homing", "homed",
  "enabled" (bit 31) and the moving set ``moving_fw, moving_bk, jogging_fw,
  jogging_bk, active``.
* IMPORTANT pylablib detail: ``is_moving()`` checks ONLY that moving set --
  "homing" is NOT in it. So during a home the real controller reports
  "not moving". This backend therefore reads ``get_status()`` ONCE per poll
  and counts "homing" as moving; otherwise the brain would give up on a
  home after its start grace while the stage is still turning.
* Thorlabs DDR25 product page: up to 1800 deg/s, 7200 deg/s^2 with a light
  load; direct drive, no backlash; optical encoder.

Every call below that was not confirmed on the device is marked ``# VERIFY``.
"""

from __future__ import annotations

from .. import hwlock
from ..config import Config

# The name written into the lock file, so a second service that finds the
# K-Cube taken can say WHO holds it.
MODULE_KEY = "ddr25"


class KinesisRotator:
    """Adapter presenting the RotatorBackend interface over pylablib."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._m = None
        # The brain's poll calls read_position, is_moving and is_homed in a
        # row, under one lock. is_moving fetches the status word (one USB
        # round trip) and is_homed reuses it, so a poll costs two round
        # trips, not three.
        self._status: list = []
        # Hardware lock on the K-Cube's serial number (hwlock.py). Held from
        # open() to close() so no other service -- say a second ddr25, or a
        # generic Kinesis module pointed at the same serial -- can drive this
        # stage at the same time. The sim backend never claims anything.
        self._lock: hwlock.HardwareLock | None = None

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        # LAZY import: nothing at module scope depends on pylablib.
        try:
            from pylablib.devices import Thorlabs  # noqa: PLC0415 (intentional)
        except ImportError as exc:  # pragma: no cover - lab PC only
            raise RuntimeError(
                "pylablib is not installed: run  .\\dev.ps1 sync --extra gui --extra real"
            ) from exc
        hw = self.cfg.hardware
        serial = str(hw.serial).strip()
        if not serial:
            # No auto-discovery on purpose: "the first K-Cube found" could be
            # another module's controller, and the lock must name the box.
            raise RuntimeError("hardware.serial is empty: set the K-Cube's serial number")
        # Claim the PHYSICAL address (the Kinesis serial) BEFORE the first
        # byte goes to the controller. If another service already holds it,
        # hwlock raises HardwareBusy naming that service, and we never talk
        # to a controller that is not ours.
        self._lock = hwlock.claim(serial, MODULE_KEY)
        try:
            # One K-Cube = one channel, so the default channel 1 is the stage.
            self._m = Thorlabs.KinesisMotor(serial, scale=hw.scale)  # VERIFY scale="DDR25" on a KBD101
        except BaseException:
            # A failed open must not leave the serial claimed: otherwise a
            # retry (or the other module) would be told "busy" by a service
            # that never managed to connect.
            self._release_lock()
            raise
        # open() only CONNECTS and reads (adopt-on-start rule, 2026-09-27):
        # no enable, no profile, no homing. A brushless servo may power up
        # with its channel DISABLED (it then does not hold position and
        # ignores moves); enabling it changes the instrument's state, so it
        # is done in _ensure_enabled() just before the first move or home the
        # user commands -- never at start.

    def _ensure_enabled(self) -> None:
        """Enable the channel if the status word says it is disabled.

        Called only from user-commanded motion (move_to, home). pylablib
        keeps the enable call private on KinesisMotor. VERIFY whether it is
        needed at all on a KBD101 and that "enabled" is the right bit name.
        """
        try:
            if "enabled" not in self._dev().get_status():  # VERIFY status string
                self._dev()._enable_channel(True)           # VERIFY private API
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._m is not None:
                try:
                    self._m.close()
                except Exception:
                    pass
            self._m = None
        finally:
            # Release AFTER the connection is closed, so there is no moment
            # where another service could open a controller we still talk to.
            self._release_lock()

    def _release_lock(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    def idn(self) -> str:
        try:
            info = self._dev().get_device_info()  # VERIFY fields on a KBD101
            return f"Thorlabs {info.model_no} + DDR25 (fw {info.fw_ver})"
        except Exception:
            return "Thorlabs K-Cube + DDR25"

    def _dev(self):
        if self._m is None:
            raise RuntimeError("K-Cube not open")
        return self._m

    # -- motion ------------------------------------------------------------ #
    def home(self) -> None:
        # sync=False: fire-and-forget; the brain polls is_homed().
        # force=True: re-home even if the controller says it already is (the
        # user asked for it, e.g. after the stage was turned by hand).
        self._ensure_enabled()
        self._status = []           # the cached word predates the home
        self._dev().home(sync=False, force=True)  # VERIFY direction/velocity (setup_homing untouched)

    def _read_status(self) -> list:
        self._status = list(self._dev().get_status())  # VERIFY bit names on a KBD101
        return self._status

    def is_homed(self) -> bool:
        # Uses the status word is_moving() just fetched (same poll, same lock);
        # fetches its own if none is cached yet.
        st = self._status or self._read_status()
        return "homed" in st  # VERIFY: cleared when a new home starts?

    def move_to(self, position: float) -> None:
        # Absolute, in scaled units (deg). The counter is NOT wrapped by the
        # firmware (Kinesis' "rotation mode" is a PC-software setting, which
        # the APT protocol pylablib speaks bypasses) -- VERIFY with a move
        # past 360 that the position reads 370, not 10.
        self._ensure_enabled()
        self._dev().move_to(float(position))  # VERIFY

    # pylablib's KinesisMotor._moving_status, plus "homing" (see docstring).
    _BUSY = ("moving_fw", "moving_bk", "jogging_fw", "jogging_bk", "active", "homing")

    def is_moving(self) -> bool:
        st = self._read_status()
        return any(b in st for b in self._BUSY)  # VERIFY "active" is clear at rest on a servo

    def stop(self, immediate: bool = False) -> None:
        # sync=False so the command thread is not blocked for the deceleration.
        self._dev().stop(immediate=bool(immediate), sync=False)  # VERIFY

    def read_position(self) -> float:
        return float(self._dev().get_position())  # VERIFY units = deg

    # -- parameters -------------------------------------------------------- #
    def set_velocity(self, velocity: float) -> None:
        self._dev().setup_velocity(max_velocity=float(velocity))  # VERIFY scaling on KBD101

    def set_acceleration(self, acceleration: float) -> None:
        self._dev().setup_velocity(acceleration=float(acceleration))  # VERIFY scaling on KBD101

    def read_velocity_params(self) -> tuple[float, float]:
        p = self._dev().get_velocity_parameters()  # VERIFY
        return float(p.max_velocity), float(p.acceleration)
