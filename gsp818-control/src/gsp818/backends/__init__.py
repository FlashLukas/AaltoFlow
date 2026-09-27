"""Hardware backends: the interface (base), the simulator (sim) and the real
GW Instek GSP-818 (gsp, over pyvisa). Only `gsp.py` touches pyvisa, and only
inside `open()`, so the package imports on a PC with no VISA installed."""

ANALYSER_NAME = "GW Instek GSP-818"


def real_backend(cfg):
    """The real backend -- ONE place, so the service and the GUI's --real can
    never disagree about it. The import stays inside, so the simulator path
    never loads it."""
    from .gsp import GspAnalyzer
    return GspAnalyzer(cfg)
