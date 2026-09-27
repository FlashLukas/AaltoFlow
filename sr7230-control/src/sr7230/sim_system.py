"""Build a fully-simulated lock-in in one call. Qt-free on purpose."""

from __future__ import annotations

import time

from . import tables
from .backends.sim import Simulated7230
from .config import Config, REF_SOURCES, SLOPES_DB
from .lockin import LockIn, _INPUT_CODES, _LINE_FILTER


def preset_front_panel(sim: Simulated7230, cfg: Config) -> None:
    """Leave the SIMULATED instrument in the state `cfg` describes, as if
    someone had set its front panel by hand before the service started.

    Why this exists: since 2026-09-27 the brain never pushes its config at
    start -- it READS the instrument and adopts what it finds. For the
    simulator that would make the .ini meaningless (every start would adopt
    the simulator's power-up values). So the simulated box is "switched on"
    in the state the config asks for; the brain then adopts it through the
    same read path it uses on the real 7230. Attributes are set directly, not
    through the setters: this is the bench before we connect, not a command.
    """
    ref, sig, flt = cfg.reference, cfg.signal, cfg.filter
    sim.ref_source = REF_SOURCES.index(ref.source) if ref.source in REF_SOURCES else 0
    sim.osc_f = float(ref.frequency_Hz)
    sim.osc_amp = float(ref.amplitude_V)
    sim.phase_deg = (float(ref.phase_deg) + 180.0) % 360.0 - 180.0
    sim.harmonic = int(ref.harmonic)
    sim.imode, sim.vmode = _INPUT_CODES.get(sig.input, (0, 1))
    sim.dc = not bool(sig.ac_coupled)
    sim.fet = bool(sig.fet)
    sim.floating = bool(sig.float_shield)
    sim.line_filter = (_LINE_FILTER.get(sig.line_filter, 0), int(sig.line_freq_Hz) != 60)
    sim.auto_ac_gain = bool(sig.auto_ac_gain)
    sim.sen_index = int(sig.sensitivity_index)
    sim.fast = bool(flt.fast_mode)
    allowed = tables.allowed_time_constants(sim.fast, 0.0, float("inf"))
    sim.tc_index = tables.nearest_tc_index(float(flt.time_constant_s), allowed)
    slope = int(flt.slope_db) if int(flt.slope_db) in SLOPES_DB else 12
    sim.slope_index = SLOPES_DB.index(slope)


def build_sim_system(cfg: Config | None = None, clock=time.monotonic,
                     seed: int | None = None,
                     preset: bool = True) -> tuple[LockIn, Simulated7230]:
    """Return (lockin, sim_backend) wired together but NOT yet started.

    `preset=True` switches the simulated instrument on in the state `cfg`
    describes (see preset_front_panel). `preset=False` leaves it at the
    simulator's own power-up values -- what the adoption tests use to prove
    the brain reports the INSTRUMENT, not its config.
    """
    cfg = cfg or Config()
    backend = Simulated7230(clock=clock, seed=seed)
    if preset:
        preset_front_panel(backend, cfg)
    # Put the simulated external source at the frequency the oscillator STARTS
    # on, so a fresh simulator shows a signal on the internal reference. Tuning
    # away still makes it vanish, as on the real instrument.
    backend.signal_Hz = backend.osc_f
    return LockIn(backend, cfg, clock=clock), backend
