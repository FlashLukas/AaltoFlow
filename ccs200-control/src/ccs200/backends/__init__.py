"""Hardware backends: the interface (base), the simulator (sim) and the real
Thorlabs CCS200 (tlccs). Only tlccs touches the vendor library, TLCCS_64.dll,
and only inside `open()`, so the package imports on a PC without it."""


def real_backend(cfg):
    """The real backend -- ONE place, so the service and the GUI's --real can
    never disagree about it. The import stays inside."""
    from .tlccs import TlccsSpectrometer
    h = cfg.hardware
    return TlccsSpectrometer(resource=h.resource, dll_path=h.dll_path,
                             calibration=h.calibration)
