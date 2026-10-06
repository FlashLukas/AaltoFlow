"""A fake pyvisa + a fake AFG1062 that answers SCPI, for offline backend tests.

Only what backends/tek_afg.py sends is understood: the queries it makes and
the settings it writes (which change the fake's state, so a written value
reads back). Every line written is logged, so a test can prove that open()
sends nothing but *CLS and queries.

The answers follow the AFG3000/AFG1000 conventions the backend assumes
(short-form shape names, phase in RADIANS, "9.9E+37" for an infinite load) --
which are exactly the # VERIFY items: if the real unit differs, these tests
still pass and the lab-PC checklist (README) is what catches it.
"""

from __future__ import annotations

import math
import re
import types

import pytest

_SHAPES = {"SIN": "SIN", "SQU": "SQU", "PULS": "PULS", "RAMP": "RAMP",
           "PRN": "PRN", "DC": "DC"}


class FakeAFGInstrument:
    def __init__(self, resource: str):
        self.resource = resource
        self.writes: list[str] = []
        self.queries: list[str] = []
        self.closed = False
        self.timeout = None
        self.write_termination = self.read_termination = None
        self.errors: list[str] = []
        self.dead = False                   # True: every query times out
        self.ren_calls: list[int] = []
        self.ch = {
            1: {"OUTP": "1", "SHAP": "SIN", "FREQ": 30.0, "AMPL": 2.0, "UNIT": "VPP",
                "OFFS": 0.0, "PHAS": 0.0, "DCYC": 50.0, "SYMM": 50.0, "IMP": 50.0,
                "BURS": "0", "MODE": "CW"},
            2: {"OUTP": "0", "SHAP": "SQU", "FREQ": 30.0, "AMPL": 1.5, "UNIT": "VPP",
                "OFFS": 0.75, "PHAS": math.pi / 2, "DCYC": 50.0, "SYMM": 50.0,
                "IMP": 9.9e37, "BURS": "0", "MODE": "CW"},
        }

    # ---- pyvisa resource surface ----------------------------------------
    def write(self, cmd: str):
        self.writes.append(cmd)
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
            c["PHAS"] = float(arg)
        elif path == "PULS:DCYC":
            c["DCYC"] = float(arg)
        elif path == "FUNC:RAMP:SYMM":
            c["SYMM"] = float(arg)

    def query(self, cmd: str) -> str:
        self.queries.append(cmd)
        if self.dead:
            raise TimeoutError("VI_ERROR_TMO")
        if cmd == "*IDN?":
            return "TEKTRONIX,AFG1062,C000001,SCPI:99.0 FV:V1.2.3\n"
        if cmd == "SYST:ERR?":
            return self.errors.pop(0) if self.errors else '0,"No error"'
        m = re.match(r"(OUTP|SOUR)(\d):(\S+)\?$", cmd)
        if not m:
            raise TimeoutError(f"unknown query {cmd}")
        root, n, path = m.group(1), int(m.group(2)), m.group(3)
        c = self.ch[n]
        table = {("OUTP", "STAT"): c["OUTP"], ("OUTP", "IMP"): f"{c['IMP']:.1E}",
                 ("SOUR", "FUNC:SHAP"): c["SHAP"], ("SOUR", "FREQ:FIX"): f"{c['FREQ']:.10E}",
                 ("SOUR", "VOLT:LEV:IMM:AMPL"): f"{c['AMPL']:.4E}",
                 ("SOUR", "VOLT:UNIT"): c["UNIT"],
                 ("SOUR", "VOLT:LEV:IMM:OFFS"): f"{c['OFFS']:.4E}",
                 ("SOUR", "PHAS:ADJ"): f"{c['PHAS']:.6E}",
                 ("SOUR", "PULS:DCYC"): f"{c['DCYC']:.2E}",
                 ("SOUR", "FUNC:RAMP:SYMM"): f"{c['SYMM']:.2E}",
                 ("SOUR", "BURS:STAT"): c["BURS"], ("SOUR", "FREQ:MODE"): c["MODE"]}
        if (root, path) in table:
            return table[(root, path)]
        if path.endswith(":STAT"):                    # AM/FM/PM/FSK/PWM
            return "0"
        raise TimeoutError(f"unknown query {cmd}")

    def control_ren(self, mode):
        self.ren_calls.append(mode)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_visa(monkeypatch):
    """Install a fake `pyvisa`; returns the list of instruments it opened."""
    opened: list[FakeAFGInstrument] = []

    class RM:
        def open_resource(self, resource):
            inst = FakeAFGInstrument(resource)
            opened.append(inst)
            return inst

        def close(self):
            pass

    mod = types.SimpleNamespace(ResourceManager=RM)
    monkeypatch.setitem(__import__("sys").modules, "pyvisa", mod)
    return opened
