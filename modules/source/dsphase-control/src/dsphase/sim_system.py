"""Build a fully-simulated phase shifter in one call.

Wires the simulated PS6000L into a PhaseShifter so the service, the GUI and the
tests run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .config import Config
from .backends.sim import SimulatedPS6000L
from .shifter import PhaseShifter


def build_sim_system(cfg: Config | None = None, **unit_state) -> tuple[PhaseShifter, SimulatedPS6000L]:
    """Return (brain, sim_backend) wired together but NOT yet started.
    The caller (service / GUI / test) calls brain.start().

    `unit_state` (phase_deg=, attenuation_dB=, output_on=) is the state the fake
    box is in BEFORE we connect -- tests use it to check that start() adopts it.

    The simulator gets the SAME cfg.device object, so a step size changed in
    Settings applies to the fake unit too (set_config edits in place).
    """
    cfg = cfg or Config()
    backend = SimulatedPS6000L(device=cfg.device, **unit_state)
    return PhaseShifter(backend, cfg), backend
