"""The real GW Instek GSP-818, over VISA (pyvisa). THE ONLY FILE THAT TOUCHES
THE INSTRUMENT'S LIBRARY -- and it imports pyvisa inside `open()`, so the
package still imports on a PC with no VISA installed.

NEVER RUN ON THE INSTRUMENT YET. Sources used (checked 2026-09-27):
  * GSP-818 Programming Manual (GW Instek, 104 pages; read on manualslib,
    "GW Instek GSP-818 Series Programming Manual"): the command syntax below,
    page numbers in the comments ("PM p.NN").
  * GSP-818 User Manual (GW Instek download), Specifications: USB device =
    "USB TMC", LAN 10/100Base; RBW 10 Hz - 3 MHz, VBW 10 Hz - 3 MHz, reference
    level -80 ... +30 dBm, attenuation 0 - 40 dB in 1 dB steps, sweep time
    10 ms - 3000 s, tracking generator 100 kHz - 1.8 GHz at -30 ... 0 dBm;
    detector "Auto" = Normal above 1 MHz span, Pos Peak at or below.
Every line that the manuals do not settle beyond doubt is marked # VERIFY.

Interface: USBTMC (the data sheet). On Windows a USBTMC device needs a VISA
library with a USB driver -- NI-VISA or Keysight IO Libraries -- so the default
is the installed VISA. pyvisa-py can do USBTMC only with pyusb + libusb, which is
not set up by this module. Over LAN the manual documents the IP settings but
not the protocol or port (# VERIFY: try VXI-11 "TCPIP0::<ip>::inst0::INSTR").

How a trace is read. The manual documents :INITiate:CONTinuous ON|OFF but NO
"start one sweep now" command (no :INIT:IMM, no *OPC? behaviour described).
So there are two modes (`hardware.sweep_mode`):
  "wait"    (default) the instrument sweeps continuously. After a trigger the
            brain waits `settle_sweeps` sweep times (+ margin) and then reads
            TRACE1: the sweep that was running at the trigger is not trusted,
            the one after it is whole. Only documented commands.
  "single"  :INIT:CONT OFF once, then :INIT:IMM for every sweep. The usual
            SCPI way, faster (one sweep per trace), but :INIT:IMM is NOT in the
            GSP-818 manual -- # VERIFY before switching to it.

Trace format (PM p.100): `:TRAC? TRACE1` returns comma-separated ASCII numbers
in the active amplitude unit, which `open()` sets to dBm (:UNIT:POW DBM, PM
p.104). The manual's example reply starts with a ">" -- stripped defensively.
"""

from __future__ import annotations

import time

import numpy as np

from ..config import Config
from ..model import SweepSettings

GW_INSTEK_USB_VID = "0x2184"      # USB vendor id of Good Will Instrument  # VERIFY in NI MAX
GW_INSTEK_USB_VID_DEC = "8580"    # the same id in decimal: some VISAs print it that way

#: the module's detector names -> the SCPI words (PM p.77)
_DETECTOR_SCPI = {"auto": "AUTO", "normal": "NORM", "pos_peak": "POS",
                  "neg_peak": "NEG", "sample": "SAMP"}


def _parse_trace(text: str) -> np.ndarray:
    """'-64.73,-68.16,...' -> float array. Tolerates a leading '>' or '#'
    (the manual's example shows one) and a trailing terminator."""
    body = text.strip().lstrip(">#").strip()
    vals = [v for v in body.replace(";", ",").split(",") if v.strip()]
    return np.array([float(v) for v in vals], dtype=float)


class GspAnalyzer:
    simulated = False

    def __init__(self, cfg: Config, resource=None, sleep=time.sleep):
        """`resource` may be an already-open pyvisa-like object (the tests pass
        a fake instrument); None opens `cfg.hardware.resource` in `open()`."""
        self.cfg = cfg
        self._inst = resource
        self._own = resource is None
        self._rm = None
        self._sleep = sleep
        self._idn = ""
        self._applied: dict = {}          # SCPI header -> value last sent (send only changes)
        self._single_armed = False
        self._pending = False
        self._points = 0
        self._sweep_rb_s = 0.0            # sweep time the instrument REPORTS it uses
        self._extra_wait_s = 0.0          # one-off wait after a settings change ("wait" mode)

    # ---- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        hw = self.cfg.hardware
        if self._inst is None:
            try:
                import pyvisa                      # lazy: only the real path needs it
            except ImportError as exc:             # pragma: no cover - depends on the PC
                raise RuntimeError("pyvisa is not installed: "
                                   "uv sync --extra gui --extra real") from exc
            self._rm = pyvisa.ResourceManager(hw.visa_library) if hw.visa_library \
                else pyvisa.ResourceManager()
            name = hw.resource or self._find_usb(self._rm)
            self._inst = self._rm.open_resource(name)
        inst = self._inst
        try:
            inst.timeout = int(hw.timeout_s * 1000)
            inst.read_termination = "\n"           # PM p.23: LF terminates a message
            inst.write_termination = "\n"
        except Exception:
            pass
        self._idn = self._query("*IDN?").strip()   # PM p.31
        # SAFETY first: the tracking generator drives whatever is on GEN OUTPUT.
        self._write(":OUTP:TRAC OFF")              # PM p.65
        self._write(":UNIT:POW DBM")               # PM p.104: traces in dBm
        # Whatever the front panel left behind must not bend the data: TRACE1
        # in plain "clear/write" mode (a Max Hold or View trace never shows a
        # fresh sweep -- "wait" mode would read a frozen trace forever), and
        # the instrument's own averaging off (the brain averages, in linear
        # power, the same way on the simulator).
        self._write(":TRAC1:MODE WRIT")            # PM p.100
        self._write(":AVER OFF")                   # PM p.69
        self._applied = {}
        self._sweep_rb_s = 0.0
        self._extra_wait_s = 0.0
        self._single_armed = False

    def close(self) -> None:
        inst, self._inst = self._inst, None
        self._pending = False
        if inst is None:
            return
        try:
            inst.write(":OUTP:TRAC OFF")           # never leave the TG driving a DUT
            if self._single_armed:
                inst.write(":INIT:CONT ON")        # hand the front panel back sweeping
        except Exception:
            pass
        if self._own:
            try:
                inst.close()
            except Exception:
                pass
            if self._rm is not None:
                try:
                    self._rm.close()
                except Exception:
                    pass
                self._rm = None

    def idn(self) -> str:
        return self._idn

    # ---- settings --------------------------------------------------------------

    def configure(self, s: SweepSettings) -> dict:
        """Send only what changed, then read back what the instrument uses."""
        sent0 = self._n_sent
        self._set(":FREQ:STAR", f"{s.start_Hz:.0f}")          # PM p.78ff
        self._set(":FREQ:STOP", f"{s.stop_Hz:.0f}")
        self._set(":SWE:POIN", f"{int(s.points)}")             # PM p.92  # VERIFY range
        if s.rbw_auto:                                          # PM p.69-70
            self._set(":BAND:AUTO", "ON")
        else:
            self._set(":BAND:AUTO", "OFF")
            self._set(":BAND", f"{s.rbw_Hz:.0f}")               # VERIFY: snaps to the 1-3 steps?
        if s.vbw_auto:                                          # PM p.71
            self._set(":BAND:VID:AUTO", "ON")
        else:
            self._set(":BAND:VID:AUTO", "OFF")
            self._set(":BAND:VID", f"{s.vbw_Hz:.0f}")
        self._set(":DISP:WIN:TRAC:Y:RLEV", f"{s.ref_level_dBm:.2f}")   # PM p.55, in dBm (unit set in open)
        if s.atten_auto:                                        # PM p.90-91
            self._set(":POW:ATT:AUTO", "ON")
        else:
            self._set(":POW:ATT:AUTO", "OFF")
            self._set(":POW:ATT", f"{int(round(s.atten_dB))}")
        # PM p.91 names the preamp command ...:GAIN[:STATe]:AUTO -- an odd name for
        # an on/off switch, but that is what the manual prints.  # VERIFY
        self._set(":POW:GAIN:AUTO", "ON" if s.preamp else "OFF")
        if s.sweep_time_auto:                                   # PM p.92
            self._set(":SWE:TIME:AUTO", "ON")
        else:
            self._set(":SWE:TIME:AUTO", "OFF")
            # "ms" suffix: the manual says the default unit is ns and the query
            # answers in ms -- a unit spelled out cannot be misread.  # VERIFY
            self._set(":SWE:TIME", f"{s.sweep_time_s * 1e3:.3f} ms")
        self._set(":DET", _DETECTOR_SCPI.get(s.detector, "AUTO"))   # PM p.77
        # tracking generator: level first, then the switch (PM p.94, p.65)
        self._set(":SOUR:POW:TRAC", f"{s.tg_level_dBm:.1f}")
        self._set(":OUTP:TRAC", "ON" if s.tg_on else "OFF")
        self._points = int(s.points)
        old_sweep_s = self._sweep_rb_s
        rb = self._readback(s)
        self._sweep_rb_s = float(rb.get("sweep_time_s", s.sweep_time_s))
        if self._n_sent != sent0:
            # Something changed. The manual does not say whether a settings
            # change restarts the running sweep; if it does NOT, the sweep in
            # progress (at the OLD sweep time -- possibly much longer, e.g.
            # after a narrow RBW) is part old, part new. Wait it out once more
            # before trusting whole sweeps.  # VERIFY: does a change restart it?
            self._extra_wait_s = old_sweep_s
        return rb

    def _readback(self, s: SweepSettings) -> dict:
        """What the instrument really uses. A query that fails or does not
        parse falls back to the brain's own number (and is not fatal: the
        trace is what matters)."""
        def q(cmd, scale, fallback):
            try:
                return float(self._query(cmd).strip().split()[0]) * scale
            except Exception:
                return fallback
        return {
            "rbw_Hz": q(":BAND?", 1.0, s.rbw_Hz),                   # VERIFY reply in Hz
            "vbw_Hz": q(":BAND:VID?", 1.0, s.vbw_Hz),               # VERIFY reply in Hz
            "atten_dB": q(":POW:ATT?", 1.0, s.atten_dB),
            "sweep_time_s": q(":SWE:TIME?", 1e-3, s.sweep_time_s),  # PM p.92: reply in ms  # VERIFY
        }

    # ---- sweeping ----------------------------------------------------------------

    def start_sweep(self, s: SweepSettings) -> float:
        hw = self.cfg.hardware
        # The instrument's own sweep time counts, not the brain's estimate: an
        # auto sweep time on the GSP-818 need not follow our formula, and a
        # wait shorter than the real sweep reads a half-old trace.
        t = max(float(s.sweep_time_s), self._sweep_rb_s, 0.0)
        if hw.sweep_mode == "single":
            if not self._single_armed:
                self._write(":INIT:CONT OFF")      # PM p.60: single-sweep mode
                self._single_armed = True
            self._write(":INIT:IMM")               # NOT in the manual  # VERIFY
            wait = t + hw.sweep_margin_s
        else:
            if self._single_armed:                 # back from single mode
                self._write(":INIT:CONT ON")
                self._single_armed = False
            self._applied.setdefault(":INIT:CONT", "ON")
            wait = max(1, int(hw.settle_sweeps)) * (t + hw.sweep_margin_s) + self._extra_wait_s
            self._extra_wait_s = 0.0
        self._pending = True
        return wait

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        if not self._pending:
            raise RuntimeError("finish_sweep without start_sweep")
        self._pending = False
        y = _parse_trace(self._query(":TRAC? TRACE1"))        # PM p.100
        if self._points and y.size != self._points:
            # A trace of another length means the instrument does not use the
            # point count we asked for -- the frequency grid would be wrong.  # VERIFY
            raise ValueError(f"trace has {y.size} points, expected {self._points} "
                             "(does the GSP-818 accept this point count?)")
        return y, {}

    def abort_sweep(self) -> None:
        # Nothing to cancel on the instrument: in "wait" mode it keeps sweeping,
        # in "single" mode the next :INIT:IMM restarts it.
        self._pending = False

    # ---- plumbing ------------------------------------------------------------------

    def _set(self, header: str, value: str) -> None:
        if self._applied.get(header) != value:
            self._write(f"{header} {value}")
            self._applied[header] = value

    _n_sent = 0                          # settings writes so far (configure compares)

    def _write(self, cmd: str) -> None:
        if self._inst is None:
            raise RuntimeError("GSP-818 is not open")
        self._inst.write(cmd)
        self._n_sent += 1

    def _query(self, cmd: str) -> str:
        if self._inst is None:
            raise RuntimeError("GSP-818 is not open")
        return self._inst.query(cmd)

    def _find_usb(self, rm) -> str:
        """The first USB instrument of GW Instek whose *IDN? says GSP-818."""
        candidates = [r for r in rm.list_resources("USB?*INSTR")
                      if GW_INSTEK_USB_VID.lower() in r.lower() or f"::{GW_INSTEK_USB_VID_DEC}::" in r]
        for name in candidates:
            try:
                inst = rm.open_resource(name)
                try:
                    inst.timeout = 2000
                    if "GSP-818" in inst.query("*IDN?").upper():
                        return name
                finally:
                    inst.close()
            except Exception:
                continue
        raise RuntimeError("no GSP-818 found on USB (is the VISA USB driver installed? "
                           "set hardware.resource to its VISA address)")
