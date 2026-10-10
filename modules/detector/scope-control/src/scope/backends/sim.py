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

The world (config group `sim`): two test signals and a sync --
  CH1  a sine;
  CH2  the same frequency, phase-shifted, with some 2nd harmonic and an
       offset (so the XY view is a tilted, slightly distorted ellipse);
  EXT  a sync square, high for the first half period.
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
                "record_points": _NATIVE_POINTS,
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
        ch1 = sim.ch1_amplitude_V * np.sin(phase)
        p2 = phase + np.radians(sim.ch2_phase_deg)
        ch2 = sim.ch2_offset_V + sim.ch2_amplitude_V * (
            np.sin(p2) + sim.ch2_harmonic * np.sin(2 * p2))
        if noise and sim.noise_V > 0:
            ch1 = ch1 + self._rng.normal(0.0, sim.noise_V, np.shape(phase))
            ch2 = ch2 + self._rng.normal(0.0, sim.noise_V, np.shape(phase))
        return {"ch1": ch1, "ch2": ch2, "ext": sync, "ext5": sync, "line": np.sin(phase)}

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
        period = 1.0 / max(self.sim.frequency_Hz, 1e-3)
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
        phase = phi + 2 * np.pi * self.sim.frequency_Hz * t
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


# ======================================================================================
# the Analog Discovery, simulated
# ======================================================================================

_AD_POINTS = 8192                 # the AD2's buffer (default device configuration)
_AD_RANGES = (5.0, 50.0)          # input ranges, peak-to-peak: V/div = range / 8
_AD_SOURCES = ("ch1", "ch2", "ext1", "ext2", "w1", "w2")
_AD_TRIGGER_OPTIONS = {"ch1": {"level": True, "slope": True},
                       "ch2": {"level": True, "slope": True},
                       "ext1": {"level": False, "slope": True},
                       "ext2": {"level": False, "slope": True},
                       "w1": {"level": False, "slope": False},
                       "w2": {"level": False, "slope": False}}


class SimulatedADScope(SimulatedScope):
    """An Analog Discovery 2 on the lab bench: its generator W1 looped back to
    its scope input 1, W2 to input 2 (Lukas, 2026-10-08). `gen` is the
    simulated generator (generator/sim.py); whatever the generator brain makes
    it output is what this scope sees, plus a little noise and a small input
    offset per channel (`ch_zero_V`, so a self-test has a zero to measure).
    T1 / T2 (ext1, ext2) are not connected: they never trigger (auto then
    free-runs). The V+ / V- supplies are simulated too (no load: they read
    back what they are set to)."""

    keeps_outputs_on_close = True

    def __init__(self, sim_cfg, gen, clock=time.monotonic, seed: int | None = None):
        super().__init__(sim_cfg, clock=clock, seed=seed)
        self.gen = gen
        self.ch_zero_V = {"ch1": 0.004, "ch2": -0.006}
        self.settings = {
            # the device's state at open (WaveForms' defaults): both inputs on,
            # 5 V range, auto, trigger on CH1 at 0 V
            "channels": {ch: {"enabled": True, "vdiv_V": _AD_RANGES[0] / 8.0,
                              "offset_V": 0.0, "coupling": "dc", "probe": 1.0}
                         for ch in ("ch1", "ch2")},
            "tdiv_s": 1e-3, "delay_s": 0.0,
            "trigger": {"source": "ch1", "level_V": 0.0, "slope": "rising", "mode": "auto"},
        }
        self.supplies = {"vplus": {"on": False, "V": 0.0}, "vminus": {"on": False, "V": 0.0}}

    def close(self, keep_outputs: bool = False) -> None:
        self._open = False

    def capabilities(self) -> dict:
        return {"model": "Analog Discovery 2 (simulated)", "channels": ["ch1", "ch2"],
                "ext_trigger": True, "generator_channels": 2, "max_points": _AD_POINTS,
                "couplings": ["dc"], "rolls": False,
                "trigger_sources": list(_AD_SOURCES),
                "trigger_options": _AD_TRIGGER_OPTIONS, "supplies": True}

    def idn(self) -> str:
        return "Digilent,Analog Discovery 2,SIMULATED" if self._open else ""

    def read_settings(self) -> dict:
        s = self.settings
        rate = _AD_POINTS / (10.0 * s["tdiv_s"])
        return {"channels": {c: dict(v) for c, v in s["channels"].items()},
                "tdiv_s": s["tdiv_s"], "delay_s": s["delay_s"],
                "sample_rate_Hz": rate, "record_points": _AD_POINTS,
                "trigger": dict(s["trigger"]), "unread": []}

    def set_channel(self, ch: str, **values) -> None:
        if values.get("coupling", "dc") != "dc":
            raise ValueError("the Analog Discovery's inputs are DC-coupled only")
        self.writes.append(("set_channel", ch, dict(values)))
        c = self.settings["channels"][ch]
        for k, v in values.items():
            if k == "vdiv_V":
                want = 8.0 * float(v)
                v = next((r for r in _AD_RANGES if r >= want * 0.999), _AD_RANGES[-1]) / 8.0
            c[k] = v

    def set_timebase(self, tdiv_s=None, delay_s=None) -> None:
        self.writes.append(("set_timebase", tdiv_s, delay_s))
        if tdiv_s is not None:
            # no steps: the rate follows the time/div (within 0.05 Hz .. 100 MHz)
            rate = min(max(_AD_POINTS / (10.0 * float(tdiv_s)), 0.05), 100e6)
            self.settings["tdiv_s"] = _AD_POINTS / rate / 10.0
        if delay_s is not None:
            self.settings["delay_s"] = float(delay_s)

    def set_trigger(self, **values) -> None:
        if values.get("source") is not None and values["source"] not in _AD_SOURCES:
            raise ValueError(f"the Analog Discovery has no trigger source {values['source']!r}")
        super().set_trigger(**values)

    # ---- the loopback -------------------------------------------------------------------
    def _volts(self, ch: str, t_abs, noise: bool):
        v = self.gen.output_value(0 if ch == "ch1" else 1, t_abs) + self.ch_zero_V[ch]
        if noise and self.sim.noise_V > 0:
            v = v + self._rng.normal(0.0, self.sim.noise_V, np.shape(t_abs))
        return v

    def _trigger_time(self, t_now: float, span: float):
        trg = self.settings["trigger"]
        src = trg["source"]
        if src in ("ext1", "ext2"):
            return None                               # nothing wired to T1 / T2
        idx = 0 if src in ("ch1", "w1") else 1
        c = self.gen.ch[idx]
        if not c["output"]:
            return None
        f = float(c["frequency_Hz"]) if c["waveform"] not in ("dc", "noise") else 0.0
        if src in ("w1", "w2"):
            if f <= 0:
                return None
            k = math.ceil((t_now - self.gen.t0) * f)
            return self.gen.t0 + k / f                # the generator's period start
        window = 2.0 / f if f > 0 else max(span, 1e-3)
        t = t_now + np.linspace(0.0, window, 4001)
        y = self._volts(src, t, noise=False)
        lvl = float(trg["level_V"])
        if trg["slope"] == "rising":
            hit = np.flatnonzero((y[:-1] < lvl) & (y[1:] >= lvl))
        else:
            hit = np.flatnonzero((y[:-1] > lvl) & (y[1:] <= lvl))
        return float(t[hit[0] + 1]) if hit.size else None

    def new_trace_ready(self) -> bool:
        trg = self.settings["trigger"]
        mode = trg["mode"]
        if mode == "stop":
            return False
        now = self._clock()
        span = 10.0 * self.settings["tdiv_s"]
        if now - self._last_record_t < span + 0.005:
            return False
        t_abs = now - self._t_start
        t_trig = self._trigger_time(t_abs, span)
        if t_trig is None:
            if mode != "auto" or now - self._last_record_t < 0.2:
                return False
            t_trig = t_abs                            # auto: free-runs
        self._last_record_t = now
        s = self.settings
        t = (np.arange(_AD_POINTS) - _AD_POINTS / 2.0) * (span / _AD_POINTS) + s["delay_s"]
        out = {}
        for ch in ("ch1", "ch2"):
            c = s["channels"][ch]
            v = self._volts(ch, t_trig + t, noise=True)
            lsb = 8.0 * c["vdiv_V"] / 16384.0          # 14 bit over the range
            lo, hi = -4 * c["vdiv_V"] - c["offset_V"], 4 * c["vdiv_V"] - c["offset_V"]
            out[ch] = np.round(np.clip(v, lo, hi) / lsb) * lsb
        self._record = (t, out)
        if mode == "single":
            trg["mode"] = "stop"
        return True

    # ---- V+ / V- -----------------------------------------------------------------------
    def read_supplies(self) -> dict:
        # like the AD2 (lab 2026-10-10): the supplies have NO readback -- the
        # measured fields are None, not an echo of the setting
        out = {k: {"on": s["on"], "V": s["V"], "V_meas": None, "A_meas": None}
               for k, s in self.supplies.items()}
        out["monitors"] = {"USB Monitor Voltage V": 4.756, "USB Monitor Current A": 0.2985,
                           "USB Monitor Temperature C": 39.0}
        return out

    def set_supply(self, which: str, on: bool | None = None, volts: float | None = None) -> None:
        if which not in self.supplies:
            raise ValueError(f"no {which} supply")
        self.writes.append(("set_supply", which, on, volts))
        lo, hi = self.supply_range(which)
        if volts is not None:
            self.supplies[which]["V"] = min(max(float(volts), lo), hi)
        if on is not None:
            self.supplies[which]["on"] = bool(on)

    def supply_range(self, which: str) -> tuple:
        # the AD2's settable ranges (FDwfAnalogIOChannelNodeSetInfo, lab 2026-10-10)
        return (0.5, 5.0) if which == "vplus" else (-5.0, -0.5)
