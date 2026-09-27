"""A quick offline sanity check -- no hardware, no network.

Builds the simulated amplifier, exercises gain / on-off / operating point, and
shows that the safety ceiling and the step quantisation work:

    uv run scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from dsamp.config import Config
from dsamp.sim_system import build_sim_system


def main() -> int:
    cfg = Config()
    amp, backend = build_sim_system(cfg)
    events = []
    amp._on_event = lambda lvl, msg: events.append((lvl, msg))

    amp.start()
    print("IDN:", backend.idn())
    s = amp.status()
    # the simulator starts as a leftover (6 dB, off) and the module ADOPTS it
    assert (s.amp_on, s.gain_dB) == (backend._output, backend._gain),         "start must adopt the amplifier's state, not change it"
    print(f"after start (adopted): amp_on={s.amp_on}  gain={s.gain_dB} dB")

    amp.set_frequency(3e9)
    amp.set_input_power(-20.0)
    amp.set_gain(4.2)                          # snaps to the 0.5 dB step -> 4.0
    amp.set_amp(True)
    amp.poll_once()
    s = amp.status()
    print(f"after set: amp_on={s.amp_on}  gain={s.gain_dB} dB  est. gain at 3 GHz "
          f"{s.est_gain_dB:.2f} dB  est. output {s.est_output_dBm:.2f} dBm")
    assert s.amp_on is True and s.gain_dB == 4.0

    amp.set_gain(99.0)                         # above the safety ceiling -> clamped
    amp.poll_once()
    s = amp.status()
    print(f"after over-range: gain={s.gain_dB} dB (ceiling {cfg.limits.gain_max_dB} dB)")
    assert s.gain_dB == cfg.limits.gain_max_dB

    amp.shutdown()
    assert backend.read_output() is False, "shutdown must switch the stage off"
    print(f"after shutdown: stage on={backend.read_output()}  gain={backend.read_gain()} dB")

    print(f"\n{len(events)} events emitted; clamps seen: "
          f"{sum('clamped' in m for _, m in events)}")
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
