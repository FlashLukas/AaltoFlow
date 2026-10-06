"""A quick offline sanity check -- no hardware, no network.

Builds the simulated AFG1062, shows that start-up only reads, drives both
channels, shows the safety clamps and the CH2-follows-CH1 coupling, and that
every output goes off on shutdown:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from afg.config import Config
from afg.sim_system import build_sim_system


def wait_settled(gen, ch, timeout=2.0, **echo):
    """Wait until the channel ECHOES the requested values and only then trust
    its settled flag (gotcha #2: the flag alone may be from before the command)."""
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        s = gen.status()
        if all(s[f"{ch}_{k}"] == v for k, v in echo.items()) and s[f"{ch}_settled"]:
            return s
        time.sleep(0.01)
    raise SystemExit(f"{ch} did not settle: {gen.status()}")


def main() -> int:
    cfg = Config()
    gen, backend = build_sim_system(cfg)
    events = []
    gen._on_event = lambda lvl, msg: events.append((lvl, msg))

    gen.start()
    print("IDN:", backend.idn())
    s = gen.status()
    # read-only start: the sim was left driving a 30 Hz sine on CH1
    assert s["ch1_output"] and s["ch1_frequency_Hz"] == 30.0, "start must adopt"
    assert backend.writes == [], "start must not write to the instrument"
    print(f"adopted: CH1 {s['ch1_waveform']} {s['ch1_frequency_Hz']} Hz "
          f"{s['ch1_amplitude_Vpp']} Vpp, output on")

    gen.set_frequency("ch1", 1000.0)
    gen.set_amplitude("ch1", 1.0)
    s = wait_settled(gen, "ch1", frequency_Hz=1000.0, amplitude_Vpp=1.0)
    print(f"CH1: {s['ch1_frequency_Hz']} Hz, {s['ch1_amplitude_Vpp']} Vpp, settled")

    # the bench use: CH2 a square locked to CH1, a quarter period later
    gen.set_follow(True, 90.0)
    gen.set_output("ch2", True)
    s = wait_settled(gen, "ch2", frequency_Hz=1000.0, phase_deg=90.0, output=True)
    print(f"CH2 follows CH1: {s['ch2_frequency_Hz']} Hz, phase {s['ch2_phase_deg']} deg")
    assert ("align_phase",) in backend.writes

    # safety: amplitude above the instrument's 10 Vpp, then an offset that
    # would push the peak past 5 V -- both clamped, with a warn event
    gen.set_amplitude("ch1", 25.0)
    wait_settled(gen, "ch1", amplitude_Vpp=10.0)
    gen.set_offset("ch1", 2.0)
    s = wait_settled(gen, "ch1", offset_V=0.0)
    print(f"after over-range: CH1 {s['ch1_amplitude_Vpp']} Vpp, offset "
          f"{s['ch1_offset_V']} V, peak {s['ch1_peak_V']} V")
    assert s["ch1_peak_V"] <= 5.0

    gen.shutdown()
    assert not backend.ch[0]["output"] and not backend.ch[1]["output"], "output left on!"
    print("after shutdown: both outputs off")

    print(f"\n{len(events)} events; clamps seen: {sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
