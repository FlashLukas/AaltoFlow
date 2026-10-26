"""A FAKE VISA resource that behaves like LibreVNA-GUI or Harogic VNStudio, as
far as backends/librevna.py uses them.

No pyvisa, no instrument: it records every command in `log`, keeps the settings
the backend reads back, and follows the two SCPI guides where it matters:
  * VNA:ACQ:SINGLE TRUE starts a sweep; it takes `sweep_polls` "done?" queries
    to end (LibreVNA: VNA:ACQ:FIN? FALSE -> TRUE; VNStudio: VNA:ACQ:RUN? TRUE ->
    FALSE);
  * VNA:TRAC:DATA? answers "[f,re,im],[f,re,im],..." on ONE line;
  * *ESR? returns the event register and clears it; a failed command can also
    leave a text line (VNStudio's GATEWAY_EXECUTION_ERROR) in front of it.
Whether the real programs agree is the hardware pass (the # VERIFY lines).
"""

from __future__ import annotations

import re

import numpy as np


class FakeSweepServer:
    def __init__(self, dialect="librevna", idn=None, connected=True, z_fn=None,
                 snap_points=None, sweep_polls=2, freq_offset_Hz=0.0):
        self.dialect = dialect
        self.log: list[str] = []
        self.timeout = None
        self.read_termination = None
        self.write_termination = None
        self.closed = False
        self.idn = idn or ("LibreVNA,LibreVNA-GUI,TEST00000001,1.6.1" if dialect == "librevna"
                           else "Harogic,PNB-60,TEST00000002,1.6.5")
        self.connected = connected
        self.state = {"start": 1e6, "stop": 6e9, "points": 501, "ifbw": 10e3, "power": -10.0}
        self.single = False
        self.sweep_mode = "FREQUENCY"
        self.avg = 3
        self.traces = {"S11": "S11", "S12": "S12", "S21": "S21", "S22": "S22"}
        self.esr = 0
        self.unsolicited: list[str] = []      # lines waiting in the socket
        self._next = None
        self.sweeps = 0
        self.polls_left = 0
        self.sweep_polls = sweep_polls
        self.snap_points = snap_points
        self.freq_offset_Hz = freq_offset_Hz
        self.z_fn = z_fn or (lambda n, k: (np.arange(n) + 1.0) * (0.25 + 0.5j) * k)

    # ---- the pyvisa Resource surface the backend uses ----------------------
    def write(self, cmd: str) -> None:
        self.log.append(cmd)
        for key, pat in (("start", r"VNA:FREQ:START (\S+)"), ("stop", r"VNA:FREQ:STOP (\S+)"),
                         ("ifbw", r"VNA:ACQ:IFBW (\S+)"), ("power", r"VNA:STIM:LVL (\S+)")):
            m = re.fullmatch(pat, cmd)
            if m:
                self.state[key] = float(m.group(1))
                self._restart()
                return
        m = re.fullmatch(r"VNA:ACQ:POINTS (\S+)", cmd)
        if m:
            n = int(m.group(1))
            self.state["points"] = self.snap_points(n) if self.snap_points else n
            self._restart()
            return
        m = re.fullmatch(r"VNA:TRAC:NEW (\S+)", cmd)
        if m and self.dialect == "librevna":
            self.traces[m.group(1)] = "S11"
            return
        m = re.fullmatch(r"VNA:TRAC:PARAM (\S+) (\S+)", cmd)
        if m and self.dialect == "librevna":
            if m.group(1) not in self.traces or m.group(2) not in ("S11", "S12", "S21", "S22"):
                self.esr |= 0x10
            else:
                self.traces[m.group(1)] = m.group(2)
            return
        if cmd == "VNA:ACQ:SINGLE TRUE":
            self.single = True
            self.sweeps += 1
            self.polls_left = self.sweep_polls
            return
        if cmd == "VNA:ACQ:SINGLE FALSE":
            self.single = False
            return
        m = re.fullmatch(r"VNA:ACQ:AVG (\d+)", cmd)
        if m:
            self.avg = int(m.group(1))
            return
        if cmd in ("VNA:SWEEP FREQUENCY", "VNA:SWEEPTYPE LIN", "DEV:MODE VNA"):
            if cmd == "VNA:SWEEPTYPE LIN" and self.dialect != "harogic":
                self.esr |= 0x20
            if cmd == "DEV:MODE VNA" and self.dialect != "librevna":
                self.esr |= 0x20
            return
        # anything else: a command error, as the real programs would flag it
        self.esr |= 0x20
        if self.dialect == "harogic":
            self.unsolicited.append("GATEWAY_EXECUTION_ERROR")

    def _restart(self):
        if self.polls_left:
            self.polls_left = self.sweep_polls

    def query(self, cmd: str) -> str:
        self.log.append(cmd)
        if self.unsolicited:
            # a reply nobody read is still in the socket: it comes out first
            line = self.unsolicited.pop(0)
            self._next = self._answer(cmd)
            return line
        return self._answer(cmd)

    def read(self) -> str:
        out, self._next = self._next, None
        return out

    def _answer(self, cmd: str) -> str:
        if cmd == "*IDN?":
            return self.idn
        if cmd == "*ESR?":
            v, self.esr = self.esr, 0
            return str(v)
        if cmd == "DEV:CONN?":
            if self.connected:
                return "TEST00000001"
            return "Not connected" if self.dialect == "librevna" else "SERVER_NOT_READY"
        simple = {"VNA:FREQ:START?": "start", "VNA:FREQ:STOP?": "stop",
                  "VNA:ACQ:IFBW?": "ifbw", "VNA:STIM:LVL?": "power"}
        if cmd in simple:
            return f"{self.state[simple[cmd]]:g}"
        if cmd == "VNA:ACQ:POINTS?":
            return str(self.state["points"])
        if cmd == "VNA:SWEEP?":
            return self.sweep_mode
        if cmd == "VNA:ACQ:SINGLE?":
            return "TRUE" if self.single else "FALSE"
        if cmd == "VNA:ACQ:AVG?":
            return str(self.avg)
        if cmd == "VNA:TRAC:LIST?" and self.dialect == "librevna":
            return ",".join(self.traces)
        m = re.fullmatch(r"VNA:TRAC:PARAM\? (\S+)", cmd)
        if m and self.dialect == "librevna":
            return self.traces[m.group(1)]
        if cmd == "VNA:ACQ:FIN?" and self.dialect == "librevna":
            return "TRUE" if self._poll() else "FALSE"
        if cmd == "VNA:ACQ:RUN?" and self.dialect == "harogic":
            return "FALSE" if self._poll() else "TRUE"
        if cmd == "VNA:ACQ:SWE:TIME?" and self.dialect == "harogic":
            return "0.1801731"
        limits = {"DEV:INF:LIM:MINF?": "100000", "DEV:INF:LIM:MAXF?": "6e+09",
                  "DEV:INF:LIM:MAXP?": "4501", "DEV:INF:LIM:MINIFBW?": "10",
                  "DEV:INF:LIM:MAXIFBW?": "75000", "DEV:INF:LIM:MINPOW?": "-42",
                  "DEV:INF:LIM:MAXPOW?": "5"}
        if cmd in limits:
            return limits[cmd]
        m = re.fullmatch(r"VNA:TRAC:DATA\? (\S+)", cmd)
        if m:
            name = m.group(1)
            if name not in self.traces:
                return "ERROR"
            return self._trace_text()
        raise AssertionError(f"FakeSweepServer({self.dialect}): unexpected query {cmd!r}")

    def _poll(self) -> bool:
        if self.polls_left > 0:
            self.polls_left -= 1
        return self.polls_left == 0

    def _trace_text(self) -> str:
        n = self.state["points"]
        f = np.linspace(self.state["start"], self.state["stop"], n) + self.freq_offset_Hz
        z = np.asarray(self.z_fn(n, self.sweeps), dtype=complex)
        return ",".join(f"[{fi:.10g},{zi.real:.10g},{zi.imag:.10g}]" for fi, zi in zip(f, z))

    def close(self) -> None:
        self.closed = True
