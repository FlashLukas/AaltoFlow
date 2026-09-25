"""List the Thorlabs power meters this PC can see, and read one value from each.

    uv run scripts/list_devices.py

The first thing to run on a new PC. It needs no service and changes no setting.
If it finds nothing: is the meter plugged in, is Thorlabs OPM closed (it holds
the meter while it runs), and is TLPMX installed (it comes with OPM)?
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from pm16.backends.tlpmx import TLPMXError, TLPMXPowerMeter, list_resources


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
        print(f"[{r['index']}] {r['model']}  S/N {r['serial']}  "
              f"{'available' if r['available'] else 'IN USE by another program'}")
        print(f"    resource: {r['resource']}")
        if not r["available"]:
            continue
        m = TLPMXPowerMeter(r["resource"])
        try:
            m.open()
            p, flag = m.measure_power()
            lo, hi = m.wavelength_range()
            print(f"    {m.idn()}")
            print(f"    wavelength {m.get_wavelength():g} nm ({lo:g}..{hi:g}), "
                  f"{'auto' if m.get_auto_range() else 'manual'} range {m.get_range():.4g} W")
            print(f"    power {p:.4e} W {flag}")
        except TLPMXError as exc:
            print(f"    could not read: {exc}")
        finally:
            m.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
