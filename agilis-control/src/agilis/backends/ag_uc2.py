"""The REAL backend: a Newport AG-UC2 over its USB virtual COM port.

The ONLY file in the package that talks to hardware. It imports ``pyserial``
lazily inside :meth:`AgUC2.open`, so the package (and the simulator) import on
a PC without it. Install it with ``uv sync --extra gui --extra real``.

Source for every command below: Newport "Agilis Series Piezo Motor Driven
Components User's Manual" A824E (rev. 03/26), section 4.5 (USB communication)
and 4.6 (ASCII command set: syntax, JA, ML, MR, PH, PR, RS, ST, SU, TE, TP, TS,
VE, ZP) and section 5.7 (CC, AG-UC8 only).

What the manual says, and what it leaves open:

* Link: 921600 baud, 8N1, no flow control, CR LF terminator (4.5). The AG-UC2
  enumerates as a virtual COM port through its own USB driver. # VERIFY the
  port name and that the Newport driver is installed on the lab PC.
* A command is ``<axis><two letters><value>``; blanks are ignored; one command
  per line; the controller executes on CR LF (4.6.1).
* SET commands send NO reply. An error is only visible through ``TE`` (error
  code of the PREVIOUS command). So every setter here is "send, then TE?" --
  the manual itself recommends that for a safe program flow.
* At power-up the controller is in LOCAL mode (buttons active, set commands
  refused with -5). ``MR`` switches to remote; ``ML`` hands the buttons back.
* Query replies echo the command, e.g. ``1TP`` -> ``1TP1234``. The exact echo
  format (axis prefix present? sign on SU replies?) is not printed in the manual,
  so replies are parsed by taking the LAST signed integer in the line.
  # VERIFY each reply format on the controller.
* ``PA``/``MA`` (absolute positioning) exist only for stages WITH a limit
  switch (AG-LS25) and interrupt the USB link for up to 2 minutes; both reply
  only when they are done, so :meth:`measure_position` / :meth:`move_absolute`
  wait for that reply with ``hardware.limit_op_timeout_s``. ``MV`` (move to
  limit) is an ordinary fire-and-forget command.
* The AG-UC2 has no step-delay (``DL``) and no ``CC``; ``CC`` is sent only when
  ``hardware.channel`` > 0 (for an AG-UC8).

START-UP WRITES (AaltoFlow rule 2026-09-27: read the state, change nothing).
:meth:`open` sends only ``VE`` (a query). The manual lets TP, SU?, PH and JA?
run ONLY in remote mode (-5 otherwise; TS and VE are the exceptions), so the
brain must call :meth:`enable_remote` (``MR``) to read the counters and
amplitudes at all. MR disables the push buttons and moves nothing; it is the
one write the start-up needs. ``SU`` is NOT written: the amplitudes are read
back and adopted.
"""

from __future__ import annotations

import re
import threading

from ..config import Config

_INT = re.compile(r"[-+]?\d+")

#: TE error codes (manual, TE command).
TE_CODES = {
    -1: "unknown command",
    -2: "axis out of range (must be 1 or 2, or must not be specified)",
    -3: "wrong format for parameter (or must not be specified)",
    -4: "parameter out of range",
    -5: "not allowed in local mode",
    -6: "not allowed in current state",
}


def _last_int(reply: str, what: str) -> int:
    """The last signed integer in a reply line (see the VERIFY note above)."""
    nums = _INT.findall(reply or "")
    if not nums:
        raise RuntimeError(f"AG-UC2: no number in reply to {what}: {reply!r}")
    return int(nums[-1])


class AgUC2:
    """AG-UC2 controller, two Agilis actuators on axes 1 and 2."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ser = None
        # One command = write + (maybe) read; two threads interleaving would
        # read each other's replies. The brain serialises too; this is the
        # backstop that makes the driver safe on its own.
        self._lock = threading.RLock()
        self._version = ""
        # True once WE switched the controller to remote mode: close() hands
        # the buttons back (ML) and stops the axes only if we took them.
        self._remote_by_us = False

    # -- low level --------------------------------------------------------- #
    def _write(self, cmd: str) -> None:
        self._ser.write((cmd + "\r\n").encode("ascii"))

    def _readline(self) -> str:
        raw = self._ser.readline()                       # up to LF or timeout
        if not raw:
            raise TimeoutError("AG-UC2 did not answer (timeout)")
        return raw.decode("ascii", errors="replace").strip()

    def query(self, cmd: str) -> str:
        """Send a query and return its one-line reply."""
        with self._lock:
            self._ser.reset_input_buffer()               # drop any stale line
            self._write(cmd)
            return self._readline()

    def command(self, cmd: str) -> None:
        """Send a set command, then ask TE whether it was accepted."""
        with self._lock:
            self._ser.reset_input_buffer()
            self._write(cmd)
            self._write("TE")                            # VERIFY: reply "TE0" / "TE-6"
            code = _last_int(self._readline(), "TE")
        if code != 0:
            raise RuntimeError(f"AG-UC2 refused {cmd!r}: error {code} "
                               f"({TE_CODES.get(code, 'unknown error code')})")

    # -- connection -------------------------------------------------------- #
    def open(self) -> None:
        try:
            import serial  # pyserial -- lazy, so the package imports without it
        except ImportError as exc:
            raise RuntimeError("pyserial is not installed: uv sync --extra gui --extra real "
                               "(docs/DEVELOPER_NOTES.md gotcha #29)") from exc
        hw = self.cfg.hardware
        if not hw.port:
            raise RuntimeError("hardware.port is empty: set the AG-UC2's COM port "
                               "(Device Manager > Ports) in Settings or the .ini")
        self._ser = serial.Serial(
            port=hw.port, baudrate=int(hw.baud),                 # VERIFY 921600 works
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, xonxoff=False, rtscts=False,
            timeout=float(hw.timeout_s), write_timeout=float(hw.timeout_s))
        try:
            # a QUERY: works in local mode and changes nothing
            self._version = self.query("VE")                     # VERIFY reply text
        except Exception:
            # a half-opened controller must not keep the COM port: the next
            # start (or the vendor applet) could not open it until this
            # process exits
            try:
                self._ser.close()
            finally:
                self._ser = None
            raise

    def enable_remote(self) -> None:
        """MR (+ CC on an AG-UC8): the writes needed to READ the controller.

        TP, SU?, PH are refused in local mode (manual 4.6.4), so without MR the
        module could not even see where the stage is. MR moves nothing; it
        disables the push buttons. The manual refuses it (-6) unless both axes
        are READY -- the brain waits for that first.
        """
        self.command("MR")                                       # remote mode
        self._remote_by_us = True
        if int(self.cfg.hardware.channel) > 0:
            self.command(f"CC{int(self.cfg.hardware.channel)}")  # AG-UC8 only  # VERIFY

    def close(self) -> None:
        if self._ser is None:
            return
        try:
            if not self._remote_by_us:
                return      # we never took the controller: leave it as we found it
            for ax in (1, 2):
                try:
                    self.command(f"{ax}ST")
                except Exception:
                    pass
            if self.cfg.hardware.local_on_close:
                try:
                    self.command("ML")                           # buttons back to the user
                except Exception:
                    pass
        finally:
            try:
                self._ser.close()
            finally:
                self._ser = None
                self._remote_by_us = False

    def idn(self) -> str:
        return f"Newport {self._version or 'AG-UC2'} on {self.cfg.hardware.port}"

    # -- motion ------------------------------------------------------------ #
    def move_by(self, hw_axis: int, delta_steps: int) -> None:
        self.command(f"{int(hw_axis)}PR{int(delta_steps)}")      # VERIFY

    def jog(self, hw_axis: int, mode: int) -> None:
        self.command(f"{int(hw_axis)}JA{int(mode)}")             # VERIFY

    def stop(self, hw_axis: int) -> None:
        self.command(f"{int(hw_axis)}ST")                        # VERIFY

    def read_position(self, hw_axis: int) -> int:
        return _last_int(self.query(f"{int(hw_axis)}TP"), "TP")  # VERIFY "1TP1234"

    def axis_state(self, hw_axis: int) -> int:
        return _last_int(self.query(f"{int(hw_axis)}TS"), "TS")  # VERIFY "1TS0"

    def zero_counter(self, hw_axis: int) -> None:
        self.command(f"{int(hw_axis)}ZP")                        # VERIFY

    # -- drive ------------------------------------------------------------- #
    def set_amplitude(self, hw_axis: int, direction: int, amplitude: int) -> None:
        a = abs(int(amplitude))
        self.command(f"{int(hw_axis)}SU{a if direction > 0 else -a}")   # VERIFY sign convention

    def read_amplitude(self, hw_axis: int, direction: int) -> int:
        sign = "+" if direction > 0 else "-"
        # reply may carry the sign of the direction ("1SU-16"): take the size
        return abs(_last_int(self.query(f"{int(hw_axis)}SU{sign}?"), "SU"))  # VERIFY

    def limit_status(self) -> int:
        return _last_int(self.query("PH"), "PH")                 # VERIFY "PH0".."PH3"

    # -- limit-switch stages (AG-LS25): MV, MA, PA ------------------------ #
    def move_to_limit(self, hw_axis: int, mode: int) -> None:
        """MV: jog at speed |mode| towards the limit (sign = way), stop there."""
        self.command(f"{int(hw_axis)}MV{int(mode)}")             # VERIFY

    def _blocking(self, cmd: str, what: str) -> int:
        """Send MA/PA and wait for THE reply (the USB link is down meanwhile).

        A refused command replies nothing at all, which from here looks exactly
        like a long measurement -- so the brain only sends these on a READY
        axis in remote mode, and a silent timeout is followed by TE to learn
        why. # VERIFY the reply text ("1MA523"? "1PA500"?) and that the
        controller really answers once the link is back.
        """
        with self._lock:
            self._ser.reset_input_buffer()
            old = self._ser.timeout
            self._ser.timeout = float(self.cfg.hardware.limit_op_timeout_s)
            try:
                self._write(cmd)
                raw = self._ser.readline()
            finally:
                self._ser.timeout = old
            if not raw:
                self._write("TE")
                code = _last_int(self._readline(), "TE")
                raise RuntimeError(f"AG-UC2 gave no answer to {cmd!r} (error {code}: "
                                   f"{TE_CODES.get(code, 'no error reported - timed out')})")
            return _last_int(raw.decode("ascii", errors="replace").strip(), what)

    def measure_position(self, hw_axis: int) -> int:
        """MA: distance to the limit in 1/1000 of the travel (0..1000)."""
        return self._blocking(f"{int(hw_axis)}MA", "MA")

    def move_absolute(self, hw_axis: int, permille: int) -> int:
        """PA: go to permille/1000 of the travel; returns the reached target."""
        return self._blocking(f"{int(hw_axis)}PA{int(permille)}", "PA")
