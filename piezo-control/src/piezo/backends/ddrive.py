"""The REAL backend: piezosystem jena d-Drive over a USB virtual COM port (§3).

This is the ONLY file that touches the serial hardware, and -- crucially -- it
imports :mod:`serial` (pyserial) **lazily inside** :meth:`open`, never at module
top.  That way the whole package still imports and the simulator still runs on a
PC that has no pyserial installed.  In ``pyproject.toml`` the ``pyserial``
dependency stays commented out until you are on the lab PC.

------------------------------------------------------------------------------
d-Drive wiring model
------------------------------------------------------------------------------
The d-Drive is a multi-channel digital controller.  Over USB it enumerates as a
virtual COM port (e.g. ``COM3``).  Each amplifier channel (0-based here) drives
one PXY-200 axis; we map logical axis 0/1 (X/Y) to channels in ``cfg.hardware``.

============================================================================
>>>  VERIFY THE COMMAND STRINGS BELOW AGAINST YOUR d-Drive MANUAL  <<<
============================================================================
piezosystem jena's ASCII protocol has varied across controller generations
(NV40 / NV200 / d-Drive) and firmware.  The exact verb spellings, whether a
channel index is sent, the value units (um vs 0-100 % stroke vs volts) and the
line terminator can differ.  EVERY wire string lives in the ``_CMD`` block just
below so you can fix them all in one place with the manual open -- the methods
only format these templates.  This is the module's "hardware pass" (the same
pattern the stepper-stage backend used for pylablib).

Defaults chosen here (typical for the d-Drive digital line, CR-terminated):
  * ``cl,<ch>,<0|1>``   set closed loop: 1 = closed (servo on), 0 = open.
  * ``set,<ch>,<val>``  set the target: position in um when closed loop, or the
                        drive level when open loop.
  * ``mess,<ch>``       query the measured position -> reply ``mess,<ch>,<val>``.
  * ``sr,<ch>,<val>``   set the slew rate (native velocity limit).  UNITS: on
                        many units this is % of full stroke per ms -- if so,
                        convert um/s <-> %/ms in ``_slew_to_wire`` below.
Adjust these to match your controller and delete this banner once verified.
"""

from __future__ import annotations

import threading

from ..config import Config, axis_channel

# --------------------------------------------------------------------------- #
# >>> THE ONE PLACE TO EDIT WIRE STRINGS <<<  (see banner above)
# --------------------------------------------------------------------------- #
_CMD = {
    "set_closed_loop": "cl,{ch},{state}",   # state = 1 (closed) / 0 (open)
    "set_setpoint":    "set,{ch},{value:.4f}",
    "query_position":  "mess,{ch}",
    "set_slew_rate":   "sr,{ch},{value:.4f}",
}
_TERM = b"\r"                                # line terminator the d-Drive expects
_ENCODING = "ascii"


class DDrivePiezo:
    """Adapter presenting the PiezoBackend interface over a d-Drive serial link."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.n = 2
        self._ser = None
        # A lock so the publisher (status reads) and the commander (writes) never
        # interleave bytes on the one shared serial line.
        self._io_lock = threading.Lock()
        # We can't always read the loop state back, so remember what we set.
        self._closed = [True, True]
        self._slew = [0.0, 0.0]
        self._channels = [axis_channel(cfg, 0), axis_channel(cfg, 1)]

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        # LAZY import: nothing above module scope depends on pyserial.
        import serial  # noqa: PLC0415  (intentional lazy import)

        self._ser = serial.Serial(
            port=self.cfg.hardware.port,
            baudrate=self.cfg.hardware.baud,
            timeout=0.5,        # read timeout (s)
            write_timeout=0.5,
        )
        # Push the start-up loop mode + slew rate for each axis.
        from ..config import axis_closed_loop_default, axis_velocity
        for axis in range(2):
            self.set_closed_loop(axis, axis_closed_loop_default(self.cfg, axis))
            self.set_slew_rate(axis, axis_velocity(self.cfg, axis))

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def idn(self) -> str:
        # The d-Drive has no universal *IDN?; report the port we opened.
        return f"piezosystem jena d-Drive @ {self.cfg.hardware.port}"

    # -- low-level serial helpers ----------------------------------------- #
    def _write(self, line: str) -> None:
        if self._ser is None:
            raise RuntimeError("serial port not open")
        with self._io_lock:
            self._ser.write(line.encode(_ENCODING) + _TERM)
            self._ser.flush()

    def _query(self, line: str) -> str:
        if self._ser is None:
            raise RuntimeError("serial port not open")
        with self._io_lock:
            self._ser.reset_input_buffer()
            self._ser.write(line.encode(_ENCODING) + _TERM)
            self._ser.flush()
            raw = self._ser.read_until(_TERM)
        return raw.decode(_ENCODING, errors="replace").strip()

    @staticmethod
    def _slew_to_wire(rate_um_s: float) -> float:
        """Convert um/s to the controller's slew-rate unit.

        >>> VERIFY <<< If your d-Drive expects um/s directly, this is identity.
        If it expects % of full stroke per ms, convert here using your actuator
        stroke, e.g.  pct_per_ms = (rate_um_s / stroke_um) * 100 / 1000.
        """
        return float(rate_um_s)

    # -- position ---------------------------------------------------------- #
    def set_setpoint(self, axis: int, position: float) -> None:
        self._write(_CMD["set_setpoint"].format(ch=self._channels[axis], value=float(position)))

    def read_position(self, axis: int) -> float:
        reply = self._query(_CMD["query_position"].format(ch=self._channels[axis]))
        # Expected reply like "mess,0,12.3456" -> take the last comma field.
        try:
            return float(reply.split(",")[-1])
        except (ValueError, IndexError):
            raise RuntimeError(f"unparsable position reply: {reply!r}")

    # -- loop mode --------------------------------------------------------- #
    def set_closed_loop(self, axis: int, enabled: bool) -> None:
        state = 1 if enabled else 0
        self._write(_CMD["set_closed_loop"].format(ch=self._channels[axis], state=state))
        self._closed[axis] = bool(enabled)

    def get_closed_loop(self, axis: int) -> bool:
        # d-Drive does not reliably echo the loop state, so return what we set.
        return self._closed[axis]

    # -- native slew rate -------------------------------------------------- #
    def set_slew_rate(self, axis: int, rate: float) -> None:
        wire = self._slew_to_wire(rate)
        self._write(_CMD["set_slew_rate"].format(ch=self._channels[axis], value=wire))
        self._slew[axis] = float(rate)

    def read_slew_rate(self, axis: int) -> float:
        return self._slew[axis]
