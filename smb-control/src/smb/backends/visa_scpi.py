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
We send UNIT:ANGL DEG once on open so phase is always in degrees both ways.

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

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        import pyvisa                                   # lazy: only needed for real hw
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self._resource)
        self._inst.timeout = self._timeout_ms
        # SCPI instruments are line-terminated; \n is the SMB100A default.
        self._inst.write_termination = "\n"
        self._inst.read_termination = "\n"
        self._inst.write("*CLS")                        # clear status/error queue
        self._inst.write("UNIT:ANGL DEG")               # phase in degrees from now on
        # Leave RF as-is on connect except make the OFF state explicit and safe.
        self._inst.write("OUTP:STAT OFF")

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
        return float(self._query("PHAS?"))

    # ---- identity --------------------------------------------------------
    def idn(self) -> str:
        try:
            return self._query("*IDN?")
        except Exception:
            return ""
