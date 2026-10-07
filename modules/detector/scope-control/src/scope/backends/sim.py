"""Simulated hardware: a two-channel scope watching a simulated bench.

Implements ScopeBackend, so the brain cannot tell it from the real scope, and
behaves like one where it matters for the module:

  * it TRIGGERS: a record exists only when the trigger source crosses the
    level with the chosen slope (normal / single mode); "auto" free-runs at
    ~20 Hz when nothing triggers; "stop" makes no records. So averaging,
    acquire-and-wait and the stopped-scope timeout are all testable;
  * the record is centred on the trigger plus the delay, 14 divisions wide;
  * it CLIPS at the screen (+-4 divisions around the offset) and quantises to
    8 bits (25 codes per division), like the real ADC -- a V/div that is too
    small shows up as flat tops, as it does on the bench;
  * V/div and time/div snap to the instrument's steps (the brain reads back);
  * fresh noise in every record, so averaging really averages.

The world (config group `sim`):
  scene "moke"  CH1 = Hall probe: hall_V_per_mT * B, B = field_amp * sin(phase);
                CH2 = light intensity: a hysteresis loop in B (tanh branches
                with coercive field hc and bias), Kerr half-jump ms_V, a linear
                Faraday slope, noise and slow drift, around 0.5 V;
                EXT = a sync square, high for the first half period.
  scene "bench" the lab bench of 2026-10-06: CH1 = the AFG's sine,
                CH2 = its synchronous square, which also feeds EXT.
"""

from __future__ import annotations

import math
import time

import numpy as np

_H_DIV = 14                    # horizontal divisions of the record (# VERIFY real scope)
_CODES_PER_DIV = 25.0          # 8-bit ADC: 25 codes per division
_NATIVE_POINTS = 7000          # samples per record in the simulated memory

_VDIV_STEPS = [m * 10.0 ** e for e in range(-3, 2) for m in (1, 2, 5)]  # 1 mV .. 50 V
_VDIV_STEPS = [v for v in _VDIV_STEPS if 2e-3 <= v <= 10.0]
_TDIV_STEPS = [m * 10.0 ** e for e in range(-9, 2) for m in (1, 2.5, 5)]
_TDIV_STEPS = [v for v in _TDIV_STEPS if 2.5e-9 <= v <= 50.0]


def _snap(value: float, steps: list) -> float:
    return min(steps, key=lambda s: abs(math.log(s) - math.log(max(value, 1e-12))))


class SimulatedScope:
    simulated = True

    def __init__(self, sim_cfg, clock=time.monotonic, seed: int | None = None):
        self.sim = sim_cfg                 # the live config group: edits apply at once
        self._clock = clock
        self._rng = np.random.default_rng(seed)
        self.settings = {
            "channels": {
                # what the pretend scope shows BEFORE the service starts -- not
                # the config defaults, so a test can tell adopt from push
                "ch1": {"enabled": True, "vdiv_V": 0.5, "offset_V": 0.0,
                        "coupling": "dc", "probe": 1.0},
                "ch2": {"enabled": True, "vdiv_V": 0.1, "offset_V": -0.5,
                        "coupling": "dc", "probe": 1.0},
            },
            "tdiv_s": 5e-3, "delay_s": 0.0,
            "trigger": {"source": "ext", "level_V": 0.5, "slope": "rising",
                        "mode": "normal"},
        }
        self.writes: list[tuple] = []      # every command that changes the scope
        self._open = False
        self._last_record_t = -1e9
        self._record = None                 # (t, {ch: volts}) of the latest record
        self._t_start = clock()

    # ---- lifecycle ---------------------------------------------------------------
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def capabilities(self) -> dict:
        return {"model": "SDS1102CML+ (simulated)", "channels": ["ch1", "ch2"],
                "ext_trigger": True, "generator_channels": 0,
                "max_points": _NATIVE_POINTS}

    def idn(self) -> str:
        return "Siglent Technologies,SDS1102CML+,SIMULATED,1.0" if self._open else ""

    # ---- settings ----------------------------------------------------------------
    def read_settings(self) -> dict:
        s = self.settings
        span = _H_DIV * s["tdiv_s"]
        return {"channels": {c: dict(v) for c, v in s["channels"].items()},
                "tdiv_s": s["tdiv_s"], "delay_s": s["delay_s"],
                "sample_rate_Hz": _NATIVE_POINTS / span,
                "trigger": dict(s["trigger"]), "unread": []}

    def set_channel(self, ch: str, **values) -> None:
        self.writes.append(("set_channel", ch, dict(values)))
        c = self.settings["channels"][ch]
        for k, v in values.items():
            if k == "vdiv_V":
                v = _snap(float(v), _VDIV_STEPS)
            elif k == "offset_V":
                lim = 50.0 * c["vdiv_V"] if c["vdiv_V"] < 0.1 else 40.0
                v = max(-lim, min(lim, float(v)))
            c[k] = v

    def set_timebase(self, tdiv_s=None, delay_s=None) -> None:
        self.writes.append(("set_timebase", tdiv_s, delay_s))
        if tdiv_s is not None:
            self.settings["tdiv_s"] = _snap(float(tdiv_s), _TDIV_STEPS)
        if delay_s is not None:
            self.settings["delay_s"] = float(delay_s)

    def set_trigger(self, **values) -> None:
        self.writes.append(("set_trigger", dict(values)))
        self.settings["trigger"].update(values)

    # ---- the simulated world --------------------------------------------------------
    def _signals(self, phase: np.ndarray, t_abs: np.ndarray, noise: bool) -> dict:
        """Volts on CH1, CH2 and EXT at drive `phase` (radians, may be an array)."""
        sim = self.sim
        sync = np.where(np.mod(phase, 2 * np.pi) < np.pi, sim.square_V, 0.0)
        if sim.scene == "bench":
            ch1 = sim.field_amp_mT * sim.hall_V_per_mT * np.sin(phase)
            ch2 = sync.copy()
            ext = sync
        else:
            b = sim.field_amp_mT * np.sin(phase)
            rising = np.cos(phase) > 0                    # dB/dt > 0
            width = max(0.05 * sim.field_amp_mT, 1e-6)    # switching width
            centre = np.where(rising, sim.hc_mT + sim.bias_mT, -sim.hc_mT + sim.bias_mT)
            m = np.tanh((b - centre) / width)
            ch1 = sim.hall_V_per_mT * b
            ch2 = 0.5 + sim.ms_V * m + sim.slope_V_per_mT * b + sim.drift_V_per_s * t_abs
            ext = sync
        if noise and sim.noise_V > 0:
            ch1 = ch1 + self._rng.normal(0.0, sim.noise_V / 2, np.shape(phase))
            ch2 = ch2 + self._rng.normal(0.0, sim.noise_V, np.shape(phase))
        return {"ch1": ch1, "ch2": ch2, "ext": ext, "ext5": ext, "line": np.sin(phase)}

    def _trigger_phase(self) -> float | None:
        """Drive phase at which the trigger condition is met, or None."""
        trg = self.settings["trigger"]
        src = trg["source"]
        if src not in ("ch1", "ch2", "ext", "ext5", "line"):
            return None
        ph = np.linspace(0, 2 * np.pi, 3601)
        y = self._signals(ph, np.zeros_like(ph), noise=False)[src]
        lvl = float(trg["level_V"])
        if trg["slope"] == "rising":
            hit = np.flatnonzero((y[:-1] < lvl) & (y[1:] >= lvl))
        else:
            hit = np.flatnonzero((y[:-1] > lvl) & (y[1:] <= lvl))
        return float(ph[hit[0] + 1]) if hit.size else None

    def new_trace_ready(self) -> bool:
        trg = self.settings["trigger"]
        mode = trg["mode"]
        if mode == "stop":
            return False
        now = self._clock()
        period = 1.0 / max(self.sim.drive_Hz, 1e-3)
        span = _H_DIV * self.settings["tdiv_s"]
        # a record needs its own span plus ~5 ms of dead time, and the next
        # trigger edge; never faster than the drive repeats
        dead = max(period, span + 0.005)
        phi = self._trigger_phase()
        if phi is None:
            if mode != "auto" or now - self._last_record_t < 0.05:
                return False
            phi = float(self._rng.uniform(0, 2 * np.pi))     # free-running
        elif now - self._last_record_t < dead:
            return False
        self._last_record_t = now
        self._make_record(phi, now - self._t_start)
        if mode == "single":
            trg["mode"] = "stop"
        return True

    def _make_record(self, phi: float, t_abs: float) -> None:
        s = self.settings
        span = _H_DIV * s["tdiv_s"]
        # the real scope's convention (measured 2026-10-07): time = +delay -
        # span/2 + i / sample_rate, i.e. a POSITIVE delay moves the window
        # LATER, the trigger stays at t = 0
        t = np.linspace(-span / 2, span / 2, _NATIVE_POINTS, endpoint=False) + s["delay_s"]
        phase = phi + 2 * np.pi * self.sim.drive_Hz * t
        sig = self._signals(phase, t_abs + t, noise=True)
        out = {}
        for ch in ("ch1", "ch2"):
            c = s["channels"][ch]
            v = sig[ch]
            if c["coupling"] == "ac":
                v = v - np.mean(v)
            elif c["coupling"] == "gnd":
                v = np.zeros_like(v)
            # the screen is +-4 divisions around the offset: clip and quantise
            lsb = c["vdiv_V"] / _CODES_PER_DIV
            lo, hi = -4 * c["vdiv_V"] - c["offset_V"], 4 * c["vdiv_V"] - c["offset_V"]
            v = np.clip(v, lo, hi)
            out[ch] = np.round((v + c["offset_V"]) / lsb) * lsb - c["offset_V"]
        self._record = (t, out)

    def read_traces(self, channels, max_points):
        if self._record is None:
            raise RuntimeError("no record yet")
        t, data = self._record
        step = max(1, int(math.ceil(t.size / max(1, int(max_points)))))
        return t[::step].copy(), {ch: data[ch][::step].copy() for ch in channels}
