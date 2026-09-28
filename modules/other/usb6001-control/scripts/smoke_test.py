"""A quick offline sanity check -- no hardware, no network.

Builds the simulated card with the demo layout, proves that starting it writes
nothing, sets an output, reads it back through the loopback input, drives a
digital output and shows the clamp and the input-line refusal:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from usb6001.sim_system import build_sim_system, demo_config


def main() -> int:
    cfg = demo_config()
    daq, sim = build_sim_system(cfg)
    events = []
    daq._on_event = lambda lvl, msg: events.append((lvl, msg))

    daq.start(poll=False)
    print("IDN:", daq.status().idn)
    assert sim.writes == [], "start must write nothing"
    print("start wrote nothing; AO known:", daq.status().ao_known)

    daq.set_ao(0, 1.25)
    r = daq.read_ai("ai0")
    print(f"ao0 = 1.25 V -> fresh ai0 = {r['values_V']['ai0']:+.4f} V (loopback)")
    assert abs(r["values_V"]["ai0"] - 1.25) < 0.01

    daq.set_ao(1, 99.0)                            # clamps to the ao1 limit (5 V)
    print("ao1 after asking 99 V:", daq.status().ao_V[1])
    assert daq.status().ao_V[1] == cfg.ao.channels[1].max_V

    daq.set_do("p0.4", True)
    print("p0.4 driven high:", daq.status().dio[4])
    try:
        daq.set_do("p0.0", True)
        print("ERROR: an input line was driven")
        return 1
    except ValueError as exc:
        print("p0.0 refused (input):", str(exc).split(":")[0])

    print("fresh inputs:", daq.read_di()["levels"])
    daq.shutdown()
    print(f"{len(events)} events, last: {events[-1][1]}")
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
