"""A pretend GSP-818 behind a pyvisa-like interface (write / query / close),
so the real backend's SCPI can be tested offline. It answers the commands
backends/gsp.py sends, in the formats the programming manual gives -- which is
exactly what is NOT yet verified on the instrument, so this fake is a record
of our reading of the manual, not proof."""

from __future__ import annotations


class FakeGsp:
    def __init__(self, points_override: int | None = None, prefix: str = ""):
        self.log: list[str] = []
        self.state = {"OUTP:TRAC": "OFF", "SWE:POIN": "601", "BAND": "3000000",
                      "BAND:VID": "3000000", "POW:ATT": "10", "SWE:TIME": "20.000",
                      "INIT:CONT": "ON"}
        self.points_override = points_override
        self.prefix = prefix              # the manual's example reply starts with ">"
        self.closed = False
        self.timeout = 0
        self.read_termination = self.write_termination = None

    def write(self, cmd: str) -> None:
        self.log.append(cmd)
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
        if cmd.startswith(":TRAC?"):
            n = self.points_override or int(self.state["SWE:POIN"])
            vals = ",".join(f"{-80.0 + (i % 7):7.3f}" for i in range(n))
            return self.prefix + vals + "\n"
        key = cmd.lstrip(":").rstrip("?")
        return self.state.get(key, "0") + "\n"

    def close(self) -> None:
        self.closed = True
