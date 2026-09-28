"""Hardware backends: the interface (base), the simulator (sim) and the real
Signal Hound analyser through sa_api.dll (sa_api). Only `sa_api` touches the
vendor library, and only inside `open()`, so the package imports on a PC
without the Signal Hound SDK."""


def analyser_name(model: str = "") -> str:
    """A label for a real analyser of this model ('' = not known yet)."""
    return f"Signal Hound {model}" if model else "Signal Hound spectrum analyser"


def real_backend(cfg):
    """The real backend -- ONE place, so the service and the GUI's --real can
    never disagree about it. The import stays inside, so the simulator path
    never loads it."""
    from .sa_api import SaApiAnalyzer
    return SaApiAnalyzer(cfg)
