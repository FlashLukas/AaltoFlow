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

    def __init__(self, cfg: Config, seed: int | None = None, time_scale: float = 1.0,
                 state: dict | None = None):
        """`time_scale` multiplies every sweep time: 1.0 behaves like a real
        analyser (the GUI, the service), 0.0 makes tests instant.

        `state` = what the pretend analyser is ALREADY doing when the module
        connects (keys as in `read_state`), so a test can start it somewhere
        the config is not and check that the brain adopts it. None = it holds
        the config's sweep (taken at `open`), which keeps the service and the
        GUI starting exactly where the .ini says."""
        self.cfg = cfg
        self.time_scale = float(time_scale)
        self._rng = np.random.default_rng(seed)
        self._open = False
        self._pending = None
        self._state = dict(state) if state is not None else None

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if self._state is None:
            sw = self.cfg.sweep
            self._state = {"start_Hz": sw.start_Hz, "stop_Hz": sw.stop_Hz,
                           "points": sw.points, "ifbw_Hz": sw.ifbw_Hz,
                           "power_dBm": sw.power_dBm, "sparam": sw.sparam,
                           "continuous": self.cfg.acquisition.continuous}
        self._open = True

    def read_state(self) -> dict:
        """The pretend analyser's settings. `continuous` is the simulator's own
        free-running flag: showing live sweeps changes nothing real here."""
        return dict(self._state or {})

    def close(self) -> None:
        self._open = False
        self._pending = None

    def idn(self) -> str:
        return "AaltoFlow simulated VNA, S-parameters of a YIG film (not a real instrument)"

    # ---- sweeping ------------------------------------------------------------
    #: finish_sweep takes the field at the END of the sweep too (the brain asks
    #: only a backend that says so; a real analyser has no use for it)
    uses_field_end = True

    #: a sweep under a MOVING field is computed in this many segments, each at
    #: the field of its own moment (see finish_sweep)
    FIELD_SEGMENTS = 64

    def sweep_time_s(self, points: int, ifbw_Hz: float) -> float:
        return (model.sweep_time_s(points, ifbw_Hz, self.cfg.line.point_dwell_ifbw)
                * self.time_scale)

    def start_sweep(self, freqs_Hz, ifbw_Hz: float, power_dBm: float,
                    sparam: str = "S21", field: FieldReading | None = None) -> None:
        if not self._open:
            raise RuntimeError("simulated VNA is not open")
        if sparam not in model.SPARAMS:
            raise ValueError(f"sparam must be one of {model.SPARAMS}, got {sparam!r}")
        field = field or FieldReading(0.0, False, "none", float("nan"))
        # the pretend instrument now HOLDS these settings (what read_state reports)
        f = np.asarray(freqs_Hz, dtype=float)
        self._state = {**(self._state or {}), "start_Hz": float(f[0]),
                       "stop_Hz": float(f[-1]), "points": int(f.size),
                       "ifbw_Hz": float(ifbw_Hz), "power_dBm": float(power_dBm),
                       "sparam": sparam}
        self._pending = {
            "freqs": np.asarray(freqs_Hz, dtype=float),
            "ifbw": float(ifbw_Hz), "power": float(power_dBm), "sparam": sparam,
            "sample": copy.copy(self.cfg.sample), "line": copy.copy(self.cfg.line),
            "field": field,
        }

    def finish_sweep(self, field_end: FieldReading | None = None) -> tuple[np.ndarray, dict]:
        """The trace of the sweep just finished.

        `field_end` = the field when the sweep ENDED. A real VNA measures its
        points one after another, so when the field moves during a sweep (a
        fly scan ramping the magnet) the early points see the field of the
        start, the late ones that of the end. With both readings the sweep is
        computed in FIELD_SEGMENTS pieces, each at the field of its moment
        (linear in time between the two readings -- what a ramp is). Without
        one (a stepped scan: the magnet has settled) it is the start field
        throughout, as it always was."""
        p, self._pending = self._pending, None
        if p is None:
            raise RuntimeError("finish_sweep without start_sweep")
        fr = p["field"]
        f = p["freqs"]
        moving = (field_end is not None and np.isfinite(field_end.field_mT)
                  and np.isfinite(fr.field_mT) and field_end.field_mT != fr.field_mT)
        if not moving:
            z = model.s21_measured(f, fr.field_mT, p["sample"], p["line"],
                                   p["ifbw"], p["power"], self._rng,
                                   angle_deg=fr.angle_deg, sparam=p["sparam"])
        else:
            z = np.empty(f.size, dtype=complex)
            bounds = np.linspace(0, f.size, min(self.FIELD_SEGMENTS, f.size) + 1).astype(int)
            # the short way round: 179 -> -179 deg is 2 deg, not 358
            d_ang = (field_end.angle_deg - fr.angle_deg + 180.0) % 360.0 - 180.0
            if not np.isfinite(d_ang):
                d_ang = 0.0
            for a, b in zip(bounds[:-1], bounds[1:]):
                if b <= a:
                    continue
                frac = (0.5 * (a + b)) / f.size             # the segment's moment
                b_mT = fr.field_mT + (field_end.field_mT - fr.field_mT) * frac
                ang = fr.angle_deg + d_ang * frac
                z[a:b] = model.s21_measured(f[a:b], b_mT, p["sample"], p["line"],
                                            p["ifbw"], p["power"], self._rng,
                                            angle_deg=ang, sparam=p["sparam"])
        return z, {"f_res_model_Hz": model.kittel_Hz(fr.field_mT, p["sample"], fr.angle_deg)}

    def abort_sweep(self) -> None:
        self._pending = None
