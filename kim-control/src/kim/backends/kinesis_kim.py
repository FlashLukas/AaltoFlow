"""The REAL backend: Thorlabs KIM101 via pylablib/Kinesis (§3 of the guide).

This is the ONLY file that touches the hardware driver, and -- crucially -- it
imports pylablib **lazily inside** :meth:`open`, never at module top.  That way
the whole package still imports and the simulator still runs on a PC that has
no Thorlabs Kinesis / pylablib installed.  In ``pyproject.toml`` the pylablib
dependency stays commented out until you are on the lab PC.

==============================================================================
KIM101 wiring model
==============================================================================
Unlike the BSC203 (which is three separate motor axes), the KIM101 is ONE
K-Cube exposing up to FOUR channels (1..4) on a single Kinesis device.  So we
open ONE pylablib handle and address each axis by its channel number.  Logical
axis 0/1/2 (X/Y/Z) maps to channels in ``cfg.hardware`` (default 1/2/3).

Everything here is in STEPS -- the controller's native unit.  The brain does
all micrometre conversion, so this file never sees a micrometre.

>>> VERIFY ON HARDWARE (lab PC) <<<
pylablib exposes the KIM series as ``Thorlabs.KinesisPiezoMotor``.  Method names
and argument spellings have shifted across pylablib versions, and the exact
drive-parameter names matter, so confirm each of the calls marked ``# VERIFY``
below against your installed pylablib (``help(Thorlabs.KinesisPiezoMotor)``):

  * opening              : KinesisPiezoMotor("97xxxxxx")   (serial as a string)
  * per-channel calls    : most take a ``channel=`` keyword; some need the
                           channel selected first with ``_setup_channel`` /
                           ``enable_channels``.
  * drive parameters     : set_drive_parameters(max_voltage=, velocity=,
                           acceleration=, channel=) where velocity is the STEP
                           RATE (steps/s) and acceleration is steps/s^2.
  * counter reset (datum): set_position_reference(0, channel=)   (a.k.a. zero).

Everything else in the app is backend-agnostic, so THIS file is the single
place you adapt.

VERIFIED on the lab unit 2026-09-13 (KIM101 S/N 97000000, FW 1.0.7,
pylablib 1.4.5): every method name and keyword below exists as written.
Exercised on hardware: open by serial string, get_device_info, get_position,
setup_drive(max_voltage|velocity|acceleration) with read-back through
get_drive_parameters, move_by (+50/-50 steps on ch1, counter 31 -> 81 -> 31)
and is_moving; then through the service + GUI, move_to (every GUI move goes
through it) with the stage seen moving, and Datum (set_position_reference)
reported working by Lukas. Not yet deliberately tested: stop mid-move.
Note that the counter is open-loop -- it counts steps SENT, so it proves the
command path, not that the actuator physically moved.

==============================================================================
THREADING: one serial link, several threads -- every call goes through a lock
==============================================================================
The service calls this backend from TWO threads at once: the publisher reads
status 8x per second, and the commander runs your moves. The KIM101 is one
request/reply byte stream over USB. If two threads interleave their requests,
one of them reads the reply meant for the other, and from then on every call
fails with "unexpected channel in the reply" -- the GUI shows zeros and nothing
moves (found on the lab unit 2026-09-13). So:

  * every device call goes through ``_call``, which holds ONE lock for the whole
    request+reply exchange;
  * after a failed call it flushes the link, so a single stray reply cannot
    desynchronise the stream for the rest of the session;
  * rate / acceleration / voltage are CACHED. They only change when we set them,
    so status polling costs 6 queries instead of 15.
"""

from __future__ import annotations

import threading

from ..config import (
    Config,
    axis_acceleration,
    axis_rate,
    axis_voltage,
)


class KinesisKim:
    """Adapter presenting the KimBackend interface over one KIM101 handle."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.n = 3
        self._dev = None  # the single KinesisPiezoMotor handle, filled in open()
        # RLock, not Lock: a locked section may call another locked helper.
        self._lock = threading.RLock()
        # axis -> TPZMotorDriveParams(max_voltage, velocity, acceleration),
        # refreshed from the controller after every write.
        self._drive: dict[int, object] = {}
        # Resolve axis -> physical channel, honouring the swap_xy convenience.
        chans = [cfg.hardware.ch_x, cfg.hardware.ch_y, cfg.hardware.ch_z]
        if cfg.hardware.swap_xy:
            chans[0], chans[1] = chans[1], chans[0]
        self._channels = chans

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        # LAZY import: nothing above module scope depends on pylablib.
        from pylablib.devices import Thorlabs  # noqa: PLC0415  (intentional)

        # A single handle for the whole K-Cube; channels addressed per call.
        serial = (self.cfg.hardware.serial or "").strip() or self._find_kim101(Thorlabs)
        with self._lock:
            self._dev = Thorlabs.KinesisPiezoMotor(serial)
            # Push the configured drive parameters straight away (this also
            # fills the drive-parameter cache for every axis).
            for axis in range(3):
                self.set_step_rate(axis, axis_rate(self.cfg, axis))
                self.set_acceleration(axis, axis_acceleration(self.cfg, axis))
                self.set_voltage(axis, axis_voltage(self.cfg, axis))

    @staticmethod
    def _find_kim101(thorlabs) -> str:
        """The serial of the one KIM101 on this PC, when none is configured.

        Thorlabs serials encode the product: KIM101 controllers start with 97
        (the BSC203 stage controller, say, with 70). # VERIFY on a second unit.
        More than one -> refuse and name them: moving the wrong stage is worse
        than asking for `hardware.serial`.
        """
        found = [str(conn) for conn, _desc in thorlabs.list_kinesis_devices()
                 if str(conn).startswith("97")]
        if not found:
            raise RuntimeError("no KIM101 found (serials starting with 97). Is it "
                               "connected, and is the Kinesis app closed?")
        if len(found) > 1:
            raise RuntimeError(f"several KIM101 found ({', '.join(found)}): set "
                               f"hardware.serial to the one to use")
        return found[0]

    def close(self) -> None:
        with self._lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                except Exception:
                    pass
            self._dev = None
            self._drive.clear()
            self._enabled = None

    def idn(self) -> str:
        if self._dev is None:
            return "Thorlabs KIM101 [closed]"
        try:
            info = self._call("get_device_info")
            return f"Thorlabs KIM101 [{info.serial_no}]"
        except Exception:
            return "Thorlabs KIM101"

    # -- helpers ----------------------------------------------------------- #
    def _ch(self, axis: int) -> int:
        return self._channels[axis]

    def _d(self):
        if self._dev is None:
            raise RuntimeError("KIM101 not open")
        return self._dev

    def _call(self, method: str, *args, **kwargs):
        """Run ONE device call with the link to ourselves (see module docstring)."""
        with self._lock:
            dev = self._d()
            try:
                return getattr(dev, method)(*args, **kwargs)
            except Exception:
                # Drop any half-read reply still sitting in the buffer, so the
                # NEXT request pairs with its own reply again.
                try:
                    dev.flush_comm()
                except Exception:
                    pass
                raise

    def _refresh_drive(self, axis: int) -> None:
        self._drive[axis] = self._call("get_drive_parameters", channel=self._ch(axis))

    def _cached_drive(self, axis: int):
        with self._lock:
            if axis not in self._drive:
                self._refresh_drive(axis)
            return self._drive[axis]

    # -- motion (STEPS) ---------------------------------------------------- #
    # Verified signatures (pylablib 1.4.5): move_to/move_by(pos, auto_enable=True,
    # channel=None).
    #
    # CHANNEL ENABLE (found 2026-09-14): the KIM101 only drives ENABLED channels,
    # either one, or the pairs (1,2) / (3,4). pylablib's auto_enable enables
    # JUST the addressed channel -- which DISABLES the other one mid-move. So an
    # X move followed at once by a Y move (the camera's XY jog, its stabiliser,
    # kim's move_image_px) stopped X after a few steps: "a 50 um jog moves half
    # a micron". We enable the PAIR a channel belongs to, so X and Y run
    # together. Z (ch3) is in the other pair: a Z move still stops an XY move in
    # progress, and vice versa.
    @staticmethod
    def _pair(channel: int) -> tuple:
        return (1, 2) if channel <= 2 else (3, 4)

    def _enable_for(self, channel: int) -> None:
        with self._lock:
            want = self._pair(channel)
            if getattr(self, "_enabled", None) != want:
                got = self._call("enable_channels", want)
                self._enabled = tuple(got) if got else None

    def move_to(self, axis: int, position_steps: int) -> None:
        ch = self._ch(axis)
        with self._lock:
            self._enable_for(ch)
            self._call("move_to", int(position_steps), auto_enable=False, channel=ch)

    def move_by(self, axis: int, delta_steps: int) -> None:
        ch = self._ch(axis)
        with self._lock:
            self._enable_for(ch)
            self._call("move_by", int(delta_steps), auto_enable=False, channel=ch)

    def is_moving(self, axis: int) -> bool:
        return bool(self._call("is_moving", channel=self._ch(axis)))

    def stop(self, axis: int) -> None:
        # sync=False: send the stop and return, rather than holding the lock
        # (and so the whole link) until the actuator has come to rest.
        self._call("stop", channel=self._ch(axis), sync=False)

    def read_position(self, axis: int) -> int:
        return int(self._call("get_position", channel=self._ch(axis)))

    def zero_counter(self, axis: int) -> None:
        # Reset the open-loop step counter to 0 at the current position.
        self._call("set_position_reference", 0, channel=self._ch(axis))  # VERIFY on hardware

    # -- drive parameters -------------------------------------------------- #
    # setup_drive(max_voltage=None, velocity=None, acceleration=None, channel=None):
    # a None argument keeps the controller's current value. Each write is read
    # back, so the cache holds what the controller ACCEPTED, not what we asked.
    def set_step_rate(self, axis: int, steps_per_sec: float) -> None:
        # velocity == step rate for a KIM piezo motor.
        with self._lock:
            self._call("setup_drive", velocity=int(steps_per_sec), channel=self._ch(axis))
            self._refresh_drive(axis)

    def read_step_rate(self, axis: int) -> float:
        return float(self._cached_drive(axis).velocity)

    def set_acceleration(self, axis: int, steps_per_sec2: float) -> None:
        with self._lock:
            self._call("setup_drive", acceleration=int(steps_per_sec2), channel=self._ch(axis))
            self._refresh_drive(axis)

    def read_acceleration(self, axis: int) -> float:
        return float(self._cached_drive(axis).acceleration)

    def set_voltage(self, axis: int, volts: float) -> None:
        with self._lock:
            self._call("setup_drive", max_voltage=int(volts), channel=self._ch(axis))
            self._refresh_drive(axis)

    def read_voltage(self, axis: int) -> float:
        return float(self._cached_drive(axis).max_voltage)
