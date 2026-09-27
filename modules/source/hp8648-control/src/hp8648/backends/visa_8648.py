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
    level       POW:AMPL <v> DBM (or DB)      query POW:AMPL?
    frequency   FREQ:CW <v> MHZ               query FREQ:CW?
    modulation  (read only)                   AM:STAT?  FM:STAT?  PM:STAT?
    references  (read only)                   POW:REF:STAT?  POW:REF?
                                              FREQ:REF:STAT? FREQ:REF?
                                              POW:ATT:AUTO?
    RPP/level   STAT:QUES:POW:COND?           bit0 RPP, bit1 unspecified level
    errors      *CLS, SYST:ERR?

ADOPT, DO NOT RESET (Lukas, 2026-09-27). open() used to send *RST, RF off,
every modulation off and the reference / attenuator modes to known values.
Now it only READS: whatever the generator is doing when the service connects
is what the module reports and keeps doing. The one write left at open() is
*CLS, which empties the status and error registers and changes nothing at the
RF output. RF OFF on close() stays -- shutdown is not part of the rule.

Because we no longer force the reference modes off, the backend reads them and
CONVERTS in software: with POWer:REFerence:STATe ON the box talks in dB
relative to the reference, with FREQuency:REFerence:STATe ON in Hz relative to
it. The brain always sees absolute dBm and Hz.

Every line that could not be confirmed against the instrument itself carries a
`# VERIFY` marker.

The rear-panel LANGUAGE switch must be on SCPI: in "COMP" (HP 8656/8657
compatible) mode none of this parses. # VERIFY on the unit.
"""

from __future__ import annotations


class Visa8648:
    """Drives a physical HP 8648D. Implements the SigGenBackend interface."""

    def __init__(self, resource: str = "GPIB0::19::INSTR",
                 timeout_ms: int = 5000):
        self._resource = resource
        self._timeout_ms = int(timeout_ms)
        self._rm = None
        self._inst = None
        self._idn = ""
        # Reference modes found at open() (read, never changed). With a mode
        # OFF the offset is 0 and the conversions below are no-ops.
        self._pow_ref_on = False
        self._pow_ref_dBm = 0.0
        self._freq_ref_on = False
        self._freq_ref_Hz = 0.0
        self._notes: list[str] = []

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
        # *CLS empties the status registers and the error queue -- nothing the
        # sample can feel -- so SYST:ERR? below reports only what WE cause.
        self._inst.write("*CLS")
        self._idn = self._query("*IDN?")
        self._notes = []
        self._read_modes()
        # A mode query the unit does not know leaves an "undefined header" in
        # the queue; drain it so it is not reported later as our fault.
        for err in self.drain_errors():
            self._notes.append(f"instrument error while reading its state: {err}")

    def _read_modes(self) -> None:
        """Read (never set) the modes that change what POW:AMPL? / FREQ:CW?
        mean, and the attenuator hold. Each query is guarded: a unit that does
        not answer one (timeout) is assumed to be in the plain mode, and a
        note says so."""
        def q_bool(cmd):
            return self._query(cmd) in ("1", "ON", "+1")
        try:
            self._pow_ref_on = q_bool("POW:REF:STAT?")              # VERIFY
            if self._pow_ref_on:
                self._pow_ref_dBm = float(self._query("POW:REF?"))  # VERIFY: dBm
                self._notes.append(
                    f"POWer reference mode is ON (reference {self._pow_ref_dBm:g} dBm): "
                    "left on, levels converted to absolute dBm in software")
        except Exception as exc:
            self._pow_ref_on = False
            self._notes.append(f"could not read POW:REF:STAT? ({exc}); assuming OFF")
        try:
            self._freq_ref_on = q_bool("FREQ:REF:STAT?")            # VERIFY
            if self._freq_ref_on:
                self._freq_ref_Hz = float(self._query("FREQ:REF?"))  # VERIFY: Hz
                self._notes.append(
                    f"FREQuency reference mode is ON (reference {self._freq_ref_Hz:g} Hz): "
                    "left on, frequencies converted to absolute Hz in software")
        except Exception as exc:
            self._freq_ref_on = False
            self._notes.append(f"could not read FREQ:REF:STAT? ({exc}); assuming OFF")
        try:
            # Attenuator HOLD (AUTO OFF) limits the level range around the held
            # setting; out-of-range levels then raise the "unspecified" bit.
            # We used to force AUTO ON; now we only report it.
            if not q_bool("POW:ATT:AUTO?"):                         # VERIFY
                self._notes.append("attenuator HOLD is on (POW:ATT:AUTO OFF): left as "
                                   "found; the usable level range is limited")
        except Exception as exc:
            self._notes.append(f"could not read POW:ATT:AUTO? ({exc})")

    def startup_notes(self) -> list[str]:
        return list(self._notes)

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
        if self._pow_ref_on:
            # reference mode left on by the operator: talk RELATIVE to it
            self._inst.write(f"POW:AMPL {dBm - self._pow_ref_dBm:.1f} DB")  # VERIFY
        else:
            self._inst.write(f"POW:AMPL {dBm:.1f} DBM")

    def read_power(self) -> float:
        raw = float(self._query("POW:AMPL?"))                    # VERIFY: DBM while REF off
        # with the reference mode on the reply is dB relative to POW:REF
        return raw + self._pow_ref_dBm if self._pow_ref_on else raw

    def set_frequency(self, hz: float) -> None:
        # "up to 9 digits with a maximum of 10 Hz resolution" -> send MHz with
        # five decimals (10 Hz), which is 9 digits at 4000 MHz.
        if self._freq_ref_on:
            hz = hz - self._freq_ref_Hz                          # VERIFY relative entry
        self._inst.write(f"FREQ:CW {hz / 1e6:.5f} MHZ")          # VERIFY digits accepted

    def read_frequency(self) -> float:
        raw = float(self._query("FREQ:CW?"))                     # VERIFY: reply in Hz
        return raw + self._freq_ref_Hz if self._freq_ref_on else raw  # VERIFY relative reply

    # ---- status registers -----------------------------------------------

    def read_power_condition(self) -> int:
        return int(float(self._query("STAT:QUES:POW:COND?")))    # VERIFY

    def read_modulation(self) -> dict:
        out = {}
        for k, cmd in (("am", "AM:STAT?"), ("fm", "FM:STAT?"), ("pm", "PM:STAT?")):
            out[k] = self._query(cmd) in ("1", "ON", "+1")       # VERIFY reply form
        return out

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
