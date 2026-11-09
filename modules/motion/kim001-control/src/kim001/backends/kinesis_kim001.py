"""The REAL backend: Thorlabs KIM001 via pylablib/Kinesis (section 3 of the guide).

This is the ONLY file that touches the hardware driver, and -- crucially -- it
imports pylablib **lazily inside** :meth:`open`, never at module top.  That way
the whole package still imports and the simulator still runs on a PC that has
no Thorlabs Kinesis / pylablib installed.

==============================================================================
KIM001 wiring model
==============================================================================
The KIM001 is the SINGLE-channel K-Cube inertial piezo motor controller: one
PIA13 actuator on channel 1, here moving the microscope objective (focus).
The brain calls that axis 2 (Z) -- the kim wire shape the camera reads -- and
this file maps axis 2 to ``cfg.hardware.ch_z`` (1). Axis 0 / 1 (X / Y) do not
exist on this controller: :meth:`_ch` refuses them, so not even a bug in the
brain could send a command for a channel that is not there.

Everything here is in STEPS -- the controller's native unit.  The brain does
all micrometre conversion, so this file never sees a micrometre.

>>> NOTHING HERE HAS RUN ON A KIM001 YET <<<
This is kim-control's KIM101 backend (VERIFIED on the lab KIM101, pylablib
1.4.5, 2026-09-13) cut down to one channel. Whether pylablib's
``Thorlabs.KinesisPiezoMotor`` drives a KIM001 the same way is NOT confirmed:
pylablib documents the KIM101 / TIM101 family. Every vendor call below is
therefore marked ``# VERIFY``. What to check first on the instrument
(``help(Thorlabs.KinesisPiezoMotor)``, then a console session):

  * listing / opening : list_kinesis_devices() shows the KIM001 and its serial
                        prefix; KinesisPiezoMotor("<serial>") opens it.
  * identity          : get_device_info().model_no says KIM001 (used below to
                        refuse a KIM101 that shares the serial prefix).
  * channel           : every per-channel call with channel=1 works; whether
                        moves need the channel ENABLED first (auto_enable).
  * drive parameters  : get_drive_parameters / setup_drive(max_voltage=,
                        velocity=, acceleration=, channel=) as on the KIM101.
  * counter reset     : set_position_reference(0, channel=1).
  * is_moving         : True on the FIRST query after a move was accepted
                        (the scan settle rule relies on it, see kim001.py).

==============================================================================
THREADING: one serial link, several threads -- every call goes through a lock
==============================================================================
The service calls this backend from TWO threads at once: the publisher reads
status 8x per second, and the commander runs your moves. A K-Cube is one
request/reply byte stream over USB. If two threads interleave their requests,
one of them reads the reply meant for the other, and from then on every call
fails (found on the lab KIM101, 2026-09-13). So:

  * every device call goes through ``_call``, which holds ONE lock for the whole
    request+reply exchange;
  * after a failed call it flushes the link, so a single stray reply cannot
    desynchronise the stream for the rest of the session;
  * rate / acceleration / voltage are CACHED. They only change when we set them,
    so status polling costs 2 queries (position, moving) instead of 5.
"""

from __future__ import annotations

import threading

from .. import hwlock
from ..config import FITTED_AXES, Config, check_fitted

#: Model names that are NOT a KIM001: refused even when picked by serial,
#: because driving the sample stage's controller (KIM101) or another inertial
#: controller as "the objective focus" would move the wrong thing.
#: # VERIFY the exact model_no text pylablib reports for a KIM001 / KIM101.
OTHER_MODELS = ("KIM101", "KIM102", "TIM101")


class KinesisKim:
    """Adapter presenting the Kim001Backend interface over one KIM001 handle."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.n = 1
        self._dev = None  # the single KinesisPiezoMotor handle, filled in open()
        # RLock, not Lock: a locked section may call another locked helper.
        self._lock = threading.RLock()
        # axis -> TPZMotorDriveParams(max_voltage, velocity, acceleration),
        # refreshed from the controller after every write.
        self._drive: dict[int, object] = {}
        # The claim on this KIM001's serial number (hwlock). One physical
        # controller, one service: a second kim001 service -- or any other
        # module (kim, ...) pointed at the same serial -- is refused before it
        # sends a byte.
        self._lock_hw: hwlock.HardwareLock | None = None
        self._serial = ""

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        # LAZY import: nothing above module scope depends on pylablib.
        from pylablib.devices import Thorlabs  # noqa: PLC0415  (intentional)

        # An explicitly configured serial (hardware.serial, or --serial from
        # the launcher) always wins. Without one, _find_kim001 only LISTS the
        # Kinesis devices on the USB bus (it opens none of them), so claiming
        # right after it still comes before the first byte to the controller.
        serial = ((self.cfg.hardware.serial or "").strip()
                  or self._find_kim001(Thorlabs, self.cfg.hardware.serial_prefix))
        # Claim the physical controller by its serial BEFORE opening it. Raises
        # hwlock.HardwareBusy (naming the holder) when another service -- any
        # module -- already drives this controller; we then never touch it.
        self._lock_hw = hwlock.claim(serial, "kim001")
        self._serial = serial
        try:
            with self._lock:
                # VERIFY: that pylablib's KinesisPiezoMotor opens a KIM001, and
                # that its constructor only opens the link and queries (device
                # info), sending no drive-parameter / enable command of its own.
                self._dev = Thorlabs.KinesisPiezoMotor(serial)
                # Is it really a KIM001? A query only. Refuses a KIM101 that
                # shares the serial prefix -- that one moves the SAMPLE.
                self._check_model()
                # ADOPT, do not push (Lukas, 2026-09-27: "all modules should read
                # the instrument state on startup, not to change anything"): only
                # READ the drive parameters; the brain copies them into its config.
                for axis in FITTED_AXES:
                    self._refresh_drive(axis)
        except BaseException:
            # A failed open must not leave the controller claimed (the next
            # start would then report it "busy" -- by ourselves), nor a
            # half-open USB handle behind.
            self.close()
            raise

    @staticmethod
    def _find_kim001(thorlabs, prefix: str) -> str:
        """The serial of the one KIM001 on this PC, when none is configured.

        Thorlabs serials encode the product in their first digits; `prefix`
        (hardware.serial_prefix) is our best guess for the KIM001 -- # VERIFY.
        More than one match -> refuse and name them: moving the wrong actuator
        is worse than asking for `hardware.serial`. (If the KIM001 shares the
        KIM101's prefix, a rig with both plugged in lands here -- set the
        serial.)
        """
        prefix = (prefix or "").strip()
        listed = [str(conn).strip() for conn, _desc in thorlabs.list_kinesis_devices()]  # VERIFY
        found = [s for s in listed if prefix and s.startswith(prefix)]
        if not found:
            raise RuntimeError(f"no KIM001 found (no Kinesis serial starting with "
                               f"{prefix!r}). Is it connected, and is the Kinesis app "
                               f"closed? Or set hardware.serial (or --serial).")
        if len(found) > 1:
            raise RuntimeError(f"several Kinesis devices start with {prefix!r} "
                               f"({', '.join(found)}): set hardware.serial (or --serial) "
                               f"to the KIM001's serial")
        return found[0]

    def _check_model(self) -> None:
        """Refuse a controller that reports itself as something else.

        get_device_info() is a query. # VERIFY its model_no for a KIM001. If
        the call fails we cannot tell, and go on: the serial was given or
        picked by prefix, so this is a second line of defence, not the first.
        """
        try:
            info = self._call("get_device_info")  # VERIFY on the KIM001
        except Exception:
            return
        text = " ".join(str(getattr(info, k, "") or "") for k in ("model_no", "notes"))
        for other in OTHER_MODELS:
            if other in text.upper():
                raise RuntimeError(f"Kinesis device {self._serial} reports itself as "
                                   f"{other!r}, not a KIM001 -- refusing to drive it as "
                                   f"the objective focus (check hardware.serial)")

    def close(self) -> None:
        with self._lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                except Exception:
                    pass
            self._dev = None
            self._drive.clear()
            # Release the claim LAST, after the handle is closed, so no other
            # service can open the controller while we still hold the link.
            if self._lock_hw is not None:
                self._lock_hw.release()
                self._lock_hw = None

    def idn(self) -> str:
        if self._dev is None:
            return "Thorlabs KIM001 [closed]"
        try:
            info = self._call("get_device_info")  # VERIFY
            return f"Thorlabs KIM001 [{info.serial_no}]"
        except Exception:
            return "Thorlabs KIM001"

    # -- helpers ----------------------------------------------------------- #
    def _ch(self, axis: int) -> int:
        """Controller channel of a logical axis: Z -> hardware.ch_z (1).

        X / Y raise NotFitted: there is no channel for them on a KIM001."""
        check_fitted(axis)
        return int(self.cfg.hardware.ch_z)

    def _d(self):
        if self._dev is None:
            raise RuntimeError("KIM001 not open")
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
        self._drive[axis] = self._call("get_drive_parameters", channel=self._ch(axis))  # VERIFY

    def _cached_drive(self, axis: int):
        with self._lock:
            if axis not in self._drive:
                self._refresh_drive(axis)
            return self._drive[axis]

    # -- motion (STEPS) ---------------------------------------------------- #
    # pylablib (KIM101, verified 1.4.5): move_to/move_by(pos, auto_enable=True,
    # channel=None). On the KIM101 auto_enable had to be OFF (it enabled only
    # the addressed channel and so stopped the other one of the X/Y pair). A
    # KIM001 has ONE channel, so pylablib's auto_enable -- "enable this channel
    # if it is not" -- is exactly right and is left on.
    def move_to(self, axis: int, position_steps: int) -> None:
        ch = self._ch(axis)
        self._call("move_to", int(position_steps), auto_enable=True, channel=ch)  # VERIFY

    def move_by(self, axis: int, delta_steps: int) -> None:
        ch = self._ch(axis)
        self._call("move_by", int(delta_steps), auto_enable=True, channel=ch)  # VERIFY

    def is_moving(self, axis: int) -> bool:
        # VERIFY on the KIM001: must report True as soon as a move has been
        # ACCEPTED (i.e. on the first query after move_to returned). The scan
        # settle rule (target echo + moving, kim001.py _set_target) relies on it.
        # Test: move_to far away, then is_moving() at once, many times.
        return bool(self._call("is_moving", channel=self._ch(axis)))

    def stop(self, axis: int) -> None:
        # sync=False: send the stop and return, rather than holding the lock
        # (and so the whole link) until the actuator has come to rest.
        self._call("stop", channel=self._ch(axis), sync=False)  # VERIFY

    def read_position(self, axis: int) -> int:
        return int(self._call("get_position", channel=self._ch(axis)))  # VERIFY

    def zero_counter(self, axis: int) -> None:
        # Reset the open-loop step counter to 0 at the current position.
        self._call("set_position_reference", 0, channel=self._ch(axis))  # VERIFY

    # -- drive parameters -------------------------------------------------- #
    # setup_drive(max_voltage=None, velocity=None, acceleration=None, channel=None):
    # a None argument keeps the controller's current value. Each write is read
    # back, so the cache holds what the controller ACCEPTED, not what we asked.
    # The KIM101 takes whole numbers: ROUND (int() truncated, 99.7 V -> 99 V).
    def set_step_rate(self, axis: int, steps_per_sec: float) -> None:
        # velocity == step rate for a piezo inertia motor.
        with self._lock:
            self._call("setup_drive", velocity=int(round(steps_per_sec)),
                       channel=self._ch(axis))  # VERIFY
            self._refresh_drive(axis)

    def read_step_rate(self, axis: int) -> float:
        return float(self._cached_drive(axis).velocity)

    def set_acceleration(self, axis: int, steps_per_sec2: float) -> None:
        with self._lock:
            self._call("setup_drive", acceleration=int(round(steps_per_sec2)),
                       channel=self._ch(axis))  # VERIFY
            self._refresh_drive(axis)

    def read_acceleration(self, axis: int) -> float:
        return float(self._cached_drive(axis).acceleration)

    def set_voltage(self, axis: int, volts: float) -> None:
        with self._lock:
            self._call("setup_drive", max_voltage=int(round(volts)),
                       channel=self._ch(axis))  # VERIFY
            self._refresh_drive(axis)

    def read_voltage(self, axis: int) -> float:
        return float(self._cached_drive(axis).max_voltage)
