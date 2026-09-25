"""The simulated VNA: S-parameters of a waveguide with a YIG film, in the field
the brain reads from the magnet service (or a manual value).

The physics is in `vna.model`; this file adds what makes it an INSTRUMENT --
sweeps take time, the conditions are latched when a sweep starts, and there is
noise.

Latching at `start_sweep` matters: the sample and line settings are COPIED
there, so changing the damping halfway through a 10-second sweep cannot hand
back a trace that is half one sample and half another. The field reading comes
in with the same call -- the magnet in a scan has already settled before the VNA
is triggered, so the field at the start of the sweep is the field of the sweep.
"""

from __future__ import annotations

import copy

import numpy as np

from .. import model
from ..config import Config
from ..field import FieldReading


class SimulatedVna:
    simulated = True

    def __init__(self, cfg: Config, seed: int | None = None, time_scale: float = 1.0):
        """`time_scale` multiplies every sweep time: 1.0 behaves like a real
        analyser (the GUI, the service), 0.0 makes tests instant."""
        self.cfg = cfg
        self.time_scale = float(time_scale)
        self._rng = np.random.default_rng(seed)
        self._open = False
        self._pending = None

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False
        self._pending = None

    def idn(self) -> str:
        return "AaltoFlow simulated VNA, S-parameters of a YIG film (not a real instrument)"

    # ---- sweeping ------------------------------------------------------------
    def sweep_time_s(self, points: int, ifbw_Hz: float) -> float:
        return model.sweep_time_s(points, ifbw_Hz) * self.time_scale

    def start_sweep(self, freqs_Hz, ifbw_Hz: float, power_dBm: float,
                    sparam: str = "S21", field: FieldReading | None = None) -> None:
        if not self._open:
            raise RuntimeError("simulated VNA is not open")
        if sparam not in model.SPARAMS:
            raise ValueError(f"sparam must be one of {model.SPARAMS}, got {sparam!r}")
        field = field or FieldReading(0.0, False, "none", float("nan"))
        self._pending = {
            "freqs": np.asarray(freqs_Hz, dtype=float),
            "ifbw": float(ifbw_Hz), "power": float(power_dBm), "sparam": sparam,
            "sample": copy.copy(self.cfg.sample), "line": copy.copy(self.cfg.line),
            "field": field,
        }

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        p, self._pending = self._pending, None
        if p is None:
            raise RuntimeError("finish_sweep without start_sweep")
        fr = p["field"]
        z = model.s21_measured(p["freqs"], fr.field_mT, p["sample"], p["line"],
                               p["ifbw"], p["power"], self._rng,
                               angle_deg=fr.angle_deg, sparam=p["sparam"])
        return z, {"f_res_model_Hz": model.kittel_Hz(fr.field_mT, p["sample"], fr.angle_deg)}

    def abort_sweep(self) -> None:
        self._pending = None
