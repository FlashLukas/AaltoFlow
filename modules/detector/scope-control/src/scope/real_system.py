"""Build the brain on the REAL instrument (the service's and the GUI's --real).

Which one: cfg.hardware.driver --
  "siglent"  the RS PRO RSDS1102CML+ (= Siglent SDS1102CML+) over VISA;
  "dwf"      a Digilent Analog Discovery 2 / 3 through WaveForms' dwf library:
             scope + generator W1/W2 (the generator brain, `scope.gen`) +
             V+/V- supplies, ONE device shared by the parts (backends/dwf.py).
The vendor libraries are imported only here, only for the chosen driver.
"""

from __future__ import annotations

from .config import Config
from .scope import Scope


def build_real_system(cfg: Config, gen_cfg=None) -> Scope:
    driver = (cfg.hardware.driver or "siglent").strip().lower()
    if driver == "dwf":
        from .backends.dwf import DwfDevice, DwfScope, DwfWaveGen
        from .generator.brain import Generator
        from .generator.config import GenConfig
        dev = DwfDevice(cfg.hardware.dwf_device)
        return Scope(DwfScope(dev), cfg,
                     gen=Generator(DwfWaveGen(dev), gen_cfg or GenConfig()))
    if driver != "siglent":
        raise ValueError(f"unknown hardware.driver {driver!r} (use 'siglent' or 'dwf')")
    from .backends.siglent import SiglentSDS
    return Scope(SiglentSDS(cfg.hardware.visa, timeout_ms=cfg.hardware.timeout_ms), cfg)
