"""The real Kepco BOP 20-10 over GPIB, spoken in SCPI via PyVISA.

This is the ONLY file that touches `pyvisa`, and it imports it LAZILY (inside
open(), not at module top) -- so the package still imports and the simulator
still runs on a PC with no VISA installed. On the lab PC:
    uv sync --extra gui --extra real       (gotcha #29: name every extra)
plus a VISA runtime (NI-VISA or Keysight IO Libraries) for the GPIB card.

Source of every command below: Kepco "BIT 4886 Operator Manual" (the GPIB
interface card of the BOP), revision dated 2022-04-25 ("042522"), fetched from
kepcopower.com/support/bit4886-opr-r32.pdf. Section numbers are cited per call.
It has NOT been run against the instrument yet: every call is marked # VERIFY
until someone has watched it work on the real unit.

What the manual says that shapes this file:
  * sec. 4.1.1.1  There are NO separate protection/limit commands on GPIB
    (no VOLT:LIM, no VOLT:PROT). The limit channel is programmed with the
    SAME commands as the main channel: in current mode `VOLT x` sets the
    voltage limit, in voltage mode `CURR x` sets the current limit, both as
    ABSOLUTE values. The front-panel screwdriver limits still apply on top.
  * sec. 4.1.1.2  Auto-ranging switches the DAC gain at 1/4 of full scale and
    can put a spike on the output while crossing it. Range 1 pins full scale:
    `VOLT:RANG 1` in voltage mode (B.61), `CURR:RANG 1` in current mode (B.52,
    "remembered until a func:mode command is processed"). FUNC:MODE and *RST
    re-enable auto-ranging, so it is re-sent after every mode change. (B.61
    says VOLT:RANG acts on the active mode's range too; the mode-specific
    command is used because B.52 states its current-mode behaviour outright.)
  * sec. 4.1.1.3  "The BIT 4886 has a maximum ramp step rate of 25
    milliseconds" -- the brain caps `ramp.step_hz` at 40.
  * sec. 4.7.2    DIAG:OUTP decides what the LIMIT channels do while the output
    is off (default n=0: both limits 0). Not changed here; # VERIFY which
    setting the rig's unit carries (DIAG:OUTP? / front panel).
  * sec. B.20     OUTP OFF saves the programmed values and programs 0 V / 0 A
    at once; OUTP ON restores them. The brain therefore programs the main
    channel to 0 BEFORE switching on and ramps to 0 BEFORE switching off.
  * sec. 1.2.1    Readback is the average of the last 16 conversions, valid
    ~320 ms after a change. (The brain's `acquisition.settle_s` covers it.)
  * sec. B.87     SYST:REM is for RS-232 only; on GPIB the bus puts the unit in
    remote, so it is not sent.
"""

from __future__ import annotations


class VisaBOP:
    """Drives a physical Kepco BOP with a BIT 4886 card. Implements
    BipolarSupplyBackend."""

    def __init__(self, resource: str = "GPIB0::6::INSTR", timeout_ms: int = 5000,
                 full_range: bool = True):
        self._resource = resource
        self._timeout_ms = int(timeout_ms)
        self._full_range = bool(full_range)
        self._rm = None
        self._inst = None
        self._idn = ""

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        try:
            import pyvisa                                   # lazy: real hardware only
        except ImportError as exc:                          # pragma: no cover
            raise RuntimeError("pyvisa is not installed: run "
                               "`uv sync --extra gui --extra real`") from exc
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self._resource)
        self._inst.timeout = self._timeout_ms
        # VERIFY: line terminations. GPIB normally ends a message with EOI;
        # "\n" is the SCPI convention and what the manual's examples imply.
        self._inst.write_termination = "\n"
        self._inst.read_termination = "\n"
        self._idn = self._query("*IDN?")                    # VERIFY A.6: "KEPCO,BIT 4886,..."
        self._write("*CLS")                                 # VERIFY A.2: clear status + error queue
        # Output off FIRST, then both channels to 0, so opening the connection
        # never energises anything (B.20: remote mode starts with OUTP OFF anyway).
        self._write("OUTP OFF")                             # VERIFY B.20
        self._write("VOLT 0")                               # VERIFY B.57
        self._write("CURR 0")                               # VERIFY B.48

    def close(self) -> None:
        try:
            if self._inst is not None:
                self._write("OUTP OFF")                     # VERIFY B.20
        finally:
            if self._inst is not None:
                try:
                    self._inst.close()
                except Exception:
                    pass
            if self._rm is not None:
                try:
                    self._rm.close()
                except Exception:
                    pass
            self._inst = None
            self._rm = None

    # ---- programming -----------------------------------------------------

    def set_mode(self, mode: str) -> None:
        if mode not in ("voltage", "current"):
            raise ValueError(f"unknown mode {mode!r}")
        self._write(f"FUNC:MODE {'CURR' if mode == 'current' else 'VOLT'}")   # VERIFY B.22
        if self._full_range:
            # FUNC:MODE just turned auto-ranging back on (sec. 4.1.1.2), so pin
            # full scale again, with the command that belongs to the mode:
            # CURR:RANG 1 (B.52) in current mode, VOLT:RANG 1 (B.61) in voltage.
            if mode == "current":
                self._write("CURR:RANG 1")                  # VERIFY B.52
            else:
                self._write("VOLT:RANG 1")                  # VERIFY B.61

    def program_voltage(self, volts: float) -> None:
        # B.57: <exp_value>, "digits with decimal point and Exponent".
        self._write(f"VOLT {float(volts):.5E}")             # VERIFY B.57

    def program_current(self, amps: float) -> None:
        self._write(f"CURR {float(amps):.5E}")              # VERIFY B.48

    def set_output(self, on: bool) -> None:
        self._write(f"OUTP {'ON' if on else 'OFF'}")        # VERIFY B.20

    # ---- measurement -----------------------------------------------------

    def measure_voltage(self) -> float:
        return float(self._query("MEAS:VOLT?"))             # VERIFY B.19 reply format

    def measure_current(self) -> float:
        return float(self._query("MEAS:CURR?"))             # VERIFY B.18 reply format

    def idn(self) -> str:
        return self._idn

    # ---- diagnostics (used by hand from a Python prompt) ----------------

    def read_mode(self) -> str:
        return "current" if self._query("FUNC:MODE?").strip() == "1" else "voltage"   # VERIFY B.23

    def read_output(self) -> bool:
        return self._query("OUTP?").strip() in ("1", "ON")  # VERIFY B.21

    def ratings(self) -> tuple[float, float]:
        """(V max, I max) of the model, e.g. (20, 10) for a BOP 20-10."""
        return (float(self._query("VOLT? MAX")),            # VERIFY B.58 syntax
                float(self._query("CURR? MAX")))            # VERIFY B.49 syntax

    def check_errors(self) -> list[str]:
        """Drain the error queue (SYST:ERR?, B.80). Empty list == clean."""
        errors = []
        for _ in range(20):                                 # guard a runaway queue
            resp = self._query("SYST:ERR?")                 # VERIFY B.80: 0,"No error"
            code = resp.split(",", 1)[0].strip()
            if code in ("0", "+0"):
                break
            errors.append(resp)
        return errors

    # ---- small SCPI helpers ---------------------------------------------

    def _write(self, cmd: str) -> None:
        self._inst.write(cmd)

    def _query(self, cmd: str) -> str:
        return self._inst.query(cmd).strip()

