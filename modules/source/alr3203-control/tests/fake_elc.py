"""A fake `serial` (pyserial) module with an ELC ALR3203 behind it.

It speaks the manual's Appendix A protocol byte for byte -- "0 VOLT WR 1250\\r"
in, "0 OK\\r" out -- so the real backend (elc_serial.py) can be tested offline.
`log` records every command line in order; `local=True` makes the supply
refuse writes with "0 LOCAL" until "REM WR 1" arrives (front panel mode).
"""

from __future__ import annotations

import types


class FakeALR:
    def __init__(self, idn="ALR3203 VERSION 1.02", output=False, volt_mv=32000,
                 curr_ma=0, local=False):
        self.idn = idn
        self.output = output
        self.volt_mv = volt_mv
        self.curr_ma = curr_ma
        self.local = local
        self.remote = False
        self.load_R = 2.0
        self.log: list[str] = []
        self.silent = False             # True: answers nothing (unplugged)

    def answer(self, line: str) -> str:
        self.log.append(line)
        if self.silent:
            return ""
        parts = line.split()
        if len(parts) < 3 or parts[0] != "0":
            return "0 ERR"
        param, cmd, val = parts[1], parts[2], parts[3:] and parts[3]
        if cmd == "WR":
            if param == "REM":
                self.remote = val == "1"
                return "0 OK"
            if self.local and not self.remote:
                return "0 LOCAL"
            if param == "VOLT":
                self.volt_mv = int(val)
            elif param == "CURR":
                self.curr_ma = int(val)
            elif param == "OUT":
                self.output = val == "1"
            else:
                return "0 ERR"
            return "0 OK"
        if cmd == "RD":
            return {"VOLT": f"0 OK {self.volt_mv}", "CURR": f"0 OK {self.curr_ma}",
                    "OUT": f"0 OK {int(self.output)}", "REM": f"0 OK {int(self.remote)}",
                    "MODE": f"0 OK {self.mode_code()}", "IDN": f"0 OK {self.idn}",
                    "SERIAL": "0 OK 12345"}.get(param, "0 ERR")
        if cmd == "MES":
            i_ma, v_mv = self.operating_point()
            if param == "VOLT":
                return f"0 OK {v_mv}"
            if param == "CURR":
                return f"0 OK {i_ma}"
        return "0 ERR"

    def operating_point(self):
        """A resistor on the output: CV or CC, whichever comes first."""
        if not self.output:
            return 0, 0
        i_cv = self.volt_mv / self.load_R          # mA if CV
        if i_cv <= self.curr_ma:
            return int(round(i_cv)), self.volt_mv
        return self.curr_ma, int(round(self.curr_ma * self.load_R))

    def mode_code(self):
        if not self.output:
            return 0
        return 1 if self.volt_mv / self.load_R <= self.curr_ma else 2


def make_serial_module(supplies: dict, opened: list):
    """A stand-in for `import serial`: supplies = {port: FakeALR}."""

    class Serial:
        def __init__(self, port, baudrate=9600, bytesize=None, parity=None,
                     stopbits=None, timeout=None, write_timeout=None):
            if port not in supplies:
                raise OSError(f"could not open port {port!r}")
            self.port = port
            self.settings = (baudrate, bytesize, parity, stopbits)
            self.dev = supplies[port]
            self._out = b""
            self.closed = False
            opened.append(self)

        def reset_input_buffer(self):
            self._out = b""

        def write(self, data: bytes):
            assert data.endswith(b"\r"), data
            line = data.decode("ascii")[:-1]
            ans = self.dev.answer(line)
            self._out += (ans + "\r").encode("ascii") if ans else b""
            return len(data)

        def read_until(self, term=b"\r"):
            i = self._out.find(term)
            if i < 0:
                out, self._out = self._out, b""
                return out
            out, self._out = self._out[:i + 1], self._out[i + 1:]
            return out

        def close(self):
            self.closed = True

    return types.SimpleNamespace(Serial=Serial, SEVENBITS=7, PARITY_EVEN="E",
                                 STOPBITS_ONE=1)
