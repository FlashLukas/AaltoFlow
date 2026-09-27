"""Build a fully-simulated laser system in one call.

Wire the simulated backend into a SuperK brain so the service (and the tests)
can run with nothing plugged in. Qt-free on purpose.

The service ADOPTS the laser's state at start (it writes nothing), so a fake
laser straight out of the box -- power 0 %, every line at 0 nm -- would make a
dull and slightly nonsensical demo. The simulated laser is therefore PRESET to a
plausible "left like this last time" state taken from the config's presets:
emission OFF and RF OFF (a laser nobody is using), the preset power level,
crystal and lines. Tests use `backend.preset(...)` for other states.
"""

from __future__ import annotations

from . import config as C
from .config import Config, N_LINES
from .backends.sim import SimulatedSuperK
from .laser import SuperK


def build_sim_system(cfg: Config | None = None) -> tuple[SuperK, SimulatedSuperK]:
    """Return (laser, sim_backend) wired together but NOT yet started.
    The caller (service / test) calls laser.start()."""
    cfg = cfg or Config()
    backend = SimulatedSuperK(warmup_s=cfg.hardware.sim_warmup_s)
    p = cfg.startup
    names = [n.lower() for n in C.names(cfg.filters.names)]
    codes = C.ints(cfg.filters.crystal, len(names), 1)
    idx = names.index(p.filter.strip().lower()) if p.filter.strip().lower() in names else 0
    crystal = codes[idx] if codes else None
    backend.preset(emission=False, rf=False, power_pct=p.power_pct,
                   crystal=crystal if crystal in backend._ranges else None,
                   wavelengths_nm=C.floats(p.wavelengths_nm, N_LINES, 650.0),
                   amplitudes_pct=C.floats(p.amplitudes_pct, N_LINES, 0.0),
                   watchdog_s=cfg.hardware.watchdog_s)
    laser = SuperK(backend, cfg)
    return laser, backend
