"""Build a fully-simulated heater controller in one call.

Wires the simulated TC200 (a heated block with a PID) into a Heater brain so
the service (and tests) run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .backends.sim import SimulatedTC200
from .config import Config
from .heater import Heater


def build_sim_system(cfg: Config | None = None, **sim_kwargs) -> tuple[Heater, SimulatedTC200]:
    """Return (heater, sim_backend) wired together but NOT yet started.
    The caller (service / test / GUI) calls heater.start(). `sim_kwargs` set the
    simulated box's starting state (temperature_C, setpoint_C, enabled, ...).
    A `clock` is shared by the box and the brain, so a test can fast-forward
    both at once."""
    cfg = cfg or Config()
    backend = SimulatedTC200(**sim_kwargs)
    if "clock" in sim_kwargs:
        return Heater(backend, cfg, clock=sim_kwargs["clock"]), backend
    return Heater(backend, cfg), backend
