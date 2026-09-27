"""The real HP / Agilent 8648D over GPIB (HP-IB), in SCPI via PyVISA.

This is the ONLY file that touches `pyvisa`, and it imports it LAZILY (inside
open(), not at module top) -- so the whole package still imports and the
simulator still runs on a machine with no VISA drivers installed. On the lab PC:
`uv sync --extra gui --extra real` (gotcha #29: name every extra you need).

Reference: HP 8648A/B/C/D Signal Generator Operation and Service Guide
(part no. 08648-90048), chapter 2 "HP-IB Programming":
  * Table 2-1 "Programming Command Statements"
  * the SCPI command reference pages: AM / FM / PM / PULM subsystems (":STATe
    ON|OFF", *RST OFF), FREQuency [:CW|:FIXed] (*RST 100 MHz), OUTPut:STATe
    (*RST OFF), POWer[:LEVel][:IMMediate][:AMPLitude] (*RST -136 dBm, answers
    in DBM while POWer:REFerence:STATe is OFF), POWer:ATTenuation:AUTO,
    STATus:QUEStionable:POWer:CONDition?, SYSTem:ERRor? ("<number>,<string>")
  * "Reverse Power Protection Status" (POWer condition bit 0; reset by turning
    the RF output on again) and "Unspecified Power Entry Status" (bit 1).

Commands used:
    RF output   OUTP:STAT ON|OFF              query OUTP:STAT?
    level       POW:AMPL <v> DBM              query POW:AMPL?
    frequency   FREQ:CW <v> HZ                query FREQ:CW?
    modulation  AM:STAT OFF  FM:STAT OFF  PM:STAT OFF  PULM:STAT OFF
    references  POW:REF:STAT OFF  FREQ:REF:STAT OFF  POW:ATT:AUTO ON
    RPP/level   STAT:QUES:POW:COND?           bit0 RPP, bit1 unspecified level
    errors      SYST:ERR?

Every line that could not be confirmed against the instrument itself carries a
`# VERIFY` marker.

The rear-panel LANGUAGE switch must be on SCPI: in "COMP" (HP 8656/8657
compatible) mode none of this parses. # VERIFY on the unit.
"""

from __future__ import annotations

import time


class Visa8648:
    """Drives a physical HP 8648D. Implements the SigGenBackend interface."""

    def __init__(self, resource: str = "GPIB0::19::INSTR",
                 timeout_ms: int = 5000, reset_on_open: bool = True):
        self._resource = resource
        self._timeout_ms = int(timeout_ms)
        self._reset_on_open = bool(reset_on_open)
        self._rm = None
        self._inst = None
        self._idn = ""

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        import pyvisa                                   # lazy: only needed for real hw
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self._resource)
        self._inst.timeout = self._timeout_ms
        # GPIB ends a message with EOI; a newline as well is harmless and lets
        # the same code work through a GPIB-to-USB/LAN adapter.
        self._inst.write_termination = "\n"             # VERIFY
        self._inst.read_termination = "\n"              # VERIFY
        self._inst.write("*CLS")                        # clear status + error queue
        # Belt and braces: RF off FIRST, before anything else is touched, so
        # nothing below can put a surprise level on the sample.
        self._inst.write("OUTP:STAT OFF")
        if self._reset_on_open:
            # *RST: RF off, every modulation off, -136 dBm, 100 MHz.
            self._inst.write("*RST")
            time.sleep(0.5)                             # VERIFY how long a preset takes
        self.modulation_off()
        # Absolute units both ways: with a reference mode on, POW:AMPL? would
        # answer in dB RELATIVE to the reference and our echo would be wrong.
        self._inst.write("POW:REF:STAT OFF")            # VERIFY
        self._inst.write("FREQ:REF:STAT OFF")           # VERIFY
        self._inst.write("POW:ATT:AUTO ON")             # VERIFY (attenuator hold off)
        self._idn = self._query("*IDN?")
        # Options that are not fitted (pulse modulation needs 1E6) answer
        # PULM:STAT with "undefined header"; that is expected, so the queue is
        # drained here and not reported as a fault.
        self.drain_errors()

    def close(self) -> None:
        try:
            if self._inst is not None:
                self._inst.write("OUTP:STAT OFF")       # RF off on the way out
        finally:
            if self._inst is not None:
                try:
                    # hand the front panel back to the operator: 6 is
                    # pyvisa's RENLineOperation.address_gtl -- Go To Local for
                    # THIS device only, leaving REN (and every other
                    # instrument on the bus) alone.
                    self._inst.control_ren(6)           # VERIFY the box leaves REMOTE
                except Exception:
                    pass
                self._inst.close()
            if self._rm is not None:
                self._rm.close()
            self._inst = None
            self._rm = None

    # ---- small SCPI helpers ---------------------------------------------

    def _query(self, cmd: str) -> str:
        return self._inst.query(cmd).strip()

    # ---- output ----------------------------------------------------------

    def set_output(self, on: bool) -> None:
        self._inst.write(f"OUTP:STAT {'ON' if on else 'OFF'}")

    def read_output(self) -> bool:
        return self._query("OUTP:STAT?") in ("1", "ON", "+1")    # VERIFY reply form

    # ---- level / frequency ------------------------------------------------

    def set_power(self, dBm: float) -> None:
        # 0.1 dB is the instrument's resolution; more digits buy nothing.
        self._inst.write(f"POW:AMPL {dBm:.1f} DBM")

    def read_power(self) -> float:
        return float(self._query("POW:AMPL?"))                   # VERIFY: DBM while REF off

    def set_frequency(self, hz: float) -> None:
        # "up to 9 digits with a maximum of 10 Hz resolution" -> send MHz with
        # five decimals (10 Hz), which is 9 digits at 4000 MHz.
        self._inst.write(f"FREQ:CW {hz / 1e6:.5f} MHZ")          # VERIFY digits accepted

    def read_frequency(self) -> float:
        return float(self._query("FREQ:CW?"))                    # VERIFY: reply in Hz

    # ---- status registers -----------------------------------------------

    def read_power_condition(self) -> int:
        return int(float(self._query("STAT:QUES:POW:COND?")))    # VERIFY

    def read_modulation(self) -> dict:
        out = {}
        for k, cmd in (("am", "AM:STAT?"), ("fm", "FM:STAT?"), ("pm", "PM:STAT?")):
            out[k] = self._query(cmd) in ("1", "ON", "+1")       # VERIFY reply form
        return out

    def modulation_off(self) -> None:
        # Each modulation must be switched off explicitly; turning one on does
        # not turn the others off (manual, AM/FM/PM subsystems).
        for cmd in ("AM:STAT OFF", "FM:STAT OFF", "PM:STAT OFF",
                    "PULM:STAT OFF"):                            # PULM only with option 1E6
            self._inst.write(cmd)

    def drain_errors(self) -> list[str]:
        errors = []
        for _ in range(20):                             # guard against a runaway queue
            resp = self._query("SYST:ERR?")
            # replies look like:  +0,"No error"   or   -222,"Data out of range"
            code = resp.split(",", 1)[0].strip()        # VERIFY exact form
            if code.lstrip("+") == "0":
                break
            errors.append(resp)
        return errors

    def idn(self) -> str:
        return self._idn
