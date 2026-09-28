"""Build a fully-simulated DAQ in one call (Qt-free on purpose).

Wires the simulated USB-6001 into a Daq, so the service, the GUI and the tests
run with nothing plugged in.
"""

from __future__ import annotations

from .backends.sim import SimulatedUsb6001
from .config import Config
from .daq import Daq


def build_sim_system(cfg: Config | None = None, **sim_kwargs) -> tuple[Daq, SimulatedUsb6001]:
    """Return (daq, sim_backend), wired together but NOT started.
    Extra keyword arguments go to SimulatedUsb6001 (ao_start, do_start, seed)."""
    cfg = cfg or Config()
    backend = SimulatedUsb6001(loopback_ai=cfg.hardware.sim_ai_loopback,
                               loopback_di=cfg.hardware.sim_di_loopback, **sim_kwargs)
    return Daq(backend, cfg), backend


def demo_config() -> Config:
    """A layout that shows every feature, for the LOCAL simulator GUI only.

    The real default (Config()) makes every digital line an input, which is
    the safe choice for hardware but leaves a demo panel with nothing to
    click. This one has outputs, a scaled channel and loopbacks.
    """
    cfg = Config()
    names = {0: "AO0 loopback", 1: "AO1 loopback", 2: "Hall probe", 3: "Photodiode"}
    for i, ch in enumerate(cfg.ai.channels):
        ch.enabled = i < 4
        ch.name = names.get(i, f"AI {i}")
    cfg.ai.channels[2].unit, cfg.ai.channels[2].slope = "mT", 100.0
    cfg.ao.channels[0].name, cfg.ao.channels[1].name = "Coil drive", "Piezo bias"
    cfg.ao.channels[1].min_V, cfg.ao.channels[1].max_V = 0.0, 5.0
    for i, d in enumerate(cfg.dio.lines):
        d.direction = "in" if i < 4 else ("out" if i < 8 else "unused")
    cfg.dio.lines[12].direction = "in"
    cfg.dio.lines[4].name, cfg.dio.lines[5].name = "Shutter", "Trigger"
    cfg.dio.lines[0].name = "Interlock"
    cfg.hardware.sim_di_loopback = True
    return cfg
