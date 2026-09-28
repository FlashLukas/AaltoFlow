"""A pretend GSP-818 behind a pyvisa-like interface (write / query / close),
so the real backend's SCPI can be tested offline. It answers the commands
backends/gsp.py sends, in the formats the programming manual gives -- which is
exactly what is NOT yet verified on the instrument, so this fake is a record
of our reading of the manual, not proof.

`state` is the pretend front panel (SCPI header without the leading colon ->
the reply text). Pass overrides to start it somewhere NON-default, as a real
instrument left by the previous user would be. `read_only = True` makes every
write raise: that is how the tests prove start-up sends queries only."""

from __future__ import annotations

import math

#: dBm -> the amplitude unit's number (50 ohm), the inverse of gsp._to_dBm
_OFFSET = {"DBM": 0.0,
           "DBMV": 30.0 + 10 * math.log10(50.0),       # 46.99
           "DBUV": 90.0 + 10 * math.log10(50.0)}       # 106.99


def dbm_to_unit(dbm: float, unit: str) -> float:
    if unit in _OFFSET:
        return dbm + _OFFSET[unit]
    if unit == "V":
        return 10 ** ((dbm - 30.0 + 10 * math.log10(50.0)) / 20.0)
    return 10 ** ((dbm - 30.0) / 10.0)                 # W


PRESET = {
    "FREQ:STAR": "9000", "FREQ:STOP": "1800000000", "SWE:POIN": "601",
    "BAND:AUTO": "1", "BAND": "3000000", "BAND:VID:AUTO": "1", "BAND:VID": "3000000",
    "DISP:WIN:TRAC:Y:RLEV": "0.00", "POW:ATT:AUTO": "1", "POW:ATT": "10",
    "POW:GAIN:AUTO": "0", "SWE:TIME:AUTO": "1", "SWE:TIME": "20.000", "DET": "AUTO",
    "SOUR:POW:TRAC": "-10.0", "OUTP:TRAC": "OFF", "UNIT:POW": "DBM",
    "TRAC1:MODE": "WRIT", "AVER": "OFF", "INIT:CONT": "ON",
}


class FakeGsp:
    def __init__(self, points_override: int | None = None, prefix: str = "",
                 state: dict | None = None, unanswered: tuple = ()):
        self.log: list[str] = []          # every command, writes and queries
        self.writes: list[str] = []       # the writes only
        self.state = dict(PRESET)
        self.state.update(state or {})
        self.unanswered = set(unanswered)  # queries that time out (not implemented)
        self.points_override = points_override
        self.prefix = prefix              # the manual's example reply starts with ">"
        self.read_only = False
        self.closed = False
        self.timeout = 0
        self.read_termination = self.write_termination = None

    def write(self, cmd: str) -> None:
        if self.read_only:
            raise AssertionError(f"write while read-only: {cmd!r}")
        self.log.append(cmd)
        self.writes.append(cmd)
        head, _, value = cmd.partition(" ")
        head = head.lstrip(":")
        if head == "SWE:TIME":
            # "12.500 ms" -> the query answers in ms (PM p.92)
            self.state[head] = value.split()[0]
        elif value:
            self.state[head] = value

    def query(self, cmd: str) -> str:
        self.log.append(cmd)
        if cmd == "*IDN?":
            return "GWINSTEK,GSP-818,GSP000000,V1.0.0\n"
        if cmd in self.unanswered:
            raise TimeoutError(f"VI_ERROR_TMO: {cmd}")
        if cmd.startswith(":TRAC?"):
            n = self.points_override or int(self.state["SWE:POIN"])
            unit = self.state["UNIT:POW"]
            vals = ",".join(f"{dbm_to_unit(-80.0 + (i % 7), unit):.6g}" for i in range(n))
            return self.prefix + vals + "\n"
        key = cmd.lstrip(":").rstrip("?")
        return self.state.get(key, "0") + "\n"

    def close(self) -> None:
        self.closed = True
