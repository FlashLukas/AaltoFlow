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
  * The same verbs WITHOUT a value (``cl,<ch>``, ``set,<ch>``, ``sr,<ch>``) are
    assumed to be QUERIES that echo ``<verb>,<ch>,<val>``.  psj's ASCII protocol
    generally works this way, but it is unconfirmed for the d-Drive.  Before
    trusting it, check on the bench that e.g. ``set,0`` alone does NOT move the
    stage (it must only report the setpoint).
Adjust these to match your controller and delete this banner once verified.

------------------------------------------------------------------------------
Start-up rule (Lukas, 2026-09-27): READ, never write
------------------------------------------------------------------------------
``open()`` only opens the port.  It does not push the loop mode, the slew rate
or a setpoint: whatever the controller is doing when the service starts (maybe
another program, or the front panel, left it in open loop at 87 um) is what the
brain ADOPTS via the query methods below.  Starting the software must never
move the stage or change its mode.
"""

from __future__ import annotations

import threading

from ..config import Config, axis_channel
from ..hwlock import claim

# --------------------------------------------------------------------------- #
# >>> THE ONE PLACE TO EDIT WIRE STRINGS <<<  (see banner above)
# --------------------------------------------------------------------------- #
_CMD = {
    "set_closed_loop": "cl,{ch},{state}",   # state = 1 (closed) / 0 (open)
    "set_setpoint":    "set,{ch},{value:.4f}",
    "query_position":  "mess,{ch}",
    "set_slew_rate":   "sr,{ch},{value:.4f}",
    # Queries used to ADOPT the controller's state at start (no value = query).
    "query_closed_loop": "cl,{ch}",          # VERIFY: reply "cl,<ch>,<0|1>"
    "query_setpoint":    "set,{ch}",         # VERIFY: reply "set,<ch>,<val>"; must NOT move
    "query_slew_rate":   "sr,{ch}",          # VERIFY: reply "sr,<ch>,<val>" (wire unit)
}
_TERM = b"\r"                                # line terminator the d-Drive expects
_ENCODING = "ascii"


class DDrivePiezo:
    """Adapter presenting the PiezoBackend interface over a d-Drive serial link."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.n = 2
        self._ser = None
        # The claim on our COM port (see open()); None while closed.
        self._hw_lock = None
        # A lock so the publisher (status reads) and the commander (writes) never
        # interleave bytes on the one shared serial line.
        self._io_lock = threading.Lock()
        # Last known loop state / slew rate per axis: filled by the queries at
        # start (adoption) and by our own writes afterwards.  Used only as a
        # fallback if a later query fails.
        self._closed = [True, True]
        self._slew = [0.0, 0.0]
        self._channels = [axis_channel(cfg, 0), axis_channel(cfg, 1)]

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        # ONE instrument, ONE service (Lukas: "the same instrument has to be
        # defined by the same physical address").  The d-Drive IS its COM
        # port, so we claim the port before opening it.  If another service --
        # a second piezo, or any module pointed at the same port -- already
        # holds it, claim() raises HardwareBusy naming the holder, and we have
        # sent nothing.  "com3" and "COM3" are the same claim (hwlock
        # normalises the spelling).
        self._hw_lock = claim(self.cfg.hardware.port, "piezo")
        try:
            # LAZY import: nothing above module scope depends on pyserial.
            import serial  # noqa: PLC0415  (intentional lazy import)

            self._ser = serial.Serial(
                port=self.cfg.hardware.port,
                baudrate=self.cfg.hardware.baud,
                timeout=0.5,        # read timeout (s)
                write_timeout=0.5,
            )
        except BaseException:
            # A failed open must not leave the port claimed, or the NEXT start
            # (after fixing the cable or the port name) would find it "busy".
            self._release_claim()
            raise
        # Nothing is WRITTEN here (adopt rule, see the module docstring): the
        # brain reads the loop mode, setpoint and slew rate through the query
        # methods and adopts them.  The old version pushed the config's loop
        # mode and velocity at this point, which could flip a running stage
        # from open to closed loop (and jump it) just by starting the service.

    def _release_claim(self) -> None:
        lock, self._hw_lock = self._hw_lock, None
        if lock is not None:
            lock.release()

    def close(self) -> None:
        try:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
        finally:
            # Release AFTER the port is closed, so the next owner never sees
            # the port still open in our process.
            self._release_claim()

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

    @staticmethod
    def _slew_from_wire(value: float) -> float:
        """Inverse of :meth:`_slew_to_wire` (controller unit -> um/s).  VERIFY."""
        return float(value)

    def _query_value(self, key: str, axis: int) -> float:
        """Send a query from ``_CMD`` and return the last comma field as float."""
        reply = self._query(_CMD[key].format(ch=self._channels[axis]))
        try:
            return float(reply.split(",")[-1])
        except (ValueError, IndexError):
            raise RuntimeError(f"unparsable reply to {key}: {reply!r}")

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
        """Ask the controller whether the servo is on.  # VERIFY query + reply.

        Raises if the reply cannot be parsed; the brain then falls back to its
        config default WITHOUT writing it (and says so in an event).
        """
        state = self._query_value("query_closed_loop", axis)
        self._closed[axis] = state >= 0.5
        return self._closed[axis]

    def read_setpoint(self, axis: int) -> float:
        """The controller's CURRENT target (not the measured position).  # VERIFY

        In open loop the measured position differs from the command by the
        piezo's hysteresis, so adopting the setpoint (not the read-out) is what
        keeps the brain's target equal to what the controller is holding.
        """
        return self._query_value("query_setpoint", axis)

    # -- native slew rate -------------------------------------------------- #
    def set_slew_rate(self, axis: int, rate: float) -> None:
        wire = self._slew_to_wire(rate)
        self._write(_CMD["set_slew_rate"].format(ch=self._channels[axis], value=wire))
        self._slew[axis] = float(rate)

    def read_slew_rate(self, axis: int) -> float:
        """Ask the controller for its slew-rate limit, in um/s.  # VERIFY"""
        wire = self._query_value("query_slew_rate", axis)
        self._slew[axis] = self._slew_from_wire(wire)
        return self._slew[axis]
