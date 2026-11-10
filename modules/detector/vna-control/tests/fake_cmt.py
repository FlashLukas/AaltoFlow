"""A FAKE VISA resource that behaves like S2VNA driving a Copper Mountain C1209,
as far as backends/cmt.py uses it.

No pyvisa, no instrument: it records every command in `log`, keeps the settings
the backend reads back, and follows the Copper Mountain manual where it matters:
  * TRIG:SING is refused (error queued, no sweep) unless the trigger source is
    BUS and the channel is waiting for a trigger;
  * *OPC? answers "1" once the triggered sweep is over;
  * SDAT comes back as interleaved (re, im) numbers, ASCII over a socket;
  * several traces on channel 1 (PAR:COUN, PAR<i>:DEF, TRAC<i>:DATA:SDAT?),
    all filled by the one triggered sweep. `sp_fn(sparam, n, k)` gives each
    S-parameter its own numbers, so a test can tell which trace it read; the
    default gives every trace z_fn's numbers (what the older tests expect).
Whether the real S2VNA agrees is the hardware pass (the # VERIFY lines).
"""

from __future__ import annotations

import re

import numpy as np


class FakeCmt:
    def __init__(self, idn="CMT,C1209,21000000,21.3.2/1.0", z_fn=None, snap_points=None,
                 sp_fn=None):
        self.log: list[str] = []
        self.timeout = None
        self.read_termination = None
        self.write_termination = None
        self.closed = False
        self.idn = idn
        self.state = {"start": 100e3, "stop": 9e9, "points": 201, "ifbw": 10e3, "power": 0.0}
        self.trig_source = "INT"
        self.cont = True
        self.fmt = "ASC"
        self.defs = {1: "S11", 2: "S21"}     # trace number -> what it shows
        self.traces = 2
        self.errors: list[str] = []
        self.sweeps = 0
        self.pending = False
        self.opc_timeouts: list = []          # the VISA timeout in force at each *OPC?
        self.snap_points = snap_points
        self.z_fn = z_fn or (lambda n, k: (np.arange(n) + 1.0) * (0.25 + 0.5j) * k)
        self.sp_fn = sp_fn or (lambda sp, n, k: self.z_fn(n, k))

    @property
    def sparam(self) -> str:
        """What trace 1 shows (the older tests' single S-parameter)."""
        return self.defs.get(1, "")

    # ---- the pyvisa Resource surface the backend uses ----------------------
    def write(self, cmd: str) -> None:
        self.log.append(cmd)
        if cmd == "*CLS":
            self.errors.clear()
        for key, pat, cast in (("start", r":SENS1:FREQ:STAR (\S+)", float),
                               ("stop", r":SENS1:FREQ:STOP (\S+)", float),
                               ("ifbw", r":SENS1:BWID (\S+)", float),
                               ("power", r":SOUR1:POW (\S+)", float)):
            m = re.fullmatch(pat, cmd)
            if m:
                self.state[key] = cast(m.group(1))
        m = re.fullmatch(r":SENS1:SWE:POIN (\S+)", cmd)
        if m:
            n = int(m.group(1))
            self.state["points"] = self.snap_points(n) if self.snap_points else n
        m = re.fullmatch(r":CALC1:PAR(\d+):DEF (\w+)", cmd)
        if m:
            i = int(m.group(1))
            if m.group(2) not in ("S11", "S12", "S21", "S22") or i > self.traces:
                # no such trace (count too small) or no such parameter
                self.errors.append('-224,"Illegal parameter value"')
            else:
                self.defs[i] = m.group(2)
        m = re.fullmatch(r":CALC1:PAR:COUN (\d+)", cmd)
        if m:
            self.traces = int(m.group(1))
            # S2VNA keeps the traces that remain; new ones default to S11
            self.defs = {i: self.defs.get(i, "S11") for i in range(1, self.traces + 1)}
        m = re.fullmatch(r":TRIG:SOUR (\w+)", cmd)
        if m:
            self.trig_source = m.group(1)
        m = re.fullmatch(r":INIT1:CONT (\w+)", cmd)
        if m:
            self.cont = m.group(1) in ("ON", "1")
        m = re.fullmatch(r":FORM:DATA (\w+)", cmd)
        if m:
            self.fmt = m.group(1)
        if cmd == ":TRIG:SING":
            # manual: needs source BUS and the channel waiting for a trigger
            if self.trig_source != "BUS" or not self.cont or self.pending:
                self.errors.append('-211,"Trigger ignored"')
            else:
                self.sweeps += 1
                self.pending = True
        if cmd == ":ABOR":
            if self.pending:
                # what the real S2VNA does (Blue Box, 2026-11-10): an aborted
                # pending single trigger is logged as an error
                self.errors.append('-239,"TRIG:SING interrupted"')
            self.pending = False

    def query(self, cmd: str) -> str:
        self.log.append(cmd)
        if cmd == "*IDN?":
            return self.idn + "\n"
        if cmd == ":SYST:ERR?":
            return self.errors.pop(0) if self.errors else "0, No error"
        if cmd == "*OPC?":
            self.opc_timeouts.append(self.timeout)
            self.pending = False                  # the sweep ran to its end
            return "1"
        simple = {":SENS1:FREQ:STAR?": "start", ":SENS1:FREQ:STOP?": "stop",
                  ":SENS1:BWID?": "ifbw", ":SOUR1:POW?": "power"}
        if cmd in simple:
            return f"{self.state[simple[cmd]]:.12E}"
        if cmd == ":SENS1:SWE:POIN?":
            return f"{self.state['points']}"
        if cmd == ":SENS1:CORR:STAT?":
            return "0"
        if cmd == ":CALC1:PAR1:DEF?":
            return self.sparam
        if cmd == ":CALC1:PAR:COUN?":
            return f"{self.traces}"
        if cmd == ":TRIG:SOUR?":
            return self.trig_source
        if cmd == ":SENS1:AVER?":
            return "0"
        raise AssertionError(f"FakeCmt: unexpected query {cmd!r}")

    def _interleaved(self, cmd: str) -> np.ndarray:
        m = re.fullmatch(r":CALC1:TRAC(\d+):DATA:SDAT\?", cmd)
        assert m, cmd
        i = int(m.group(1))
        assert 1 <= i <= self.traces, f"trace {i} read, but only {self.traces} exist"
        z = np.asarray(self.sp_fn(self.defs[i], self.state["points"], self.sweeps),
                       dtype=complex)
        out = np.empty(2 * z.size)
        out[0::2], out[1::2] = z.real, z.imag
        return out

    def query_ascii_values(self, cmd, container=list):
        self.log.append(cmd)
        assert self.fmt == "ASC", "ASCII data asked for, but FORM:DATA is " + self.fmt
        return container(self._interleaved(cmd))

    def query_binary_values(self, cmd, datatype="f", is_big_endian=False, container=list):
        self.log.append(cmd)
        assert datatype == "d" and is_big_endian is False, (datatype, is_big_endian)
        return container(self._interleaved(cmd))

    def close(self) -> None:
        self.closed = True
