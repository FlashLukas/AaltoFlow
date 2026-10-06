"""A fake pyvisa + a fake Siglent SDS1000CML+ that answers the legacy command
set, for offline tests of backends/siglent.py.

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


class FakeSDS:
    def __init__(self, resource):
        self.resource = resource
        self.writes: list[str] = []
        self.closed = False
        self.timeout = None
        self.write_termination = self.read_termination = None
        self.dead = False
        self.ren = []
        self.st = {"C1": {"TRA": "ON", "VDIV": 0.5, "OFST": 0.0, "CPL": "D1M", "ATTN": 1.0},
                   "C2": {"TRA": "ON", "VDIV": 0.1, "OFST": -0.5, "CPL": "A1M", "ATTN": 10.0},
                   "TDIV": 5e-3, "TRDL": 0.0, "SARA": 1e5, "SANU": 14000,
                   "TRSE": "EDGE,SR,EX,HT,OFF", "TRMD": "NORM",
                   "TRLV": {"C1": 0.0, "C2": 0.0, "EX": 0.5, "EX5": 0.5, "LINE": 0.0},
                   "TRSL": {"C1": "POS", "C2": "POS", "EX": "POS", "EX5": "POS", "LINE": "POS"},
                   "INR": 1, "WFSU": None}
        self._pending_raw = None

    # ---- pyvisa resource surface ----------------------------------------------
    def write(self, cmd: str):
        self.writes.append(cmd)
        st = self.st
        m = re.match(r"(C[12]):WF\? DAT2$", cmd)
        if m:
            self._pending_raw = self._waveform(m.group(1))
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
        st = self.st
        if cmd == "*IDN?":
            return "Siglent Technologies,SDS1102CML+,SDS00000000001,1.01.01.25"
        m = re.match(r"(C[12]):(TRA|VDIV|OFST|CPL|ATTN)\?$", cmd)
        if m:
            ch, key = m.groups()
            v = st[ch][key]
            unit = "V" if key in ("VDIV", "OFST") else ""
            return f"{ch}:{key} {v:.2E}{unit}" if isinstance(v, float) else f"{ch}:{key} {v}"
        if cmd in ("TDIV?", "TRDL?"):
            return f"{cmd[:-1]} {st[cmd[:-1]]:.2E}S"
        if cmd == "SARA?":
            return f"SARA {st['SARA']:.2E}Sa/s"
        m = re.match(r"SANU\? (C[12])$", cmd)
        if m:
            return f"SANU {st['SANU']:.2E}"
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
        raw, self._pending_raw = self._pending_raw, None
        if raw is None:
            raise TimeoutError("nothing to read")
        return raw

    def _waveform(self, ch: str) -> bytes:
        sp = 1
        if self.st["WFSU"]:
            sp = int(self.st["WFSU"].split(",")[1])
        n = self.st["SANU"] // sp
        # a sine of +-2 divisions: codes +-50
        codes = np.round(50 * np.sin(np.linspace(0, 4 * np.pi, n, endpoint=False))).astype(np.int8)
        body = codes.tobytes()
        return f"{ch}:WF DAT2,#9{len(body):09d}".encode() + body + b"\n\n"

    def control_ren(self, mode):
        self.ren.append(mode)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_visa(monkeypatch):
    """Install a fake `pyvisa`; returns the list of instruments it opened."""
    opened: list[FakeSDS] = []

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
