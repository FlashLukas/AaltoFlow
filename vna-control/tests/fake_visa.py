"""A FAKE VISA resource that behaves like the PNA-X, as far as the backend uses it.

No pyvisa, no instrument: it records every command in `log`, keeps the few
settings the backend reads back, and serves SDATA as interleaved (re, im)
doubles. What it pretends is what the Keysight SCPI reference says -- so these
tests pin the SEQUENCE the backend sends and the PARSING of what comes back.
Whether the real firmware agrees is the hardware pass (the # VERIFY lines).
"""

from __future__ import annotations

import re

import numpy as np


class FakePna:
    def __init__(self, sweep_polls: int = 2, catalog: str = '"CH1_S11_1,S11"',
                 snap_points=None, z_fn=None):
        self.log: list[str] = []
        self.timeout = None
        self.read_termination = None
        self.write_termination = None
        self.closed = False
        self.state = {"start": 10e6, "stop": 26.5e9, "points": 201, "ifbw": 100e3,
                      "power": -5.0}
        self.mode = "CONT"
        self.catalog = catalog
        self.sparam = None
        self.errors: list[str] = []          # queued "code,message" strings
        self.sweep_polls = sweep_polls
        self._polls_left = 0
        self.sweeps = 0
        self.snap_points = snap_points       # e.g. lambda n: n - 1, to refuse a grid
        # the trace served for the k-th sweep (k = 1, 2, ...)
        self.z_fn = z_fn or (lambda n, k: (np.arange(n) + 1.0) * (0.25 + 0.5j) * k)

    # ---- the pyvisa Resource surface the backend uses ----------------------
    def write(self, cmd: str) -> None:
        self.log.append(cmd)
        m = re.fullmatch(r"SENS1:FREQ:STAR (\S+)", cmd)
        if m:
            self.state["start"] = float(m.group(1))
        m = re.fullmatch(r"SENS1:FREQ:STOP (\S+)", cmd)
        if m:
            self.state["stop"] = float(m.group(1))
        m = re.fullmatch(r"SENS1:SWE:POIN (\S+)", cmd)
        if m:
            n = int(m.group(1))
            self.state["points"] = self.snap_points(n) if self.snap_points else n
        m = re.fullmatch(r"SENS1:BAND (\S+)", cmd)
        if m:
            self.state["ifbw"] = float(m.group(1))
        m = re.fullmatch(r"SOUR1:POW1:LEV:IMM:AMPL (\S+)", cmd)
        if m:
            self.state["power"] = float(m.group(1))
        m = re.fullmatch(r"SENS1:SWE:MODE (\w+)", cmd)
        if m:
            self.mode = m.group(1)
            if self.mode == "SING":
                self.sweeps += 1
                self._polls_left = self.sweep_polls
        m = re.fullmatch(r"CALC1:PAR:DEF:EXT '(\w+)','(S\d\d)'", cmd)
        if m:
            self.catalog = self.catalog.rstrip('"') + f',{m.group(1)},{m.group(2)}"'
            self.sparam = m.group(2)
        m = re.fullmatch(r"CALC1:PAR:MOD:EXT '(S\d\d)'", cmd)
        if m:
            self.sparam = m.group(1)

    def query(self, cmd: str) -> str:
        self.log.append(cmd)
        if cmd == "*IDN?":
            return "Keysight Technologies,N5222A,MY00000000,A.00.00.00\n"
        if cmd == "SYST:ERR?":
            return self.errors.pop(0) if self.errors else '+0,"No error"'
        simple = {"SENS1:FREQ:STAR?": "start", "SENS1:FREQ:STOP?": "stop",
                  "SENS1:BAND?": "ifbw", "SOUR1:POW1:LEV:IMM:AMPL?": "power"}
        if cmd in simple:
            return f"{self.state[simple[cmd]]:+.12E}"
        if cmd == "SENS1:SWE:POIN?":
            return f"+{self.state['points']}"
        if cmd == "SENS1:SWE:TIME?":
            return "+5.000000000E-002"
        if cmd == "SENS1:CORR:STAT?":
            return "0"
        if cmd == "CALC1:PAR:CAT:EXT?":
            return self.catalog
        if cmd == "DISP:WIND1:TRAC:NEXT?":
            return "+2"
        if cmd == "SENS1:SWE:MODE?":
            if self.mode == "SING":
                if self._polls_left > 0:
                    self._polls_left -= 1
                    return "SING"
                self.mode = "HOLD"               # the single sweep finished
            return self.mode
        raise AssertionError(f"FakePna: unexpected query {cmd!r}")

    def _interleaved(self) -> np.ndarray:
        z = np.asarray(self.z_fn(self.state["points"], self.sweeps), dtype=complex)
        out = np.empty(2 * z.size)
        out[0::2], out[1::2] = z.real, z.imag
        return out

    def query_binary_values(self, cmd, datatype="f", is_big_endian=False, container=list):
        self.log.append(cmd)
        assert datatype == "d" and is_big_endian is False, (datatype, is_big_endian)
        return container(self._interleaved())

    def query_ascii_values(self, cmd, container=list):
        self.log.append(cmd)
        return container(self._interleaved())

    def close(self) -> None:
        self.closed = True
