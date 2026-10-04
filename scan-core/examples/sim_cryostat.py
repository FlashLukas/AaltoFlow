"""sim_cryostat.py -- a pretend cryostat, so the examples run without the lab.

The simulated instruments of scan-core have a magnet, an RF source, a stage
and a lock-in, but no temperature. The examples need one, so this adds two
entries to a SIMULATED registry:

    temperature          settable, K   -- relaxes towards the setpoint
    temperature_stable   detector      -- True once within 0.05 K of it

On the rig the same roles are played by the cryostat's own module (for the
DynaCool: "ppms.temperature" and "ppms.temperature_stable"); nothing here is
used there.
"""

from __future__ import annotations

import math
import time

from scan_core.registry import Gettable, Settable


def add_sim_cryostat(lab, tau_s: float = 0.25, start_K: float = 300.0) -> None:
    """Give `lab` (an api.Lab made with simulate=True) a toy temperature."""
    state = {"from": start_K, "to": start_K, "t0": time.monotonic()}

    def now_K() -> float:
        # first-order approach: T = to + (from - to) * exp(-t / tau)
        t = time.monotonic() - state["t0"]
        return state["to"] + (state["from"] - state["to"]) * math.exp(-t / tau_s)

    def set_K(value: float) -> None:
        # like a real controller: accept the setpoint at once and get there
        # in its own time -- the SCRIPT decides how long to wait
        state.update({"from": now_K(), "to": float(value), "t0": time.monotonic()})

    reg = lab.registry
    reg.add(Settable("temperature", "Temperature (simulated)", "K", (1.8, 400.0),
                     set_fn=set_K, get_fn=now_K))
    reg.add(Gettable("temperature_stable", "Temperature stable (simulated)", "",
                     lambda: abs(now_K() - state["to"]) < 0.05, dtype="bool"))
