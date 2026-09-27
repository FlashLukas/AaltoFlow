"""Build a fully-simulated SR830 in one call. Qt-free on purpose."""

from __future__ import annotations

import time

from . import tables
from .backends.sim import SimulatedSR830
from .config import Config
from .lockin import DspLockIn


def build_sim_system(cfg: Config | None = None, clock=time.monotonic,
                     seed: int | None = None) -> tuple[DspLockIn, SimulatedSR830]:
    """Return (lockin, sim_backend) wired together but NOT yet started."""
    cfg = cfg or Config()
    backend = SimulatedSR830(clock=clock, seed=seed)
    # Put the simulated experiment at the frequency the lock-in STARTS on, so a
    # fresh simulator shows a signal. Tuning away still makes it vanish, as on
    # the real instrument.
    backend.ext_ref_Hz = cfg.reference.frequency_Hz
    # The service ADOPTS the instrument's settings at start and writes nothing.
    # A simulator has no front panel that remembers anything, so its POWER-ON
    # state is taken from cfg (the .ini): in simulation, the .ini describes
    # "how the instrument was left". This is the sim's own state, not a push --
    # the brain still only reads it. Tests that check adoption give the sim a
    # different state before start() (tests/test_adopt.py).
    preset_from_config(backend, cfg)
    return DspLockIn(backend, cfg, clock=clock), backend


def preset_from_config(sim: SimulatedSR830, cfg: Config) -> None:
    """Set the simulator's front panel to the settings in cfg (sim only)."""
    ref, inp, dem = cfg.reference, cfg.input, cfg.demod
    sim.internal = ref.source == "internal"
    sim.osc_hz = float(ref.frequency_Hz)
    sim._pll_hz = float(ref.frequency_Hz)
    sim.harmonic = int(ref.harmonic)
    sim.phase_deg = float(ref.phase_deg)
    sim.trigger = tables.TRIGGERS.index(ref.trigger) if ref.trigger in tables.TRIGGERS else 0
    sim.sine_V = round(float(ref.sine_out_V) / 0.002) * 0.002
    for attr, options, value in (("source", tables.INPUT_SOURCES, inp.source),
                                 ("ground", tables.GROUNDS, inp.ground),
                                 ("coupling", tables.COUPLINGS, inp.coupling),
                                 ("line", tables.LINE_FILTERS, inp.line_filter),
                                 ("reserve", tables.RESERVES, dem.reserve),
                                 ("slope", tables.SLOPES, dem.slope)):
        if value in options:
            setattr(sim, attr, options.index(value))
    try:
        sim.sens = tables.sens_index(dem.sensitivity)
    except ValueError:
        pass
    try:
        sim.tc = tables.tc_index(dem.time_constant)
    except ValueError:
        pass
    sim.sync = bool(dem.sync_filter) if isinstance(dem.sync_filter, bool) else False
    sim.aux_out = [round(float(cfg.aux_out.get(k)), 3) for k in range(4)]
