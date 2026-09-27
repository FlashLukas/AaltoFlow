"""The REAL backend: Thorlabs Elliptec mounts (ELL14) over a virtual COM port.

This is the ONLY file in the package that touches the hardware library
(pyserial), and it imports it lazily inside ``open()`` -- so the package still
imports, and the simulator still runs, on a PC without pyserial.  pyserial is in
the optional extra ``real``:  ``.\\dev.ps1 sync --extra gui --extra real``.
No Thorlabs DLL, no Kinesis: the ELL14K's interface board is an FTDI USB-serial
chip, and the mounts speak a short ASCII protocol.

Sources used (each call below that was not confirmed against them is marked
``# VERIFY``):
  * Thorlabs, "Elliptec ELLx OEM/Bare modules protocol manual", Issue 10 --
    packet format, the command set (in, gs, gp, ma, mr, ho, sv, gv, st) and the
    status codes.
  * the open-source ``elliptec`` package (github.com/roesel/elliptec): the
    field slicing of the ``IN`` reply, "angle = pulses / pulses_per_rev * range",
    and the signed 32-bit hex decoding of positions.
  * the ``thorlabs-elliptec`` package (gitlab.com/ptapping/thorlabs-elliptec):
    9600 8N1, replies end in CR LF, a move is complete when the mount answers
    with ``PO`` (or with ``GS`` on an error), ``ho0``/``ho1`` for the direction,
    ``ma`` data = ``f"{counts & 0xffffffff:08X}"``.

The protocol in one paragraph: the host sends ``<address><2 lower-case
letters><data>``, e.g. ``0ma00004600``; the address is one hex digit, data is
upper-case hex.  The mount answers ``<address><2 UPPER-case letters><data>\\r\\n``,
e.g. ``0PO00004600``.  Every mount on the bus hears every packet, and only the
one whose address matches answers, so a reply is matched to its mount by its
first character.  A MOVE is answered only when it has FINISHED (``PO`` with the
final position) or failed (``GS`` with an error code).  That is why this driver
never sends ``gp`` to a mount that is moving: its ``PO`` answer could not be told
apart from the move-complete reply.

Threading: the brain calls this object from ONE worker thread only, so there
is no lock here.  ``poll`` never blocks for longer than a ``gp`` round trip.
"""

from __future__ import annotations

import time

from ..config import Config
from .base import AxisReading


def encode_s32(value: int) -> str:
    """int -> 8 upper-case hex digits, two's complement (negative relative moves)."""
    return f"{int(value) & 0xFFFFFFFF:08X}"


def decode_s32(text: str) -> int:
    """8 hex digits -> signed int (the inverse of :func:`encode_s32`)."""
    v = int(text, 16) & 0xFFFFFFFF
    return -(v & 0x80000000) | (v & 0x7FFFFFFF)


def parse_in_reply(data: str) -> dict:
    """Decode the data part of an ``IN`` reply (everything after "<a>IN").

    Layout (protocol manual, "in" command; slicing as in the ``elliptec``
    package): motor type 2 hex | serial number 8 | year 4 | firmware 2 |
    thread/hardware 2 | travel 4 hex | pulses per unit 8 hex.
    For a rotary mount "travel" is 360 (degrees) and "pulses per unit" is the
    count for that whole travel, i.e. pulses per revolution.        # VERIFY
    """
    return {
        "model": f"ELL{int(data[0:2], 16)}",
        "serial": data[2:10],
        "year": data[10:14],
        "firmware": data[14:16],
        "hardware": data[16:18],
        "travel_deg": int(data[18:22], 16),
        "pulses_per_rev": int(data[22:30], 16),
    }


class _Axis:
    def __init__(self):
        self.pulses_per_rev = 0
        self.travel = 360.0
        self.pos_deg = float("nan")
        self.moving = False
        self.deadline = 0.0
        self.error = 0
        self.velocity = 100
        self.pending_velocity = None
        self.last_gp = 0.0
        self.info = {}


class EllSerialBus:
    """One serial port, several Elliptec mounts on it."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ser = None
        self._buf = b""
        self._axes: dict[str, _Axis] = {}

    # ------------------------------------------------------------------ #
    # connection
    # ------------------------------------------------------------------ #
    def open(self, addresses: list) -> None:
        try:
            import serial  # lazy: only the real path needs pyserial
        except ImportError as exc:  # pragma: no cover - depends on the PC
            raise RuntimeError(
                "pyserial is not installed: run  .\\dev.ps1 sync --extra gui --extra real"
            ) from exc
        hw = self.cfg.hardware
        self._ser = serial.Serial(
            port=hw.port, baudrate=int(hw.baudrate), bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=float(hw.read_timeout_s), write_timeout=1.0,
        )
        self._ser.reset_input_buffer()
        self._axes = {}
        for a in addresses:
            ax = _Axis()
            self._axes[a] = ax
            # "in": identify the mount and learn its pulses per revolution --
            # from the mount itself, not from a constant typed in here.
            data = self._transact(a, "in", "", ("IN",), timeout=1.0)   # VERIFY reply length
            ax.info = parse_in_reply(data)
            ax.info["address"] = a
            override = int(hw.pulses_per_rev_override)
            ax.pulses_per_rev = override if override > 0 else ax.info["pulses_per_rev"]
            if ax.pulses_per_rev <= 0:
                raise RuntimeError(f"mount {a} reported 0 pulses per revolution")
            ax.travel = float(ax.info["travel_deg"] or 360)
            ax.info["pulses_per_rev"] = ax.pulses_per_rev
            try:
                ax.velocity = int(self._transact(a, "gv", "", ("GV",)), 16)   # VERIFY
            except Exception:
                ax.velocity = 100
            self._read_position(a)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def idn(self) -> str:
        models = ", ".join(f"{a}:{ax.info.get('model', '?')}" for a, ax in self._axes.items())
        return f"Thorlabs Elliptec on {self.cfg.hardware.port} ({models})"

    def device_info(self, address: str) -> dict:
        return dict(self._axis(address).info)

    # ------------------------------------------------------------------ #
    # low level: packets and replies
    # ------------------------------------------------------------------ #
    def _axis(self, address: str) -> _Axis:
        ax = self._axes.get(address)
        if ax is None:
            raise RuntimeError(f"address {address} is not open")
        return ax

    def _write(self, address: str, cmd: str, data: str = "") -> None:
        # No terminator: the mount knows each command's length.         # VERIFY
        self._ser.write(f"{address}{cmd}{data}".encode("ascii"))

    def _read_line(self):
        """One reply line without CR LF, or None if nothing complete arrived
        within the serial timeout."""
        while b"\n" not in self._buf:
            chunk = self._ser.read(max(1, self._ser.in_waiting))
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("ascii", "replace").strip()

    def _handle(self, line: str):
        """Dispatch one reply to its mount; return (address, code, data)."""
        if len(line) < 3:
            return None
        a, code, data = line[0].upper(), line[1:3].upper(), line[3:]
        ax = self._axes.get(a)
        if ax is None:
            return a, code, data          # a device we do not drive: ignore
        if code in ("PO", "HO", "BO"):     # position (a move ends with PO)
            try:
                ax.pos_deg = decode_s32(data[:8]) / ax.pulses_per_rev * ax.travel
            except ValueError:
                pass
            ax.moving = False
        elif code == "GS":
            try:
                status = int(data[:2], 16)
            except ValueError:
                status = 3
            if status == 9:               # busy: still moving
                pass
            else:
                ax.error = status
                ax.moving = False          # a move answered with GS has ended  # VERIFY GS00 after st
        return a, code, data

    def _pump(self) -> None:
        """Handle every reply that is already waiting, without blocking."""
        while self._ser is not None and (self._ser.in_waiting or b"\n" in self._buf):
            line = self._read_line()
            if line is None:
                break
            self._handle(line)

    def _transact(self, address: str, cmd: str, data: str, expect: tuple,
                  timeout: float = 0.5) -> str:
        """Send one command and wait for ITS reply (other replies are handled
        on the way, so a move finishing on another mount is not lost)."""
        self._pump()
        self._write(address, cmd, data)
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            line = self._read_line()
            if line is None:
                continue
            got = self._handle(line)
            if got and got[0] == address.upper():
                if got[1] in expect:
                    return got[2]
                if got[1] == "GS" and got[2][:2] not in ("00", "09"):
                    raise RuntimeError(f"mount {address}: '{cmd}' answered GS{got[2][:2]}")
        raise TimeoutError(f"mount {address}: no reply to '{cmd}' within {timeout} s")

    def _read_position(self, address: str) -> None:
        self._transact(address, "gp", "", ("PO",))                        # VERIFY
        self._axis(address).last_gp = time.monotonic()

    # ------------------------------------------------------------------ #
    # motion (fire-and-forget)
    # ------------------------------------------------------------------ #
    def _deg_to_pulses(self, ax: _Axis, deg: float) -> int:
        return int(round(deg / ax.travel * ax.pulses_per_rev))

    def _start(self, address: str, cmd: str, data: str) -> None:
        ax = self._axis(address)
        self._pump()
        self._write(address, cmd, data)
        ax.moving = True
        ax.error = 0
        ax.deadline = time.monotonic() + float(self.cfg.hardware.move_timeout_s)

    def start_move_abs(self, address: str, device_deg: float) -> None:
        ax = self._axis(address)
        # "ma" + signed 32-bit pulses; the mount answers PO when it arrives.
        self._start(address, "ma", encode_s32(self._deg_to_pulses(ax, device_deg)))  # VERIFY

    def start_move_rel(self, address: str, delta_deg: float) -> None:
        ax = self._axis(address)
        self._start(address, "mr", encode_s32(self._deg_to_pulses(ax, delta_deg)))   # VERIFY

    def start_home(self, address: str, ccw: bool = False) -> None:
        # Rotary mounts take a direction: ho0 = clockwise, ho1 = counter-clockwise.
        self._start(address, "ho", "1" if ccw else "0")                            # VERIFY

    def stop(self, address: str) -> None:
        # "st" halts the ELL14's motion; the mount then reports GS/PO, which
        # _handle turns into "not moving".                               # VERIFY reply
        self._axis(address)
        self._write(address, "st")

    def poll(self, address: str) -> AxisReading:
        ax = self._axis(address)
        self._pump()
        now = time.monotonic()
        if ax.moving and now > ax.deadline:
            # No completion reply: give up on this move, report a mechanical
            # timeout, and ask where the mount actually is.
            ax.moving = False
            ax.error = 2
        if not ax.moving:
            if ax.pending_velocity is not None:
                v, ax.pending_velocity = ax.pending_velocity, None
                self._set_velocity_now(address, v)
            if now - ax.last_gp > 0.2:     # refresh the angle at ~5 Hz when idle
                try:
                    self._read_position(address)
                except TimeoutError:
                    ax.error = 1           # communication timeout
        return AxisReading(ax.pos_deg, ax.moving, ax.error)

    # ------------------------------------------------------------------ #
    # parameters
    # ------------------------------------------------------------------ #
    def _set_velocity_now(self, address: str, percent: int) -> None:
        # "sv" + 2 hex digits, percent of the maximum speed.              # VERIFY range
        data = self._transact(address, "sv", f"{int(percent) & 0xFF:02X}", ("GS",))
        # The answer is a status: GS00 = accepted.  Anything else (e.g. GS04
        # "value out of range") means the mount kept its old speed, so do not
        # report the new one as if it had been taken.
        try:
            code = int(data[:2], 16)
        except ValueError:
            code = 3
        if code not in (0, 9):
            raise RuntimeError(f"mount {address}: sv{int(percent):02X} answered GS{data[:2]}")
        self._axis(address).velocity = int(percent)

    def set_velocity(self, address: str, percent: int) -> None:
        ax = self._axis(address)
        if ax.moving:
            # The GS answer to "sv" would look like the end of the move, so the
            # new speed is applied as soon as this move has finished.
            ax.pending_velocity = int(percent)
            ax.velocity = int(percent)
            return
        self._set_velocity_now(address, percent)

    def read_velocity(self, address: str) -> int:
        return self._axis(address).velocity
