"""A fake pyvisa + a fake Siglent SDS1000CML+ that answers the legacy command
set, for offline tests of backends/siglent.py.

It has a real OUTPUT QUEUE, like the instrument: a query puts its reply into
it, a read takes bytes out -- up to the first "\n" while the resource's
read_termination is "\n", all of the queued message otherwise. That is what
reproduces the lab-PC bug of 2026-10-06: a waveform block whose int8 data
holds a 0x0A, read with the terminator on, leaves the rest queued and every
later reply out of step. Replies use the forms measured on the lab's
RSDS1102CML+ (SI prefixes: "SARA 500.0KSa", "TRDL 0.00us", "TDIV 1.00E-03s";
TRSE with a holdoff).

It answers the way the programming guide says (headers included: "C1:VDIV
5.00E-01V"), keeps the settings the backend writes, and serves a waveform as
the binary block "C1:WF DAT2,#9<len><int8 codes>\\n\\n". Every write is logged,
so a test can prove open() and read_settings() write nothing. Where the real
scope may differ is exactly the # VERIFY list -- the lab-PC checklist catches
that, not these tests.
"""

from __future__ import annotations

import re
import types

import numpy as np
import pytest


# Faults to inject, shared by every instrument the fake opens (a reopened
# session is a new object): "idn" = how many *IDN? queries still fail with
# the lab's VI_ERROR_INP_PROT_VIOL. Reset by the fixture.
FAULTS = {"idn": 0}


class InpProtViol(OSError):
    """What pyvisa raised on the lab PC (a VisaIOError) after the services
    had vanished mid-scan."""

    def __init__(self):
        super().__init__("VI_ERROR_INP_PROT_VIOL (-1073807305): Device reported "
                         "an input protocol error during transfer.")


class FakeSDS:
    def __init__(self, resource):
        self.resource = resource
        self.writes: list[str] = []
        self.queries: list[str] = []      # query() calls (writes holds only write())
        self.closed = False
        self.timeout = None
        self.write_termination = self.read_termination = None
        self.dead = False
        self.ren = []
        self.st = {"C1": {"TRA": "ON", "VDIV": 0.5, "OFST": 0.0, "CPL": "D1M", "ATTN": 1.0},
                   "C2": {"TRA": "ON", "VDIV": 0.1, "OFST": -0.5, "CPL": "A1M", "ATTN": 10.0},
                   "TDIV": 5e-3, "TRDL": 0.0, "SARA": 1e5, "SANU": 14000,
                   # points in the block WF? actually sends -- on the real scope
                   # MORE than SANU says (8000 vs 20480 at 1 ms/div, lab PC)
                   "MEM": 14000,
                   "TRSE": "EDGE,SR,EX,HT,TI,HV,100NS", "TRMD": "NORM",
                   "TRLV": {"C1": 0.0, "C2": 0.0, "EX": 0.5, "EX5": 0.5, "LINE": 0.0},
                   "TRSL": {"C1": "POS", "C2": "POS", "EX": "POS", "EX5": "POS", "LINE": "POS"},
                   "INR": 1, "WFSU": None}
        self._out = bytearray(FakeSDS.stale)   # what an earlier client left behind
        self.clear_calls = 0

    #: bytes queued before the session opened (a crashed client's leftovers)
    stale = b""

    # ---- pyvisa resource surface ----------------------------------------------
    def write(self, cmd: str):
        self.writes.append(cmd)
        self._handle(cmd)

    def _handle(self, cmd: str):
        st = self.st
        m = re.match(r"(C[12]):WF\? DAT2$", cmd)
        if m:
            self._out += self._waveform(m.group(1))
            return
        if cmd.endswith("?") or "? " in cmd:
            self._out += self._reply(cmd).encode("latin-1") + b"\n"
            return
        m = re.match(r"(C[12]):(TRA|VDIV|OFST|CPL|ATTN) (\S+)$", cmd)
        if m:
            ch, key, arg = m.groups()
            st[ch][key] = arg if key in ("TRA", "CPL") else float(arg.rstrip("V"))
            return
        m = re.match(r"(TDIV|TRDL) (\S+)$", cmd)
        if m:
            st[m.group(1)] = float(m.group(2).rstrip("S"))
            return
        m = re.match(r"TRSE (\S+)$", cmd)
        if m:
            st["TRSE"] = m.group(1)
            return
        m = re.match(r"(C1|C2|EX5|EX|LINE):(TRLV|TRSL) (\S+)$", cmd)
        if m:
            src, key, arg = m.groups()
            st[key][src] = float(arg.rstrip("V")) if key == "TRLV" else arg
            return
        m = re.match(r"TRMD (\S+)$", cmd)
        if m:
            st["TRMD"] = m.group(1)
            return
        m = re.match(r"WFSU (.*)$", cmd)
        if m:
            st["WFSU"] = m.group(1)

    def query(self, cmd: str) -> str:
        if self.dead:
            raise TimeoutError("VI_ERROR_TMO")
        self.queries.append(cmd)
        if cmd == "*IDN?" and FAULTS["idn"] > 0:
            FAULTS["idn"] -= 1
            raise InpProtViol()
        self._handle(cmd)
        return self.read_raw().decode("latin-1").rstrip("\n")

    def _reply(self, cmd: str) -> str:
        st = self.st
        if cmd == "*IDN?":
            return "Siglent Technologies,SDS1102CML+,SDS00000000001,1.01.01.25"
        m = re.match(r"(C[12]):(TRA|VDIV|OFST|CPL|ATTN)\?$", cmd)
        if m:
            ch, key = m.groups()
            v = st[ch][key]
            unit = "V" if key in ("VDIV", "OFST") else ""
            return f"{ch}:{key} {v:.2E}{unit}" if isinstance(v, float) else f"{ch}:{key} {v}"
        if cmd == "TDIV?":
            return f"TDIV {st['TDIV']:.2E}s"
        if cmd == "TRDL?":
            return f"TRDL {st['TRDL'] * 1e6:.2f}us"
        if cmd == "SARA?":
            return f"SARA {st['SARA'] / 1e3:.1f}KSa"
        m = re.match(r"SANU\? (C[12])$", cmd)
        if m:
            return f"SANU {st['SANU']}"
        if cmd == "TRSE?":
            return f"TRSE {st['TRSE']}"
        if cmd == "TRMD?":
            return f"TRMD {st['TRMD']}"
        m = re.match(r"(C1|C2|EX5|EX|LINE):(TRLV|TRSL)\?$", cmd)
        if m:
            src, key = m.groups()
            v = st[key][src]
            return f"{src}:{key} {v:.2E}V" if key == "TRLV" else f"{src}:{key} {v}"
        if cmd == "INR?":
            v, st["INR"] = st["INR"], 0               # read-and-clear
            return f"INR {v}"
        raise TimeoutError(f"unknown query {cmd}")

    def read_raw(self) -> bytes:
        if not self._out:
            raise TimeoutError("VI_ERROR_TMO: nothing to read")
        if self.read_termination:
            i = self._out.find(self.read_termination.encode())
            n = len(self._out) if i < 0 else i + 1
        else:
            n = len(self._out)                   # the whole message (EOI)
        chunk = bytes(self._out[:n])
        del self._out[:n]
        return chunk

    def clear(self):
        # the lab's scope answers viClear with VI_ERROR_SYSTEM_ERROR
        self.clear_calls += 1
        raise OSError("VI_ERROR_SYSTEM_ERROR")

    def _waveform(self, ch: str) -> bytes:
        sp = 1
        if self.st["WFSU"]:
            sp = int(self.st["WFSU"].split(",")[1])
        n = self.st["MEM"] // sp
        # a sine of +-2 divisions: codes +-50 -- which includes the code 10,
        # i.e. the byte 0x0A in the middle of the block (the lab-PC bug)
        codes = np.round(50 * np.sin(np.linspace(0, 4 * np.pi, n, endpoint=False))).astype(np.int8)
        # a NEW record (INR's flag set by the test = "the scope triggered")
        # has new content: the module tells records apart by it, INR? blocks
        # ~0.5 s on the real scope while it runs
        if ch == "C1" and self.st.get("INR") and not self.st.get("REC_SAME"):
            self.st["INR"] = 0
            self.st["REC"] = self.st.get("REC", 0) + 1
        codes = np.roll(codes, self.st.get("REC", 0))
        body = codes.tobytes()
        assert b"\n" in body
        return f"{ch}:WF DAT2,#9{len(body):09d}".encode() + body + b"\t\n\n"

    def control_ren(self, mode):
        self.ren.append(mode)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_visa(monkeypatch):
    """Install a fake `pyvisa`; returns the list of instruments it opened."""
    opened: list[FakeSDS] = []
    FAULTS["idn"] = 0

    class RM:
        def open_resource(self, resource):
            inst = FakeSDS(resource)
            opened.append(inst)
            return inst

        def close(self):
            pass

    monkeypatch.setitem(__import__("sys").modules, "pyvisa",
                        types.SimpleNamespace(ResourceManager=RM))
    return opened
