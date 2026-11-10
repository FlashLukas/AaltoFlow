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

REPLIES CAN FALL ONE BEHIND -- HOW THIS DRIVER TALKS (Blue Box rig, 2026-11-10,
AG-UC2 v2.2.1 on its Newport USB serial driver). Measured there:
  * the controller lets out AT MOST ONE reply line per incoming USB write;
  * a reply that is not ready when its command's write arrives (e.g. while an
    axis steps), or two commands that reach it in one USB packet, leave a line
    HELD; from then on every answer is one behind ("VE" answered "TE0", "1TP"
    answered the VE) until a power-cycle -- the moves still run;
  * two lines written back to back can be merged into one packet by the USB
    driver: "2TS\r\nVE\r\n" in one write gives ONE reply and holds the other,
    so a backlog GROWS; the same two 20 ms apart keep it constant.
Newport's own library (CmdLibAgilis.dll, under the LabVIEW program that runs
this rig fine) sends one command per write, waits for its answer with a read
timeout, sleeps between commands, and opens the port with DTR and RTS on. So:
  1. ONE command per write, and never two writes closer than MIN_GAP_S;
  2. a query waits for ITS answer -- the line starting with what it asked
     ("1TP" for 1TP, "TE" for TE, "AG-UC" for VE); any other line is a late
     answer to something earlier and is dropped (`late_lines` counts them);
  3. only if nothing has come for PUSH_AFTER_S does a VE go out, in its own
     write, to let a held line out -- at most MAX_PUSHES times. In step that
     never happens (answers take 2-17 ms); one behind, it costs one extra
     write and keeps the backlog from growing;
  4. a set command (no reply) is followed by SET_PAUSE_S, then TE;
  5. the brain polls at 5 Hz (hardware.poll_hz), not 20.
(First attempt the same morning: a VE after EVERY command, written right
behind it. Merged into one packet, it grew the backlog on every exchange.)

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

from .. import hwlock
from ..config import Config

_INT = re.compile(r"[-+]?\d+")

#: The query that lets a held reply line out (sent only after PUSH_AFTER_S
#: of silence, in its own write -- see "REPLIES CAN FALL ONE BEHIND").
PUSHER = "VE"
#: Never two writes closer than this: two lines in one USB packet get one
#: answer and the other is held (Blue Box: 20 ms apart kept them separate).
MIN_GAP_S = 0.02
#: Silence after a query before a pusher goes out. An answer in step takes
#: 2-17 ms on the rig.
PUSH_AFTER_S = 0.06
#: At most this many pushers per question (one per line the controller is behind).
MAX_PUSHES = 3
#: After a set command (no reply), before asking TE about it.
SET_PAUSE_S = 0.03
#: How a VE reply starts ("AG-UC2 v2.2.1 " on the rig; an AG-UC8 says AG-UC8).
VE_PREFIX = "AG-UC"
_CODE = re.compile(r"^(\d?[A-Z]{2})")


def reply_prefix(cmd: str) -> str:
    """What the reply to `cmd` starts with: the axis + two letters
    ("1TP" -> "1TP", "1SU+?" -> "1SU", "PH" -> "PH", "2MA" -> "2MA"),
    or the version text for VE."""
    cmd = cmd.strip().upper()
    if cmd == "VE":
        return VE_PREFIX
    m = _CODE.match(cmd)
    if not m:
        raise ValueError(f"AG-UC2: cannot tell what {cmd!r} answers")
    return m.group(1)

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


class AgUC2OutOfStep(RuntimeError):
    """The controller's answers fell behind and could not be brought back
    (see "REPLIES CAN FALL ONE BEHIND"): it needs a power-cycle."""


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
        # The claim on the COM port (hwlock.py): held from open() to close(),
        # so a second service pointed at the same AG-UC2 is refused before it
        # sends a single byte. None while not open.
        self._hwlock = None
        # Reply lines dropped because they did not answer the question asked
        # (late answers to earlier questions). Diagnostics only.
        self.late_lines = 0
        self._last_write = 0.0                   # monotonic time of the last write

    # -- low level --------------------------------------------------------- #
    def _write(self, cmd: str) -> None:
        """ONE command in its own USB write, never closer than MIN_GAP_S to
        the previous one (see "REPLIES CAN FALL ONE BEHIND")."""
        import time
        wait = self._last_write + MIN_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._ser.write((cmd + "\r\n").encode("ascii"))
        try:
            self._ser.flush()                    # out now, not merged with the next one
        except Exception:
            pass
        self._last_write = time.monotonic()

    def _read_for(self, seconds: float) -> str:
        """One line, waiting at most `seconds` ("" = nothing came)."""
        self._ser.timeout = max(0.001, float(seconds))
        raw = self._ser.readline()               # up to LF or timeout
        return raw.decode("ascii", errors="replace").strip() if raw else ""

    def _expect(self, prefix: str, what: str) -> str:
        """The reply that starts with `prefix`. Other lines are late answers to
        earlier questions and are dropped. After PUSH_AFTER_S of silence a VE
        goes out in its own write to let a held line out (at most MAX_PUSHES);
        the whole wait is bounded by hardware.timeout_s."""
        import time
        want = prefix.upper()
        deadline = time.monotonic() + float(self.cfg.hardware.timeout_s)
        pushes, stale = 0, ""
        old_timeout = self._ser.timeout
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                line = self._read_for(min(PUSH_AFTER_S, left) if pushes < MAX_PUSHES else left)
                if line.upper().startswith(want):
                    return line
                if line:
                    self.late_lines += 1          # an answer to something earlier
                    stale = line
                    continue
                if pushes < MAX_PUSHES and deadline - time.monotonic() > 0:
                    self._write(PUSHER)
                    pushes += 1
        finally:
            self._ser.timeout = old_timeout
        # Lines came, but none answered the question: out of step. Nothing at
        # all, not even to a release query: the controller is not answering.
        if stale:
            raise AgUC2OutOfStep(
                f"AG-UC2 out of step: no answer to {what}" +
                (f" (last line {stale!r})" if stale else "") +
                f" after {pushes} release attempt(s) -- power-cycle the controller")
        raise TimeoutError(f"AG-UC2 did not answer {what} (timeout)")

    def query(self, cmd: str) -> str:
        """Send a query and return ITS reply."""
        with self._lock:
            self._ser.reset_input_buffer()               # lines already out: stale
            self._write(cmd)
            return self._expect(reply_prefix(cmd), cmd)

    def command(self, cmd: str) -> None:
        """Send a set command, then ask TE whether it was accepted (set
        commands reply nothing, so only TE tells). A pause first, like
        Newport's library -- and TE as its own write."""
        import time
        with self._lock:
            self._ser.reset_input_buffer()
            self._write(cmd)
            time.sleep(SET_PAUSE_S)
            self._write("TE")                            # reply "TE0" / "TE-6"
            code = _last_int(self._expect("TE", f"TE after {cmd}"), "TE")
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
        # Claim the PHYSICAL address before the first byte goes out (Lukas's
        # rule: one instrument = one physical address = one service). The COM
        # port IS the AG-UC2's identity on this PC; "com5", "COM5" and
        # "ASRL5::INSTR" are the same box (hwlock.normalize). Raises
        # HardwareBusy naming the holder -- then nothing below runs, so a
        # controller owned by another service is never touched.
        self._hwlock = hwlock.claim(hw.port, "agilis")
        try:
            self._ser = serial.Serial(
                port=hw.port, baudrate=int(hw.baud),             # VERIFY 921600 works
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, xonxoff=False, rtscts=False,
                dsrdtr=False,
                timeout=float(hw.timeout_s), write_timeout=float(hw.timeout_s))
            # Newport's library (System.IO.Ports, under the LabVIEW program)
            # sets DtrEnable and RtsEnable: do the same.  # VERIFY it matters
            try:
                self._ser.dtr = True
                self._ser.rts = True
            except Exception:
                pass
        except Exception:
            self._release_claim()      # a failed open must not keep the address "busy"
            raise
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
                self._release_claim()
            raise

    def _release_claim(self) -> None:
        lk, self._hwlock = self._hwlock, None
        if lk is not None:
            lk.release()

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
            self._release_claim()      # nothing open; make sure no claim lingers
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
                # released LAST: until the port is shut, the box is still ours
                self._release_claim()

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
        prefix = reply_prefix(cmd)
        with self._lock:
            self._ser.reset_input_buffer()
            self._write(cmd)
            # The link is down while MA / PA run: wait for THE answer (no
            # pusher into a dead link), dropping any late line from before.
            line = ""
            old = self._ser.timeout
            try:
                import time
                deadline = time.monotonic() + float(self.cfg.hardware.limit_op_timeout_s)
                while time.monotonic() < deadline:
                    text = self._read_for(deadline - time.monotonic())
                    if text.upper().startswith(prefix):
                        line = text
                        break
                    if text:
                        self.late_lines += 1
            finally:
                self._ser.timeout = old
            if not line:
                # Silence: refused (no reply at all) -- or the answer is held
                # back; _expect lets a held line out, then TE says why.
                try:
                    line = self._expect(prefix, cmd)
                except (TimeoutError, AgUC2OutOfStep):
                    line = ""
            if not line:
                time.sleep(SET_PAUSE_S)
                self._write("TE")
                code = _last_int(self._expect("TE", f"TE after {cmd}"), "TE")
                raise RuntimeError(f"AG-UC2 gave no answer to {cmd!r} (error {code}: "
                                   f"{TE_CODES.get(code, 'no error reported - timed out')})")
            return _last_int(line, what)

    def measure_position(self, hw_axis: int) -> int:
        """MA: distance to the limit in 1/1000 of the travel (0..1000)."""
        return self._blocking(f"{int(hw_axis)}MA", "MA")

    def move_absolute(self, hw_axis: int, permille: int) -> int:
        """PA: go to permille/1000 of the travel; returns the reached target."""
        return self._blocking(f"{int(hw_axis)}PA{int(permille)}", "PA")
