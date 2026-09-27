"""Build a fully-simulated generator system in one call.

Wire the simulated SG12000L into a Synthesizer so the service (and the tests)
can run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .config import Config
from .backends.sim import SimulatedSG12000L
from .synthesizer import Synthesizer


def build_sim_system(cfg: Config | None = None) -> tuple[Synthesizer, SimulatedSG12000L]:
    """Return (synthesizer, sim_backend) wired together but NOT yet started.
    The caller (service / test / GUI) calls synthesizer.start()."""
    cfg = cfg or Config()
    backend = SimulatedSG12000L(sim=cfg.sim,
                                power_step_dB=cfg.hardware.power_step_dB)
    return Synthesizer(backend, cfg), backend


def build_real_backend(cfg: Config):
    """The real SG12000L backend, configured from cfg.hardware (not opened)."""
    from .backends.dsi_scpi import DsiSG12000L
    hw = cfg.hardware
    return DsiSG12000L(transport=hw.transport, com_port=hw.com_port, baud=hw.baud,
                       host=hw.host, tcp_port=hw.tcp_port, timeout_s=hw.timeout_s,
                       phase_mode=hw.phase_mode)
