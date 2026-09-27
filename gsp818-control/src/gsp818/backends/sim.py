"""The simulated GSP-818: a noise floor that follows RBW, VBW, attenuation and
the preamp, a few carriers on the input, and -- tracking generator on -- a
device under test (the physics is in `gsp818.model`).

This file adds what makes it an INSTRUMENT: sweeps take time, and the bench is
latched when a sweep starts, so changing the DUT halfway through a 10-second
sweep cannot hand back a trace that is half one filter and half another.
"""

from __future__ import annotations

import copy

import numpy as np

from .. import model
from ..config import Config
from ..model import SweepSettings


class SimulatedAnalyzer:
    simulated = True

    #: the settings a pretend front panel holds (the keys `read_state` returns)
    PANEL_KEYS = ("start_Hz", "stop_Hz", "points", "rbw_Hz", "rbw_auto", "vbw_Hz",
                  "vbw_auto", "ref_level_dBm", "atten_dB", "atten_auto", "sweep_time_s",
                  "sweep_time_auto", "detector", "preamp", "tg_on", "tg_level_dBm")

    def __init__(self, cfg: Config, seed: int | None = None, time_scale: float = 1.0,
                 state: dict | None = None):
        """`time_scale` multiplies every sweep time: 1.0 behaves like the real
        analyser (the GUI, the service), 0.0 makes tests instant.

        `state` = what the pretend instrument is set to BEFORE the service
        connects (its front panel). The brain READS it at start and adopts it
        (Lukas's rule, 2026-09-27: starting the software must not change the
        instrument). None = the .ini values, i.e. the .ini plays the role of
        the instrument's memory -- so the simulator behaves as before for a
        user, while tests pass a deliberately non-default state to prove the
        adoption."""
        self.cfg = cfg
        self.time_scale = float(time_scale)
        self._rng = np.random.default_rng(seed)
        self._open = False
        self._pending = None
        sw, tg = cfg.sweep, cfg.tracking
        self.panel = {
            "start_Hz": float(sw.start_Hz), "stop_Hz": float(sw.stop_Hz),
            "points": int(sw.points), "rbw_Hz": float(sw.rbw_Hz), "rbw_auto": bool(sw.rbw_auto),
            "vbw_Hz": float(sw.vbw_Hz), "vbw_auto": bool(sw.vbw_auto),
            "ref_level_dBm": float(sw.ref_level_dBm), "atten_dB": float(sw.atten_dB),
            "atten_auto": bool(sw.atten_auto), "sweep_time_s": float(sw.sweep_time_s),
            "sweep_time_auto": bool(sw.sweep_time_auto), "detector": str(sw.detector),
            "preamp": bool(sw.preamp), "tg_on": bool(tg.tg_on),
            "tg_level_dBm": float(tg.level_dBm)}
        if state:
            unknown = set(state) - set(self.PANEL_KEYS)
            if unknown:
                raise ValueError(f"unknown panel keys {sorted(unknown)}")
            self.panel.update(state)
        #: every panel setting the software CHANGED, in order: (key, value).
        #: Tests assert it stays empty through start-up.
        self.writes: list[tuple[str, object]] = []

    @property
    def tg_output(self) -> bool:
        """What the pretend GEN OUTPUT is doing (tests check it)."""
        return bool(self.panel["tg_on"])

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        # Connecting changes nothing on the instrument (the TG keeps doing what
        # it was doing): only queries are allowed at start.
        self._open = True

    def close(self) -> None:
        # SHUTDOWN behaviour is kept as it was: the TG is switched off when the
        # service stops, so nothing is left driving a DUT unattended.
        if self.panel["tg_on"]:
            self.panel["tg_on"] = False
            self.writes.append(("tg_on", False))
        self._open = False
        self._pending = None

    def read_state(self) -> dict:
        """What the pretend front panel is set to (queries only)."""
        if not self._open:
            raise RuntimeError("simulated analyser is not open")
        return dict(self.panel)

    def mark_in_sync(self, s: SweepSettings) -> None:
        """Nothing to do: the simulator knows its whole panel."""

    def ensure_live(self) -> list[str]:
        """The simulated trace is always a fresh sweep."""
        return []

    def idn(self) -> str:
        return "AaltoFlow,simulated GSP-818 spectrum analyser,0,sim (not a real instrument)"

    # ---- sweeping ------------------------------------------------------------
    def configure(self, s: SweepSettings) -> dict:
        if not self._open:
            raise RuntimeError("simulated analyser is not open")
        # Change only what differs (like the real backend) and record it, so a
        # test can prove that start-up wrote nothing.
        new = {"start_Hz": s.start_Hz, "stop_Hz": s.stop_Hz, "points": int(s.points),
               "rbw_auto": s.rbw_auto, "vbw_auto": s.vbw_auto,
               "ref_level_dBm": s.ref_level_dBm, "atten_auto": s.atten_auto,
               "sweep_time_auto": s.sweep_time_auto, "detector": s.detector,
               "preamp": s.preamp, "tg_level_dBm": s.tg_level_dBm, "tg_on": s.tg_on}
        # a manual value is only a setting while its auto is off (front panel)
        for key, auto in (("rbw_Hz", s.rbw_auto), ("vbw_Hz", s.vbw_auto),
                          ("atten_dB", s.atten_auto), ("sweep_time_s", s.sweep_time_auto)):
            if not auto:
                new[key] = getattr(s, key)
        for key, value in new.items():
            if self.panel.get(key) != value:
                self.panel[key] = value
                self.writes.append((key, value))
        return {"rbw_Hz": s.rbw_Hz, "vbw_Hz": s.vbw_Hz, "atten_dB": s.atten_dB,
                "sweep_time_s": s.sweep_time_s}

    def start_sweep(self, s: SweepSettings) -> float:
        if not self._open:
            raise RuntimeError("simulated analyser is not open")
        self._pending = (s, copy.copy(self.cfg.bench))
        return s.sweep_time_s * self.time_scale

    def finish_sweep(self) -> tuple[np.ndarray, dict]:
        p, self._pending = self._pending, None
        if p is None:
            raise RuntimeError("finish_sweep without start_sweep")
        s, bench = p
        return model.simulate(s, bench, self._rng)

    def abort_sweep(self) -> None:
        self._pending = None
