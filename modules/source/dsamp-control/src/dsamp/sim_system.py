"""Build a fully simulated amplifier system in one call.

Wire the simulated backend into an Amplifier so the service (and the tests) run
with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .amplifier import Amplifier
from .backends.sim import SimulatedGB6000L
from .config import Config


def build_sim_system(cfg: Config | None = None) -> tuple[Amplifier, SimulatedGB6000L]:
    """Return (amplifier, sim_backend) wired together but NOT yet started.
    The caller (service / test / GUI) calls amplifier.start()."""
    cfg = cfg or Config()
    hw = cfg.hardware
    backend = SimulatedGB6000L(hw.gain_min_dB, hw.gain_max_dB, hw.gain_step_dB)
    amp = Amplifier(backend, cfg)
    return amp, backend
