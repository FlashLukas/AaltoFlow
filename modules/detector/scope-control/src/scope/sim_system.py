"""Build a fully-simulated scope in one call (Qt-free), for the service and
the tests: the brain on a simulated instrument.

cfg.sim.model chooses which: "sds" (default) = the Siglent bench, two test
signals; "ad" = an Analog Discovery 2 -- scope + generator W1/W2 (looped back
to CH1/CH2) + V+/V- supplies, one device like the real one."""

from __future__ import annotations

from .config import Config
from .backends.sim import SimulatedScope, SimulatedADScope
from .scope import Scope


def build_sim_system(cfg: Config | None = None, seed: int | None = None,
                     gen_cfg=None) -> tuple[Scope, SimulatedScope]:
    """(scope, sim_backend), wired but NOT started; the caller calls start().
    The simulator reads the live `cfg.sim` group, so set_sim / set_config
    change the pretend bench at once. In "ad" mode the generator brain is
    `scope.gen` (its config `gen_cfg`, a generator.config.GenConfig)."""
    cfg = cfg or Config()
    if getattr(cfg.sim, "model", "sds") == "ad":
        from .generator.brain import Generator
        from .generator.config import GenConfig
        from .generator.sim import SimulatedADGen
        wave = SimulatedADGen()
        backend = SimulatedADScope(cfg.sim, wave, seed=seed)
        gen = Generator(wave, gen_cfg or GenConfig())
        return Scope(backend, cfg, gen=gen), backend
    backend = SimulatedScope(cfg.sim, seed=seed)
    return Scope(backend, cfg), backend
