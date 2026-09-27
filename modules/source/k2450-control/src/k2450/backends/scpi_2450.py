"""Real hardware: the Keithley 2450 SourceMeter over VISA, SCPI command set.

THE ONLY FILE THAT TOUCHES THE VENDOR LIBRARY. pyvisa is imported lazily
inside open(), so the package imports (and the simulator runs) on a PC with no
VISA installed. Install it with `uv sync --extra gui --extra real`
(gotcha #29: a plain `uv sync --extra gui` REMOVES pyvisa again).

Sources used for the command strings:
  * Keithley "Model 2450 Interactive SourceMeter Instrument Reference Manual"
    (2450-901-01), sections "SCPI command reference" (:SOURce, :SENSe, :OUTPut,
    :READ?, :ROUTe:TERMinals, *LANG) and "Source and measure ranges".
  * pymeasure's Keithley2450 driver (pymeasure/instruments/keithley/
    keithley2450.py), which uses the same :SOUR/:SENS/:OUTP strings.
Nothing here has been run against a real 2450 yet: every line that talks to
the instrument is marked # VERIFY until it has.

The 2450 speaks THREE command sets, chosen on the front panel (MENU > System >
Settings > Command Set) or with *LANG, and a change needs a power cycle:
  SCPI      -- this file.
  TSP       -- Lua scripts; not supported here.
  SCPI2400  -- 2400 emulation; different strings (:SOUR:VOLT:LEV, :SENS:CURR:PROT).
open() checks *LANG? and refuses anything but SCPI, with a clear message,
rather than sending commands the instrument would reject one by one.
"""

from __future__ import annotations

import math

from .base import InstrumentState, Reading, other

# SCPI mnemonics per function
_SRC = {"voltage": "VOLT", "current": "CURR"}
# The compliance of a source function is the OTHER quantity: ILIM when sourcing V.
_LIM = {"voltage": "ILIM", "current": "VLIM"}

#: The 2450 returns this for an overflowed reading (the usual Keithley 9.9E37).
_OVERFLOW = 9.0e37


class VisaK2450:
    """Keithley 2450 SourceMeter, SCPI over VISA (USB-TMC, GPIB or LAN)."""

    def __init__(self, resource: str, visa_library: str = "",
                 timeout_ms: int = 10000, terminals: str = "front"):
        self.resource = resource
        self.visa_library = visa_library
        self.timeout_ms = int(timeout_ms)
        # Only used if the user later ASKS for front/rear (set_config); open()
        # no longer writes it -- the instrument's own choice is read and adopted.
        self.terminals = terminals
        self._rm = None
        self._inst = None
        self._idn = ""
        self._fn = "voltage"
        self._src_auto = {"voltage": True, "current": True}
        self._meas_auto = {"voltage": True, "current": True}

    # ---- lifecycle -----------------------------------------------------------
    def open(self) -> None:
        try:
            import pyvisa                                   # lazy: only on the lab PC
        except ImportError as exc:
            raise RuntimeError("pyvisa is not installed: uv sync --extra gui "
                               "--extra real") from exc
        self._rm = (pyvisa.ResourceManager(self.visa_library)
                    if self.visa_library else pyvisa.ResourceManager())
        inst = self._rm.open_resource(self.resource)
        inst.timeout = self.timeout_ms
        inst.read_termination = "\n"                          # VERIFY (LAN socket needs it; USB/GPIB default EOI)
        inst.write_termination = "\n"                         # VERIFY
        self._inst = inst
        self._idn = self._q("*IDN?")                          # VERIFY
        lang = self._q("*LANG?").upper()                      # VERIFY: returns SCPI | TSP | SCPI2400
        if lang != "SCPI":
            raise RuntimeError(
                f"the 2450 is in the {lang!r} command set; this module needs SCPI. "
                "Front panel: MENU > System > Settings > Command Set > SCPI, "
                "then power-cycle the instrument.")
        # QUERIES ONLY from here on (Lukas, 2026-09-27: "read the instrument
        # state on startup, not to change anything"). No :OUTP OFF, no
        # :ROUT:TERM, no :READ:BACK ON -- whatever the instrument is doing, it
        # keeps doing; read_state() tells the brain what that is.
        # *CLS only empties the error queue (and the event registers), so that
        # an old error left by someone at the front panel is not blamed on
        # our first command. It changes no source or measure setting.
        self._w("*CLS")                                      # VERIFY: clears the error queue only

    def close(self) -> None:
        if self._inst is None:
            return
        try:
            self._w(":OUTP OFF")                              # VERIFY
        finally:
            try:
                self._inst.close()
            finally:
                self._inst = None
                if self._rm is not None:
                    try:
                        self._rm.close()
                    finally:
                        self._rm = None

    def idn(self) -> str:
        return self._idn

    def read_state(self) -> InstrumentState:
        """Every setting the brain needs, read back with queries only.

        Both source functions are read (level, limit, range): the brain keeps
        the inactive function's pair for when you switch, and it should be the
        instrument's pair, not a config default. Measure settings (range, NPLC,
        remote sense) are read for the quantity each source function MEASURES.
        """
        fn = _func_from(self._q(":SOUR:FUNC?"))              # VERIFY: reply "VOLT" / "CURR"
        if fn not in ("voltage", "current"):
            raise RuntimeError(f"2450 reports source function {fn!r}")
        sense = _func_from(self._q(":SENS:FUNC?"))           # VERIFY: reply '"CURR:DC"' (quoted)
        level, limit, src_auto, src_range = {}, {}, {}, {}
        meas_auto, meas_range, nplc, rsen = {}, {}, {}, {}
        for f in ("voltage", "current"):
            m, mm = _SRC[f], _SRC[other(f)]
            # VERIFY: the level/limit/range of the INACTIVE function can be queried
            level[f] = float(self._q(f":SOUR:{m}?"))                     # VERIFY
            limit[f] = float(self._q(f":SOUR:{m}:{_LIM[f]}?"))           # VERIFY
            src_auto[f] = _on(self._q(f":SOUR:{m}:RANG:AUTO?"))          # VERIFY
            src_range[f] = float(self._q(f":SOUR:{m}:RANG?"))            # VERIFY
            mf = other(f)
            meas_auto[mf] = _on(self._q(f":SENS:{mm}:RANG:AUTO?"))       # VERIFY
            meas_range[mf] = float(self._q(f":SENS:{mm}:RANG?"))         # VERIFY
            nplc[mf] = float(self._q(f":SENS:{mm}:NPLC?"))               # VERIFY
            rsen[mf] = _on(self._q(f":SENS:{mm}:RSEN?"))                 # VERIFY
        output = _on(self._q(":OUTP?"))                                  # VERIFY
        term = self._q(":ROUT:TERM?").upper()                            # VERIFY: "FRON" / "REAR"
        terminals = "rear" if term.startswith("REAR") else "front"
        readback = _on(self._q(f":SOUR:{_SRC[fn]}:READ:BACK?"))          # VERIFY
        # measure() needs these without asking the instrument every time
        self._fn = fn
        self._src_auto = dict(src_auto)
        self._meas_auto = dict(meas_auto)
        return InstrumentState(function=fn, sense_function=sense, level=level,
                               limit=limit, src_auto=src_auto, src_range=src_range,
                               meas_auto=meas_auto, meas_range=meas_range,
                               nplc=nplc, four_wire=rsen, output=output,
                               terminals=terminals, readback=readback)

    def set_terminals(self, where: str) -> None:
        # VERIFY: the 2450 switches the OUTPUT OFF when the terminals change;
        # the brain switches it off itself first, so the two agree either way.
        self._w(f":ROUT:TERM {'REAR' if str(where).lower() == 'rear' else 'FRON'}")  # VERIFY
        self._check_errors("terminals")

    # ---- source --------------------------------------------------------------
    def set_source_function(self, fn: str) -> None:
        self._fn = fn
        self._w(f":SOUR:FUNC {_SRC[fn]}")                     # VERIFY
        # the measure function is a quoted string in the 2450's SCPI
        self._w(f':SENS:FUNC "{_SRC[other(fn)]}"')            # VERIFY
        self._check_errors("source function")

    def set_limit(self, fn: str, value: float) -> None:
        self._w(f":SOUR:{_SRC[fn]}:{_LIM[fn]} {abs(value):.9g}")   # VERIFY
        self._check_errors("limit")

    def set_level(self, fn: str, value: float) -> None:
        self._w(f":SOUR:{_SRC[fn]} {value:.9g}")              # VERIFY
        self._check_errors("level")

    def set_source_range(self, fn: str, auto: bool, value: float) -> None:
        self._src_auto[fn] = bool(auto)
        if auto:
            self._w(f":SOUR:{_SRC[fn]}:RANG:AUTO ON")          # VERIFY
        else:
            self._w(f":SOUR:{_SRC[fn]}:RANG:AUTO OFF")         # VERIFY
            self._w(f":SOUR:{_SRC[fn]}:RANG {abs(value):.9g}")  # VERIFY: snaps up
        self._check_errors("source range")

    def get_source_range(self, fn: str) -> float:
        return float(self._q(f":SOUR:{_SRC[fn]}:RANG?"))       # VERIFY

    # ---- measure ---------------------------------------------------------------
    def set_measure_range(self, mfn: str, auto: bool, value: float) -> None:
        self._meas_auto[mfn] = bool(auto)
        if auto:
            self._w(f":SENS:{_SRC[mfn]}:RANG:AUTO ON")         # VERIFY
        else:
            self._w(f":SENS:{_SRC[mfn]}:RANG:AUTO OFF")        # VERIFY
            self._w(f":SENS:{_SRC[mfn]}:RANG {abs(value):.9g}")  # VERIFY
        self._check_errors("measure range")

    def get_measure_range(self, mfn: str) -> float:
        return float(self._q(f":SENS:{_SRC[mfn]}:RANG?"))      # VERIFY

    def set_nplc(self, mfn: str, nplc: float) -> None:
        self._w(f":SENS:{_SRC[mfn]}:NPLC {nplc:.6g}")          # VERIFY
        self._check_errors("nplc")

    def set_four_wire(self, on: bool) -> None:
        # Remote sense is a per-measure-function setting on the 2450; set it on
        # both so switching the source function cannot silently drop it.
        for f in ("VOLT", "CURR"):
            self._w(f":SENS:{f}:RSEN {'ON' if on else 'OFF'}")   # VERIFY
        self._check_errors("remote sense")

    # ---- output and readings -----------------------------------------------------
    def set_output(self, on: bool) -> None:
        self._w(f":OUTP {'ON' if on else 'OFF'}")             # VERIFY
        if on:
            # A refused OUTP ON (interlock open above 42 V, a settings conflict)
            # shows up as an error in the queue, not as an exception.
            self._check_errors("output on")

    def get_output(self) -> bool:
        return self._q(":OUTP?").strip() in ("1", "ON")       # VERIFY

    def measure(self) -> Reading:
        fn, mfn = self._fn, other(self._fn)
        # One triggered reading, returned as "measured,source-readback".
        # VERIFY: buffer element order follows the list (READ, SOUR).
        raw = self._q(':READ? "defbuffer1", READ, SOUR')
        parts = [float(x) for x in raw.split(",")]
        measured, source = parts[0], parts[1]
        overflow = abs(measured) >= _OVERFLOW
        if overflow:
            measured = math.nan
        # VERIFY: "in compliance" query, :SOUR:VOLT:ILIM:TRIP? -> 0|1
        tripped = self._q(f":SOUR:{_SRC[fn]}:{_LIM[fn]}:TRIP?").strip() in ("1", "ON")
        mrange = self.get_measure_range(mfn) if self._meas_auto[mfn] else math.nan
        srange = self.get_source_range(fn) if self._src_auto[fn] else math.nan
        return Reading(measured=measured, source=source, tripped=tripped,
                       overflow=overflow, measure_range=mrange, source_range=srange)

    # ---- internals -----------------------------------------------------------------
    def _w(self, cmd: str) -> None:
        if self._inst is None:
            raise RuntimeError("2450 not open")
        self._inst.write(cmd)

    def _q(self, cmd: str) -> str:
        if self._inst is None:
            raise RuntimeError("2450 not open")
        return self._inst.query(cmd).strip()

    def _check_errors(self, what: str) -> None:
        """Drain the error queue; raise with every message found.

        SCPI instruments report a refused setting ONLY here -- the write itself
        "succeeds". Reading the queue after each group of writes is what turns
        a silently ignored command into an event the user sees.
        """
        errors = []
        for _ in range(10):
            reply = self._q(":SYST:ERR:NEXT?")                # VERIFY: '0,"No error"'
            code = reply.split(",", 1)[0].strip()
            if code in ("0", "+0"):
                break
            errors.append(reply)
        if errors:
            raise RuntimeError(f"2450 refused {what}: " + "; ".join(errors))


def _on(reply: str) -> bool:
    """SCPI booleans come back as 1/0 (sometimes ON/OFF)."""
    return reply.strip().strip('"').upper() in ("1", "ON")


def _func_from(reply: str) -> str:
    """'VOLT', '"CURR:DC"', 'RES' -> 'voltage' / 'current' / 'resistance'."""
    r = reply.strip().strip('"').upper()
    if r.startswith("VOLT"):
        return "voltage"
    if r.startswith("CURR"):
        return "current"
    if r.startswith("RES"):
        return "resistance"
    return r.lower() or "unknown"
