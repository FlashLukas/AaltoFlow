"""List the Thorlabs power meters this PC can see, and read the head on each.

    uv run scripts/list_devices.py

The first thing to run on a new PC. It needs no service and changes no setting.
If it finds nothing: is the console plugged in and switched on, is Thorlabs OPM
closed (it holds the console while it runs), and is TLPMX installed (it comes
with OPM)?

Output is ASCII only (suite gotcha #14).
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from pm400.backends.tlpmx import TLPMXConsole, TLPMXError, list_resources


def main() -> int:
    try:
        found = list_resources()
    except TLPMXError as exc:
        print(f"TLPMX unavailable: {exc}")
        return 1
    if not found:
        print("no Thorlabs power meter found")
        return 1
    for r in found:
        # The 'available' flag can lie (suite gotcha #23), so we try to open
        # every resource anyway and let the open decide.
        print(f"[{r['index']}] {r['model']}  S/N {r['serial']}  "
              f"(driver says {'available' if r['available'] else 'in use'})")
        print(f"    resource: {r['resource']}")
        m = TLPMXConsole(r["resource"])
        try:
            m.open()
            print(f"    {m.idn()}")
            head = m.sensor_info()
            print(f"    head: {head['name'] or '-'} kind={head['kind']} "
                  f"measures={'energy' if head['energy'] else 'power'}")
            if head["kind"] in ("photodiode", "thermal", "pyro"):
                lo, hi = m.wavelength_range()
                print(f"    wavelength {m.get_wavelength():g} nm ({lo:g}..{hi:g})")
                if head["energy"]:
                    e, flag = m.measure_energy()
                    print(f"    energy range {m.get_energy_range():.4g} J, "
                          f"pulse {e:.4e} J {flag}, rate {m.measure_frequency():.3g} Hz")
                else:
                    p, flag = m.measure_power()
                    print(f"    {'auto' if m.get_auto_range() else 'manual'} range "
                          f"{m.get_range():.4g} W, averaging {m.get_avg_time() * 1e3:.4g} ms")
                    print(f"    power {p:.4e} W {flag}")
        except TLPMXError as exc:
            print(f"    could not read: {exc}")
        finally:
            m.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
