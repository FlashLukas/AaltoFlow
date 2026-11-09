"""Build a fully-simulated supply in one call.

Wires the simulated ALR3203 (with its coil load) and a simulated polarity relay
into a BipolarSupply so the service, the GUI and the tests run with nothing
plugged in. Qt-free on purpose.
"""

from __future__ import annotations

from .backends.sim import NoRelay, SimRelay, SimulatedALR3203
from .config import Config
from .supply import BipolarSupply


def build_sim_system(cfg: Config | None = None, seed=None,
                     clock=None) -> tuple[BipolarSupply, SimulatedALR3203]:
    """Return (supply, sim_backend) wired together but NOT yet started.
    The relay is `supply.relay` (a SimRelay, or NoRelay when
    polarity.driver = "none"). The caller calls supply.start()."""
    cfg = cfg or Config()
    kw = {} if clock is None else {"clock": clock}
    backend = SimulatedALR3203(load=cfg.sim, seed=seed, **kw)
    if str(cfg.polarity.driver).strip().lower() == "none":
        relay = NoRelay()
    else:
        relay = SimRelay(backend, inverted=bool(cfg.sim.found_relay_inverted))
    supply = BipolarSupply(backend, cfg, relay=relay, **kw)
    return supply, backend


def build_real_relay(cfg: Config):
    """The relay for the REAL service: on usb6001-control, or none."""
    p = cfg.polarity
    driver = str(p.driver).strip().lower()
    if driver == "none":
        return NoRelay()
    if driver == "sim":
        # a real supply with a pretend relay makes no sense: say so loudly
        raise ValueError("polarity.driver = 'sim' with the real supply: set it to "
                         "'usb6001' (the relay on the DAQ) or 'none' (unipolar)")
    from .backends.remote_usb6001 import Usb6001Relay
    return Usb6001Relay(host=p.host, cmd_port=p.cmd_port, line=p.line,
                        inverted_level=p.inverted_level, timeout_ms=p.timeout_ms)
