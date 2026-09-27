"""List the VISA instruments this PC can see and ask each one who it is.

    uv sync --extra gui --extra real
    uv run scripts/list_devices.py

The first thing to run on a new PC. It needs no service and changes no
setting: it only sends *IDN?. A Lake Shore 455 answers "LSCI,MODEL455,...".

Serial ports are asked with the 455's fixed frame (7 data bits, odd parity,
1 stop bit) at 9600 baud; another instrument on a COM port may simply not
answer that, which is harmless.
"""

from __future__ import annotations


def main() -> int:
    try:
        import pyvisa
        from pyvisa import constants
    except ImportError:
        print("pyvisa is not installed: uv sync --extra gui --extra real")
        return 1
    try:
        rm = pyvisa.ResourceManager()
    except (OSError, ValueError):
        rm = pyvisa.ResourceManager("@py")
    found = rm.list_resources()
    if not found:
        print("no VISA instruments found")
        return 1
    for res in found:
        try:
            inst = rm.open_resource(res)
            inst.timeout = 1500
            inst.read_termination = "\r\n"
            inst.write_termination = "\r\n"
            if res.upper().startswith("ASRL"):
                inst.baud_rate = 9600
                inst.data_bits = 7
                inst.parity = constants.Parity.odd
                inst.stop_bits = constants.StopBits.one
            idn = inst.query("*IDN?").strip()
            inst.close()
        except Exception as exc:                  # a port that is busy or silent
            idn = f"(no answer: {type(exc).__name__})"
        tag = "   <-- Lake Shore 455" if "MODEL455" in idn.upper() else ""
        print(f"{res:28s} {idn}{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
