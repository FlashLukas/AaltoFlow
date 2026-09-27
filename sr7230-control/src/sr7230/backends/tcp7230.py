"""The real Model 7230, over Ethernet (TCP). Implements the LockInBackend interface.

Source for every command below: "Model 7230 DSP Lock-in Amplifier Instruction
Manual", 198004-A-MNL-D (firmware 2.20 or later), chapter 6:
  6.5.04/6.5.05  sockets 50000/50001, terminators, status + overload bytes
  6.6            command format; the floating-point form ends the name in "."
  6.6.01         IMODE, VMODE, FLOAT, FET, DCCOUPLE, SEN (Table 6-2), AS, ASM,
                 AUTOMATIC, LF
  6.6.02         REFMODE, IE, REFN, REFP., AQN, FRQ.
  6.6.03/04      TC (Table 6-4), TC., SLOPE, FASTMODE
  6.6.05         X., Y., XY., MAG., PHA., MP.
  6.6.06         OA., OF.
  6.6.09         ADC. n
  6.6.11/12      ST, N, ID, VER

No vendor library: Signal Recovery's command set is plain ASCII over a socket,
so the standard library is enough. (It is NOT SCPI -- no "*IDN?", no colons.)

THE WIRE (port 50000), as the manual describes it:
    computer -> instrument:  b"TC 12\\x00"                 command + NUL
    instrument -> computer:  b"<reply text>\\x00<ST><N>"   reply, NUL, then TWO
                                                          raw bytes: the status
                                                          byte and the overload
                                                          byte (Table 6-1)
A command with no reply still returns b"\\x00<ST><N>", which is how we know it
was carried out. So every exchange here is: send, read to the NUL, read two
more bytes. Status bit 1 (invalid command) or 2 (parameter error) turns into
an exception, so a typo cannot pass silently.

Why port 50000 and not 50001 (which ends replies in CR instead): the two
trailing bytes give the overload and reference-unlock state with EVERY reply
-- for free, with no extra ST / N round trip per poll.

RS-232 / USB are the documented alternatives. RS-232 differs in framing (CR
terminators, a byte-by-byte echo handshake, a '*' or '?' prompt) and is NOT
implemented here; `hardware.interface = "serial"` refuses with a message. It
would be a second `_Transport` with the same `query()`.

Everything not yet seen working against a real 7230 is marked `# VERIFY`.
First hardware session:
  1. Give the 7230 an IP address (manual 5.2), open it in a browser to check.
  2. hardware.host = that address (Settings, or `--address` on run_service).
  3. `uv run scripts/run_service.py --real --address <ip>`, then drive it from
     `sr7230_console.py` and compare every value with the web panel.
"""

from __future__ import annotations

import math
import re
import socket
import threading

# a number as the 7230 prints it: "+1.234E-03", "12", "-5.0E+00"
_NUM = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")

#: status-byte bits (manual Table 6-1)
ST_COMPLETE, ST_INVALID, ST_PARAM, ST_UNLOCK, ST_OVERLOAD, ST_ADC, ST_INPUT_OVL, ST_DATA = \
    (1 << k for k in range(8))


class InstrumentError(RuntimeError):
    """The 7230 flagged a command as invalid or out of range."""


class _TcpTransport:
    """One TCP connection to port 50000, framing as in manual 6.5.05."""

    def __init__(self, host: str, port: int, timeout_s: float):
        self.host, self.port, self.timeout_s = host, int(port), float(timeout_s)
        self._sock: socket.socket | None = None
        self._buf = b""

    def open(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        # small commands, answered at once: do not let Nagle hold them back
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buf = b""

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def query(self, cmd: str, timeout_s: float | None = None) -> tuple[str, int, int]:
        """Send one command, return (reply text, status byte, overload byte)."""
        if self._sock is None:
            raise ConnectionError("not connected")
        self._sock.settimeout(timeout_s or self.timeout_s)
        self._sock.sendall(cmd.encode("ascii") + b"\x00")
        # read until the NUL, then exactly two more bytes (status, overload).
        # Those two may themselves be 0x00, so they are counted, not searched.
        while True:
            nul = self._buf.find(b"\x00")
            if nul >= 0 and len(self._buf) >= nul + 3:
                break
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("the 7230 closed the connection")
            self._buf += chunk
        text = self._buf[:nul].decode("ascii", errors="replace").strip()
        status, overload = self._buf[nul + 1], self._buf[nul + 2]
        self._buf = self._buf[nul + 3:]
        return text, status, overload


class Tcp7230:
    """Drives a physical Model 7230 over Ethernet."""

    def __init__(self, host: str, port: int = 50000, timeout_s: float = 2.0,
                 interface: str = "tcp", transport=None):
        if interface != "tcp":
            raise ValueError(f"interface {interface!r} is not implemented yet; "
                             f"use 'tcp' (Ethernet, port 50000)")
        if transport is None and not host:
            raise ValueError("no IP address for the 7230: set hardware.host in the "
                             "config, or pass --address to run_service.py")
        self._t = transport or _TcpTransport(host, port, timeout_s)
        self._idn = ""
        self._last_status = 0
        self._last_overload = 0
        # The instrument handles one command at a time; the brain already
        # serialises calls, this lock makes the backend safe on its own too.
        self._io = threading.RLock()

    # ---- the exchange ------------------------------------------------------------

    def _q(self, cmd: str, timeout_s: float | None = None) -> str:
        with self._io:
            text, st, ovl = self._t.query(cmd, timeout_s)
        self._last_status, self._last_overload = st, ovl
        # VERIFY: that bits 1/2 describe THIS command and are cleared by the
        # next one (the manual implies it; if they are sticky until ST is
        # read, every later command would raise too).
        if st & (ST_INVALID | ST_PARAM):
            what = "invalid command" if st & ST_INVALID else "parameter out of range"
            raise InstrumentError(f"7230 refused {cmd!r}: {what} (status byte {st})")
        return text

    def _float(self, cmd: str) -> float:
        nums = _NUM.findall(self._q(cmd))
        if not nums:
            raise InstrumentError(f"7230 sent no number for {cmd!r}")
        return float(nums[0])

    def _floats(self, cmd: str, n: int) -> list[float]:
        # XY. / MP. answer "x<delimiter>y"; the delimiter is a comma by default
        # but settable (DD), so split on anything that is not part of a number.
        nums = [float(v) for v in _NUM.findall(self._q(cmd))]
        if len(nums) < n:
            raise InstrumentError(f"7230 sent {len(nums)} numbers for {cmd!r}, expected {n}")
        return nums[:n]

    # ---- lifecycle ------------------------------------------------------------------

    def open(self) -> None:
        self._t.open()
        ident = self._q("ID")                   # answers 7230
        ver = self._q("VER")                    # VERIFY: reply format of VER
        if "7230" not in ident:
            self._t.close()
            raise InstrumentError(f"not a 7230: ID answered {ident!r}")
        self._idn = f"Signal Recovery {ident} firmware {ver}".strip()
        # This module speaks single-reference mode only; the dual modes change
        # the command set (manual 6.6.14).
        self._q("REFMODE 0")                    # 0 = single reference (6.6.02)
        # Deliberately NOT touched: OA (oscillator amplitude). The brain decides
        # what OSC OUT does; opening a connection must never raise it.

    def close(self) -> None:
        self._t.close()

    def idn(self) -> str:
        return self._idn

    # ---- reference + oscillator --------------------------------------------------------

    def set_ref_source(self, index: int) -> None:
        self._q(f"IE {int(index)}")

    def set_osc_frequency(self, hz: float) -> None:
        self._q(f"OF. {float(hz):.6E}")         # VERIFY: accepts exponent form (manual says yes)

    def set_osc_amplitude(self, volts_rms: float) -> None:
        self._q(f"OA. {float(volts_rms):.6E}")

    def set_phase(self, deg: float) -> None:
        self._q(f"REFP. {float(deg):.4f}")

    def get_phase(self) -> float:
        return self._float("REFP.")

    def set_harmonic(self, n: int) -> None:
        self._q(f"REFN {int(n)}")

    # ---- signal channel --------------------------------------------------------------------

    def set_input(self, imode: int, vmode: int) -> None:
        # IMODE takes precedence; VMODE is only meaningful with IMODE 0, but it
        # is sent anyway so switching back to voltage mode lands where we think.
        self._q(f"IMODE {int(imode)}")
        self._q(f"VMODE {int(vmode)}")

    def set_coupling(self, dc: bool) -> None:
        self._q(f"DCCOUPLE {1 if dc else 0}")

    def set_fet(self, fet: bool) -> None:
        self._q(f"FET {1 if fet else 0}")

    def set_float(self, floating: bool) -> None:
        self._q(f"FLOAT {1 if floating else 0}")

    def set_line_filter(self, mode: int, fifty_hz: bool) -> None:
        self._q(f"LF {int(mode)} {1 if fifty_hz else 0}")

    def set_auto_ac_gain(self, on: bool) -> None:
        self._q(f"AUTOMATIC {1 if on else 0}")

    def set_sensitivity_index(self, index: int) -> None:
        self._q(f"SEN {int(index)}")

    def get_sensitivity_index(self) -> int:
        return int(self._float("SEN"))

    # ---- output filter ------------------------------------------------------------------------

    def set_fast_mode(self, on: bool) -> None:
        self._q(f"FASTMODE {1 if on else 0}")

    def set_tc_index(self, index: int) -> None:
        self._q(f"TC {int(index)}")

    def get_time_constant(self) -> float:
        return self._float("TC.")

    def set_slope_index(self, index: int) -> None:
        self._q(f"SLOPE {int(index)}")

    # ---- data --------------------------------------------------------------------------------------

    def read_outputs(self, read_adc: bool = True) -> dict:
        # X and Y in ONE command, so they belong to the same instant.
        # VERIFY the sign of Y (hence theta) against the web panel: PHASEPOL
        # (manual 6.6.05) flips the phase convention between firmware
        # generations, and the simulator assumes theta = atan2(Y, X) with a
        # positive phase for a signal that LEADS the reference.
        x, y = self._floats("XY.", 2)
        st_xy, ovl_xy = self._last_status, self._last_overload
        freq = self._float("FRQ.")              # 0 when an external reference is unlocked
        adc = [math.nan, math.nan]
        if read_adc:
            adc = [self._float("ADC. 1"), self._float("ADC. 2")]   # VERIFY: space before n
        # the status/overload bytes that came with the XY. reply describe the
        # outputs we just read; OR in the later ones so nothing is missed
        return {"x": x, "y": y, "freq_Hz": freq, "adc": adc,
                "status": st_xy | self._last_status,
                "overload": ovl_xy | self._last_overload}

    # ---- automatic operations -------------------------------------------------------------------------
    # The manual says every command returns its terminator once it has been
    # carried out, so these block until the operation is over. How long that
    # is depends on the time constant (auto-sensitivity waits for the output
    # to settle at each range it tries), hence the long timeout. # VERIFY the
    # duration on the instrument with a long time constant.

    def auto_phase(self) -> None:
        self._q("AQN", timeout_s=60.0)

    def auto_sensitivity(self) -> None:
        self._q("AS", timeout_s=120.0)

    def auto_measure(self) -> None:
        # The manual's example (6.7.02) says ASM changes the time constant; the
        # brain reads TC. back afterwards.
        # VERIFY: whether ASM also changes the slope or fast mode (there is no
        # readback of those here yet).
        self._q("ASM", timeout_s=180.0)
