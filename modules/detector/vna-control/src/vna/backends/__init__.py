"""Hardware backends: the interface (base), the simulator (sim) and the real
analysers -- the Keysight PNA-X N5222A (pna), the Copper Mountain C1209 (cmt),
and the LibreVNA and Harogic PNB-60 (librevna.py: one SCPI language, two
programs). Only those files touch pyvisa, and only inside `open()`, so the
package imports on a PC with no VISA installed."""

#: hardware.driver -> the analyser it drives, for labels and messages
ANALYSERS = {"pna": "Keysight PNA-X N5222A", "cmt": "Copper Mountain C1209",
             "librevna": "LibreVNA", "harogic": "Harogic PNB-60"}

#: hardware.driver -> the Hardware field holding its address (what --visa sets)
RESOURCE_FIELDS = {"pna": "visa_resource", "cmt": "cmt_resource",
                   "librevna": "librevna_resource", "harogic": "harogic_resource"}


def resource_field(cfg) -> str:
    """The name of the config field with the address of the chosen analyser."""
    return RESOURCE_FIELDS.get(cfg.hardware.driver, "visa_resource")


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
    if driver in ("librevna", "harogic"):
        from .librevna import DIALECTS, SweepServerVna, limit_envelope
        dialect = DIALECTS[driver]
        limit_envelope(cfg, dialect)      # its data-sheet envelope, before the brain clamps
        return SweepServerVna(cfg, dialect)
    if driver == "pna":
        from .pna import PnaVna
        return PnaVna(cfg)
    raise ValueError(f"hardware.driver must be one of {tuple(ANALYSERS)}, got {driver!r}")
