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

from ..config import (
    Config,
    axis_acceleration,
    axis_velocity,
)


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

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        # LAZY import: nothing above module scope depends on pylablib.
        from pylablib.devices import Thorlabs  # noqa: PLC0415  (intentional)

        serial = self.cfg.hardware.serial
        scale = self.cfg.hardware.scale

        for axis, channel in enumerate(self._channels):
            # --- style A: one handle per (serial, channel) tuple ----------- #
            motor = Thorlabs.KinesisMotor((serial, channel), scale=scale)
            # --- style B (if your pylablib wants a single multi-channel
            #     handle) would instead open once and pass channel= to calls.
            self._motors[axis] = motor

        # Push the configured motion parameters straight away.
        for axis in range(3):
            self.set_velocity(axis, axis_velocity(self.cfg, axis))
            self.set_acceleration(axis, axis_acceleration(self.cfg, axis))

    def close(self) -> None:
        for motor in self._motors:
            if motor is not None:
                try:
                    motor.close()
                except Exception:
                    pass
        self._motors = [None, None, None]

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
