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
    VERIFY on the unit: an earlier note read B.20 as "remote mode starts with
    OUTP OFF". If addressing the BOP over GPIB really switches a live output
    off, adoption can only ever find it OFF -- watch the front panel when the
    service starts with the output on.
  * START-UP READS, IT DOES NOT WRITE (Lukas, 2026-09-27). open() sends *IDN?
    and *CLS only; read_state() then queries FUNC:MODE?, OUTP?, VOLT?, CURR?
    and the brain adopts the answers. The range (CURR:RANG / VOLT:RANG) is left
    as found and is pinned only after the next EXPLICIT mode change.
  * sec. 1.2.1    Readback is the average of the last 16 conversions, valid
    ~320 ms after a change. (The brain's `acquisition.settle_s` covers it.)
  * sec. B.87     SYST:REM is for RS-232 only; on GPIB the bus puts the unit in
    remote, so it is not sent.
"""

from __future__ import annotations

from ..hwlock import claim

# The name this module signs its hardware claims with. Another service that
# finds the address taken reads it in its error message ("... in use by kepco").
MODULE = "kepco"


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
        self._lock = None      # our claim on the GPIB address (see open())

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        # ONE INSTRUMENT, ONE SERVICE (Lukas: "the same instrument has to be
        # defined by the same physical address"). This BOP is the same box
        # clMag-control drives on GPIB0::6. Claim the address BEFORE a single
        # byte goes on the bus: if clMag (or a second kepco) already holds it,
        # claim() raises HardwareBusy naming the holder and we never touch the
        # instrument. hwlock normalises the spelling, so "GPIB::6" and
        # "GPIB0::6::INSTR" are recognised as the same unit.
        self._lock = claim(self._resource, MODULE)
        try:
            self._open_claimed()
        except BaseException:
            # A failed open must not leave the address claimed (nor a VISA
            # session dangling). _disconnect() writes NOTHING: we may have
            # failed before learning the instrument's state, and it may be
            # driving a coil.
            self._disconnect()
            raise

    def _open_claimed(self) -> None:
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
        # *CLS only empties the status registers and the error queue; it does
        # not touch the output, the mode or any programmed value. It is the
        # ONLY write at start: so that a later SYST:ERR? reports our errors,
        # not something left over from the last session.
        self._write("*CLS")                                 # VERIFY A.2: clear status + error queue
        # Nothing else is written here (Lukas, 2026-09-27: "read the instrument
        # state on startup, not change anything"). The BOP may be driving a
        # coil right now -- possibly left live by clMag-control, which drives
        # the SAME physical unit. No OUTP OFF, no VOLT 0 / CURR 0, no FUNC:MODE,
        # no range pinning: the brain reads the state with read_state() and
        # adopts it.

    def read_state(self) -> dict:
        """What the BOP is doing right now, from QUERIES only.

        mode      FUNC:MODE?  (B.23: 0 = voltage, 1 = current)
        output    OUTP?       (B.21: 0/1)
        voltage_V VOLT?       the PROGRAMMED voltage: the output in voltage
                              mode, the voltage LIMIT in current mode (4.1.1.1)
        current_A CURR?       the programmed current, likewise
        """
        mode = self.read_mode()
        output = self.read_output()
        # VERIFY B.58 / B.49: VOLT? / CURR? return the PROGRAMMED value (not a
        # measurement) in <exp_value> form. Also check what they return while
        # the output is OFF: B.20 says OUTP OFF saves the programmed values and
        # programs 0, so they may read 0 until OUTP ON restores them.
        volts = float(self._query("VOLT?"))
        amps = float(self._query("CURR?"))
        return {"mode": mode, "output": output, "voltage_V": volts,
                "current_A": amps}

    def close(self, output_off: bool = True) -> None:
        # output_off=False is a restart (shutdown{keep_outputs}): no OUTP OFF,
        # the coil keeps its current and the next start adopts it
        try:
            if self._inst is not None and output_off:
                self._write("OUTP OFF")                     # VERIFY B.20
        finally:
            self._disconnect()

    def disconnect(self) -> None:
        """Let go of the instrument WITHOUT writing anything: close the VISA
        session and release the address claim. For the brain's "could not read
        the state at start" path, where close() (which sends OUTP OFF) would
        de-energise a coil we never took control of."""
        self._disconnect()

    def _disconnect(self) -> None:
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
        # Release LAST: only once our session is closed may another service
        # open the instrument.
        if self._lock is not None:
            self._lock.release()
            self._lock = None

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

    # ---- queries (read_state uses the first two; the rest by hand) ------

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

