"""The REAL backend: Thorlabs BSC203 via pylablib/Kinesis (§3 of the guide).

This is the ONLY file that touches the hardware driver, and -- crucially -- it
imports pylablib **lazily inside** :meth:`open`, never at module top.  That way
the whole package still imports and the simulator still runs on a PC that has
no Thorlabs Kinesis / pylablib installed.  In ``pyproject.toml`` the pylablib
dependency stays commented out until you are on the lab PC.

------------------------------------------------------------------------------
BSC203 wiring model
------------------------------------------------------------------------------
The BSC203 is a 3-channel benchtop stepper controller: one USB/serial device
(a Kinesis serial number like ``70xxxxxx``) exposing three bay channels 1..3.
We open one pylablib ``KinesisMotor`` per axis, each addressed by its channel,
and map logical axis 0/1/2 (X/Y/Z) to the channels in ``cfg.hardware``.

>>> VERIFY ON HARDWARE <<<
pylablib's multi-channel handling for the BSC series has evolved across
versions.  Depending on your pylablib version you may need either:

    Thorlabs.KinesisMotor(("70xxxxxx", channel), scale="stage")   # tuple conn
or  Thorlabs.KinesisMotor("70xxxxxx", scale="stage")              # then pass
                                                                  # channel= to
                                                                  # each call

Both styles are sketched below; pick the one that matches your installed
pylablib and delete the other.  Everything else in the app is backend-agnostic,
so this file is the single place you adapt.  Units come back in mm when opened
with ``scale="stage"`` (pylablib loads the actuator calibration).
"""

from __future__ import annotations

from ..config import Config
from ..hwlock import claim

# The module name written into the lock file, so a refused second service
# can tell you WHO holds the controller.
MODULE_KEY = "stage"


class KinesisStage:
    """Adapter presenting the StageBackend interface over pylablib."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.n = 3
        # One motor handle per axis; filled in open().
        self._motors: list = [None, None, None]
        # Resolve axis -> physical channel, honouring the swap_xy convenience.
        chans = [cfg.hardware.ch_x, cfg.hardware.ch_y, cfg.hardware.ch_z]
        if cfg.hardware.swap_xy:
            chans[0], chans[1] = chans[1], chans[0]
        self._channels = chans
        # The hardware claim on the BSC203's Kinesis serial number (see
        # open()).  None while the controller is not ours.
        self._lock = None

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        serial = str(self.cfg.hardware.serial).strip()
        scale = self.cfg.hardware.scale
        if not serial:
            # pylablib cannot pick "the first BSC203" for us, and an empty
            # address could not be claimed meaningfully either.
            raise ValueError("hardware.serial is empty: set the BSC203's Kinesis "
                             "serial number (starts with 70...) in the config")

        # ONE INSTRUMENT, ONE SERVICE (Lukas's rule): the BSC203 is identified
        # by its Kinesis serial number, whatever module points at it.  Claim it
        # BEFORE the first byte goes to the controller; a second stage service
        # (or any other module configured with this serial) gets HardwareBusy
        # naming the holder, and never touches the motors.  The three axes are
        # three channels of ONE box, so one claim covers them all.
        self._lock = claim(serial, MODULE_KEY)
        try:
            # LAZY import: nothing above module scope depends on pylablib.
            from pylablib.devices import Thorlabs  # noqa: PLC0415  (intentional)

            for axis, channel in enumerate(self._channels):
                # --- style A: one handle per (serial, channel) tuple ------- #
                motor = Thorlabs.KinesisMotor((serial, channel), scale=scale)  # VERIFY
                # --- style B (if your pylablib wants a single multi-channel
                #     handle) would instead open once and pass channel= to calls.
                self._motors[axis] = motor
        except BaseException:
            # A failed open must not leave the serial claimed (the next start
            # would then report "busy" against ourselves) nor half-open handles.
            self.close()
            raise

        # NO writes here (Lukas's adopt-on-start rule, 2026-09-27): opening the
        # stage must not change it.  The old code pushed cfg velocity and
        # acceleration to every axis at this point; now the brain READS them
        # (read_velocity / read_acceleration) and adopts them into the config.
        # VERIFY: that constructing KinesisMotor itself sends nothing that
        # changes the controller (pylablib may enable the channel or query the
        # stage scale on open -- a query is fine, an enable/parameter write is
        # not).  Check by comparing velocity/position in the Kinesis GUI before
        # and after starting the service.

    def close(self) -> None:
        for motor in self._motors:
            if motor is not None:
                try:
                    motor.close()
                except Exception:
                    pass
        self._motors = [None, None, None]
        # Release the claim LAST, after the handles are closed, so no other
        # service can open the controller while we still hold it.
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def idn(self) -> str:
        parts = []
        for axis, motor in enumerate(self._motors):
            if motor is None:
                continue
            try:
                info = motor.get_device_info()
                parts.append(f"{'XYZ'[axis]}:{info.serial_no}")
            except Exception:
                parts.append(f"{'XYZ'[axis]}:?")
        return "Thorlabs BSC203 [" + ", ".join(parts) + "]"

    # -- helpers ----------------------------------------------------------- #
    def _m(self, axis: int):
        motor = self._motors[axis]
        if motor is None:
            raise RuntimeError(f"axis {axis} not open")
        return motor

    # -- motion ------------------------------------------------------------ #
    def home(self, axis: int) -> None:
        # sync=False -> fire-and-forget; we poll is_homed() for completion.
        self._m(axis).home(sync=False)

    def is_homed(self, axis: int) -> bool:
        return bool(self._m(axis).is_homed())

    def move_to(self, axis: int, position: float) -> None:
        self._m(axis).move_to(position)

    def is_moving(self, axis: int) -> bool:
        return bool(self._m(axis).is_moving())

    def stop(self, axis: int) -> None:
        # immediate=True -> hard stop; use sync=False so we don't block.
        try:
            self._m(axis).stop(immediate=True, sync=False)
        except TypeError:
            # Older pylablib signatures.
            self._m(axis).stop()

    def read_position(self, axis: int) -> float:
        return float(self._m(axis).get_position())

    # -- parameters -------------------------------------------------------- #
    def set_velocity(self, axis: int, velocity: float) -> None:
        # pylablib bundles velocity params together; set just max_velocity.
        self._m(axis).setup_velocity(max_velocity=velocity)

    def read_velocity(self, axis: int) -> float:
        # get_velocity_parameters() -> (min_velocity, acceleration, max_velocity)
        return float(self._m(axis).get_velocity_parameters()[2])

    def set_acceleration(self, axis: int, acceleration: float) -> None:
        self._m(axis).setup_velocity(acceleration=acceleration)

    def read_acceleration(self, axis: int) -> float:
        return float(self._m(axis).get_velocity_parameters()[1])
