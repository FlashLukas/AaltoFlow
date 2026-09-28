"""A quick offline sanity check -- no hardware, no network.

Builds the simulated phase shifter, exercises the controls, and shows the three
things that make this module more than a memory cell: rounding to the device
step, wrapping into -180..+180 with the readback reported in your branch, and
the safety clamp. Run it any time:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from dsphase.config import Config
from dsphase.sim_system import build_sim_system


def wait_for(brain, pred, timeout=2.0):
    """The brain reads the unit back on a worker thread; wait for its snapshot."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        s = brain.status()
        if pred(s):
            return s
        time.sleep(0.02)
    return brain.status()


def main() -> int:
    cfg = Config()
    # the fake unit was left at -90 deg / 12.5 dB with RF ON by someone else:
    # start() must ADOPT that, not reset it
    brain, backend = build_sim_system(cfg, phase_deg=-90.0, attenuation_dB=12.5, output_on=True)
    events = []
    brain._on_event = lambda lvl, msg: events.append((lvl, msg))

    brain.start()
    print("IDN:", backend.idn())
    s0 = brain.status()
    print(f"adopted: phase {s0.phase_deg} deg, att {s0.attenuation_dB} dB, out={s0.output_on}")
    assert s0.output_on is True and s0.phase_deg == -90.0 and s0.attenuation_dB == 12.5, \
        "start must adopt the unit's state"
    assert backend.write_log == [], "start must not write to the unit"

    brain.set_phase(33.3)                      # -> 33.5 (0.5 deg step)
    brain.set_attenuation(6.1)                 # -> 6.0 (0.25 dB step)
    brain.set_output(True)
    s = wait_for(brain, lambda s: s.phase_deg == 33.5 and s.output_on)
    print(f"phase 33.3 -> {s.phase_deg} deg, att 6.1 -> {s.attenuation_dB} dB, out={s.output_on}")
    assert s.phase_deg == 33.5 and s.attenuation_dB == 6.0 and s.output_on

    brain.set_phase(270.0)                     # device holds -90
    s = wait_for(brain, lambda s: s.phase_deg == 270.0)
    print(f"phase 270 -> reported {s.phase_deg} deg, device holds {s.phase_device_deg} deg")
    assert s.phase_deg == 270.0 and s.phase_device_deg == -90.0

    brain.set_phase(1000.0)                    # clamp
    brain.set_attenuation(-5.0)
    s = wait_for(brain, lambda s: s.phase_deg == cfg.limits.phase_max_deg)
    print(f"over-range: phase -> {s.phase_deg}, att -> {s.attenuation_dB}")
    assert s.phase_deg == cfg.limits.phase_max_deg
    assert s.attenuation_dB == cfg.limits.att_min_dB

    brain.shutdown()
    assert backend._output is False, "shutdown must switch the RF output off"
    assert brain.status().connected is False
    print(f"after shutdown: connected={brain.status().connected}, output off")
    print(f"\n{len(events)} events; clamps seen: {sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
