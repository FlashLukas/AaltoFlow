"""Hardware backends: the interface (base), the simulator (sim) and two real
analysers -- the Keysight PNA-X N5222A (pna) and the Copper Mountain C1209 (cmt).
Only those two touch pyvisa, and only inside `open()`, so the package imports
on a PC with no VISA installed."""

#: hardware.driver -> the analyser it drives, for labels and messages
ANALYSERS = {"pna": "Keysight PNA-X N5222A", "cmt": "Copper Mountain C1209"}


def analyser_name(cfg) -> str:
    """What a real analyser of this config is called (for a label)."""
    return ANALYSERS.get(cfg.hardware.driver, f"unknown driver {cfg.hardware.driver!r}")


def real_backend(cfg):
    """The real backend `cfg.hardware.driver` asks for -- ONE place, so the
    service and the GUI's --real can never disagree about it. The imports stay
    inside, so choosing one never loads the other."""
    driver = cfg.hardware.driver
    if driver == "cmt":
        from .cmt import CmtVna, limit_envelope
        limit_envelope(cfg)               # 100 kHz - 9 GHz, before the brain clamps
        return CmtVna(cfg)
    if driver == "pna":
        from .pna import PnaVna
        return PnaVna(cfg)
    raise ValueError(f"hardware.driver must be one of {tuple(ANALYSERS)}, got {driver!r}")
