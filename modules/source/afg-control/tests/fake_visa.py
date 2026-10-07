"""A fake pyvisa + a fake AFG1062 that answers SCPI, for offline backend tests.

Only what backends/tek_afg.py sends is understood: the queries it makes and
the settings it writes (which change the fake's state, so a written value
reads back). The backend asks with write("...?") + read_raw(), as it does on
the real unit; a write ending in "?" is logged in `queries`, every other one
in `writes` -- so a test can prove that open() sets nothing.

Two FIRMWARE profiles:

  "manual"   -- everything the AFG1000 programmer manual lists answers
                cleanly (short-form shape names, phase in RADIANS, "9.9E+37"
                for an infinite load). What another firmware may do.
  "v1.0.2"   -- what the lab's unit (FV:V1.0.2) did on 2026-10-06, measured
                with read-only queries and SYST:ERR? after each:
                  * a query it does not know (FUNC:RAMP:SYMM?, VOLT:UNIT?,
                    ...) -> an EMPTY answer and -102,"Syntax error" queued;
                  * PULS:DCYC? -> the value AND -102 queued;
                  * OUTP:IMP? -> b'9.9E+37\\xa6\\xb8\\n' (a GBK Ohm sign);
                  * the first *IDN? after *CLS -> empty; the next one complete.

`error_log` keeps every error the fake ever queued, with the query that
caused it, so a test can tell which queries made errors.
"""

from __future__ import annotations

import math
import re
import types

import pytest

_SHAPES = {"SIN": "SIN", "SQU": "SQU", "PULS": "PULS", "RAMP": "RAMP",
           "PRN": "PRN", "DC": "DC"}

_SYNTAX = '-102,"Syntax error"'
_OHM_GBK = b"\xa6\xb8"


class FakeAFGInstrument:
    #: the profile new instruments get; the fixtures set it
    firmware = "manual"

    def __init__(self, resource: str, firmware: str | None = None):
        self.resource = resource
        self.firmware = firmware or type(self).firmware
        self.writes: list[str] = []
        self.queries: list[str] = []
        self.closed = False
        self.timeout = None
        self.write_termination = self.read_termination = None
        self.errors: list[str] = []
        self.error_log: list[tuple[str, str]] = []   # (query, error) ever queued
        self.dead = False                   # True: every read times out
        self.ren_calls: list[int] = []
        self._reply: bytes | None = None    # the answer waiting for read_raw
        self._idn_after_cls = False
        self.ch = {
            1: {"OUTP": "1", "SHAP": "SIN", "FREQ": 30.0, "AMPL": 2.0, "UNIT": "VPP",
                "OFFS": 0.0, "PHAS": 0.0, "DCYC": 50.0, "SYMM": 50.0, "IMP": 50.0,
                "BURS": "0", "MODE": "CW"},
            2: {"OUTP": "0", "SHAP": "SQU", "FREQ": 30.0, "AMPL": 1.5, "UNIT": "VPP",
                "OFFS": 0.75, "PHAS": math.pi / 2, "DCYC": 50.0, "SYMM": 50.0,
                "IMP": 9.9e37, "BURS": "0", "MODE": "CW"},
        }

    @property
    def measured(self) -> bool:
        return self.firmware == "v1.0.2"

    def _error(self, cmd: str, err: str = _SYNTAX) -> None:
        self.errors.append(err)
        self.error_log.append((cmd, err))

    # ---- pyvisa resource surface ----------------------------------------
    def write(self, cmd: str):
        if cmd.endswith("?"):
            self.queries.append(cmd)
            self._reply = self._answer(cmd)
            return
        self.writes.append(cmd)
        if cmd == "*CLS":
            self.errors.clear()
            self._idn_after_cls = True
            return
        m = re.match(r"(OUTP|SOUR)(\d):(\S+) (\S+)$", cmd)
        if not m:
            return
        root, n, path, arg = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        c = self.ch[n]
        if root == "OUTP" and path == "STAT":
            c["OUTP"] = "1" if arg == "ON" else "0"
        elif root == "OUTP" and path == "IMP":
            c["IMP"] = 9.9e37 if arg == "INF" else float(arg)
        elif path == "FUNC:SHAP":
            c["SHAP"] = _SHAPES[arg]
        elif path == "FREQ:FIX":
            c["FREQ"] = float(arg)
        elif path == "VOLT:LEV:IMM:AMPL":
            c["AMPL"] = float(arg.removesuffix("VPP"))
            c["UNIT"] = "VPP"
        elif path == "VOLT:LEV:IMM:OFFS":
            c["OFFS"] = float(arg)
        elif path == "PHAS:ADJ":
            # as MEASURED on the AFG1062 (2026-10-07): "<x>DEG" is degrees,
            # a bare number radians; the unit keeps whole degrees, TRUNCATED;
            # a negative (or >= 360) phase is rejected with -201
            deg = float(arg[:-3]) if arg.upper().endswith("DEG") else math.degrees(float(arg))
            if not 0.0 <= deg < 360.0:
                self.errors.append('-201,"Invalid while in local"')
            else:
                c["PHAS"] = math.radians(int(deg + 1e-9))
        elif path == "PULS:DCYC":
            c["DCYC"] = float(arg)
        elif path == "FUNC:RAMP:SYMM":
            # accepted here; on the real FV:V1.0.2 NOT TESTED (# VERIFY)
            c["SYMM"] = float(arg)

    def read_raw(self) -> bytes:
        if self.dead:
            raise TimeoutError("VI_ERROR_TMO")
        reply, self._reply = self._reply, None
        if reply is None:
            raise TimeoutError("VI_ERROR_TMO (nothing was asked)")
        return reply

    def _answer(self, cmd: str) -> bytes:
        if cmd == "*IDN?":
            if self.measured and self._idn_after_cls:
                self._idn_after_cls = False
                return b""                      # measured: first one after *CLS
            return b"TEKTRONIX,AFG1062,C000001,SCPI:99.0 FV:V1.0.2\n"
        self._idn_after_cls = False
        if cmd == "SYST:ERR?":
            return ((self.errors.pop(0) if self.errors else '0,"No error"') + "\n").encode()
        m = re.match(r"(OUTP|SOUR)(\d):(\S+)\?$", cmd)
        if not m:
            return self._unknown(cmd)
        root, n, path = m.group(1), int(m.group(2)), m.group(3)
        c = self.ch[n]
        if self.measured:
            if (root, path) == ("OUTP", "IMP"):
                return f"{c['IMP']:.1E}".encode() + _OHM_GBK + b"\n"
            if (root, path) in (("SOUR", "VOLT:UNIT"), ("SOUR", "FUNC:RAMP:SYMM")):
                return self._unknown(cmd)
            if (root, path) == ("SOUR", "PULS:DCYC"):
                self._error(cmd)                # answers AND complains
                return f"{c['DCYC']:g}\n".encode()
        table = {("OUTP", "STAT"): c["OUTP"], ("OUTP", "IMP"): f"{c['IMP']:.1E}",
                 ("SOUR", "FUNC:SHAP"): c["SHAP"], ("SOUR", "FREQ:FIX"): f"{c['FREQ']:.10E}",
                 ("SOUR", "VOLT:LEV:IMM:AMPL"): f"{c['AMPL']:.6E}",
                 ("SOUR", "VOLT:UNIT"): c["UNIT"],
                 ("SOUR", "VOLT:LEV:IMM:OFFS"): f"{c['OFFS']:.4E}",
                 ("SOUR", "PHAS:ADJ"): f"{c['PHAS']:.6E}",
                 ("SOUR", "PULS:DCYC"): f"{c['DCYC']:.2E}",
                 ("SOUR", "FUNC:RAMP:SYMM"): f"{c['SYMM']:.2E}",
                 ("SOUR", "BURS:STAT"): c["BURS"], ("SOUR", "FREQ:MODE"): c["MODE"]}
        if (root, path) in table:
            return (str(table[(root, path)]) + "\n").encode()
        if root == "SOUR" and path in ("AM:STAT", "FM:STAT", "PM:STAT", "FSK:STAT",
                                       "PWM:STAT"):
            return b"0\n"
        return self._unknown(cmd)

    def _unknown(self, cmd: str) -> bytes:
        """A query this firmware does not know: an empty answer + -102."""
        self._error(cmd)
        return b""

    def control_ren(self, mode):
        self.ren_calls.append(mode)

    def close(self):
        self.closed = True


def _install(monkeypatch, firmware: str) -> list:
    opened: list[FakeAFGInstrument] = []

    class RM:
        def open_resource(self, resource):
            inst = FakeAFGInstrument(resource, firmware)
            opened.append(inst)
            return inst

        def close(self):
            pass

    mod = types.SimpleNamespace(ResourceManager=RM)
    monkeypatch.setitem(__import__("sys").modules, "pyvisa", mod)
    return opened


@pytest.fixture
def fake_visa(monkeypatch):
    """A fake `pyvisa` whose AFG answers everything the manual lists; returns
    the list of instruments it opened."""
    return _install(monkeypatch, "manual")


@pytest.fixture
def fake_visa_v102(monkeypatch):
    """A fake `pyvisa` whose AFG behaves as the lab's unit (FV:V1.0.2) did."""
    return _install(monkeypatch, "v1.0.2")
