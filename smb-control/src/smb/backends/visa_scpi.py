"""The real SMB100A over GPIB, spoken in SCPI via PyVISA.

This is the only file that touches `pyvisa`, and it imports it LAZILY (inside
open(), not at module top) -- so the whole package still imports and the
simulator still runs on a machine with no VISA drivers installed. On the lab PC,
uncomment `pyvisa` in pyproject.toml, `uv sync`, and this backend just works.

SCPI reference (R&S SMB100A), the four things we control:
    RF output   OUTP:STAT ON|OFF          query OUTP:STAT?   -> 0 / 1
    level       POW <value>               (dBm)  query POW?
    frequency   FREQ <value>              (Hz)   query FREQ?
    phase       PHAS <value>              (deg)  query PHAS?
open() changes NOTHING on the instrument (the adopt-on-start rule, 2026-09-27):
it used to send UNIT:ANGL DEG and OUTP:STAT OFF, which switched off an RF
output that a running experiment was using. Now it only clears the error queue
(*CLS -- no effect on the signal) and QUERIES the angle unit; if the box is set
to radians, read_phase() converts in software instead of changing the unit.
set_phase() always sends an explicit "DEG" suffix, so it is unit-proof anyway.

The Generator clamps every value to the configured safety limits BEFORE it
reaches this backend, so here we simply forward commands and read back.
"""

from __future__ import annotations


class VisaSMB100A:
    """Drives a physical SMB100A. Implements the RFSource interface."""

    def __init__(self, resource: str = "GPIB0::28::INSTR",
                 timeout_ms: int = 5000, settle_s: float = 0.05):
        self._resource = resource
        self._timeout_ms = timeout_ms
        self._settle_s = settle_s
        self._rm = None
        self._inst = None
        self._phase_in_rad = False                      # learned in open(), never set

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        import pyvisa                                   # lazy: only needed for real hw
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self._resource)
        self._inst.timeout = self._timeout_ms
        # SCPI instruments are line-terminated; \n is the SMB100A default.
        self._inst.write_termination = "\n"
        self._inst.read_termination = "\n"
        # *CLS only empties the status/error queue; it does not touch RF,
        # level, frequency or phase, so it is allowed under the adopt rule.
        self._inst.write("*CLS")
        # Which unit will PHAS? answer in? READ it, do not set it.
        # VERIFY on the SMB100A: the reply spelling of UNIT:ANGL? (expected
        # "DEG" / "RAD") and that PHAS? follows this unit.
        try:
            unit = self._query("UNIT:ANGL?").upper()
        except Exception:
            unit = "DEG"                                # the factory default
        self._phase_in_rad = unit.startswith("RAD")

    def close(self) -> None:
        try:
            if self._inst is not None:
                self._inst.write("OUTP:STAT OFF")       # RF off on the way out
        finally:
            if self._inst is not None:
                self._inst.close()
            if self._rm is not None:
                self._rm.close()
            self._inst = None
            self._rm = None

    # ---- small SCPI helpers ---------------------------------------------

    def _write(self, cmd: str) -> None:
        self._inst.write(cmd)
        if self._settle_s:
            import time
            time.sleep(self._settle_s)

    def _query(self, cmd: str) -> str:
        return self._inst.query(cmd).strip()

    def check_errors(self) -> list[str]:
        """Drain the instrument's error queue (SYST:ERR?). Empty list == clean."""
        errors = []
        for _ in range(20):                             # guard against a runaway queue
            resp = self._query("SYST:ERR?")
            # replies look like:  0,"No error"   or   -222,"Data out of range"
            code = resp.split(",", 1)[0].strip()
            if code in ("0", "+0"):
                break
            errors.append(resp)
        return errors

    # ---- RF output on/off ------------------------------------------------
    def set_output(self, on: bool) -> None:
        self._write(f"OUTP:STAT {'ON' if on else 'OFF'}")

    def read_output(self) -> bool:
        return self._query("OUTP:STAT?").strip() in ("1", "ON")

    # ---- level -----------------------------------------------------------
    def set_power(self, dBm: float) -> None:
        self._write(f"POW {dBm:.3f}")

    def read_power(self) -> float:
        return float(self._query("POW?"))

    # ---- frequency -------------------------------------------------------
    def set_frequency(self, hz: float) -> None:
        self._write(f"FREQ {hz:.3f}")

    def read_frequency(self) -> float:
        return float(self._query("FREQ?"))

    # ---- phase -----------------------------------------------------------
    def set_phase(self, deg: float) -> None:
        self._write(f"PHAS {deg:.3f} DEG")

    def read_phase(self) -> float:
        value = float(self._query("PHAS?"))
        if self._phase_in_rad:                          # convert, never change the unit
            import math
            value = math.degrees(value)
        return value

    # ---- identity --------------------------------------------------------
    def idn(self) -> str:
        try:
            return self._query("*IDN?")
        except Exception:
            return ""
