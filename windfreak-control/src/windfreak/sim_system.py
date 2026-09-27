"""Build a fully-simulated synthesizer in one call.

Wires the simulated SynthHD into a Synthesizer so the service (and the tests)
can run with nothing plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .config import Config
from .backends.sim import SimulatedSynthHD
from .synthesizer import Synthesizer


def build_sim_system(cfg: Config | None = None,
                     external_ref_MHz: float | None = None
                     ) -> tuple[Synthesizer, SimulatedSynthHD]:
    """Return (synthesizer, sim_backend) wired together but NOT yet started.
    The caller (service / test) calls synthesizer.start().

    `external_ref_MHz`: what the pretend lab has plugged into REF IN (None =
    nothing, so selecting the external reference unlocks both channels)."""
    cfg = cfg or Config()
    hw = cfg.hardware
    backend = SimulatedSynthHD(channel_spacing_Hz=hw.channel_spacing_Hz or 100.0,
                               pll_off_when_rf_off=hw.pll_off_when_rf_off,
                               external_ref_MHz=external_ref_MHz)
    return Synthesizer(backend, cfg), backend
