"""Simulated hardware: the two generator outputs (W1, W2) of an Analog Discovery.

Implements WaveGen (base.py), so the generator brain cannot tell it from the
real device (backends/dwf.py), and behaves like the device where it matters:

  * at open both outputs are OFF (the Analog Discovery starts with its
    generator disabled -- read on the lab's AD2, 2026-10-08), so start-up
    adopts "off" and changes nothing;
  * a value outside the range is clipped, as the device's driver does
    (dwf clips silently; the brain's read-back then shows the clipped value);
  * no load setting: the volts are what the output drives.

`output_value(i, t)` is the voltage on output i at time t: the simulated
scope in Analog Discovery mode (backends/sim.py, `SimulatedADScope`) watches
W1 on CH1 and W2 on CH2 -- the loopback wiring of the lab bench.
"""

from __future__ import annotations

import numpy as np

from . import waveforms

_SHAPES = ("sine", "square", "pulse", "ramp", "noise", "dc")

#: the Analog Discovery 2's outputs (datasheet / FDwfAnalogOut*Info on the
#: lab's AD2): +-5 V, 14 bit; the frequency the driver accepts goes far
#: beyond the analog bandwidth (~12 MHz), so the envelope stops at 20 MHz.
AD2_ENVELOPE = {"freq_min_Hz": 1e-3, "freq_max_Hz": 20e6,
                "amp_min_Vpp": 0.0, "amp_max_Vpp": 10.0, "peak_max_V": 5.0,
                "duty_min_pct": 0.0, "duty_max_pct": 100.0}


def ad_envelope(waveform: str, info: dict | None = None) -> dict:
    """The range for one waveform. `info` (from the device) overrides the
    defaults; DC and noise have no frequency."""
    env = dict(AD2_ENVELOPE)
    env.update({k: v for k, v in (info or {}).items() if v is not None})
    if waveform in ("dc", "noise"):
        # (as the AFG's envelope: no frequency to set for these)
        env["freq_min_Hz"] = env["freq_max_Hz"] = None
    return env


class SimulatedADGen:
    """Pretends to be W1/W2 of an Analog Discovery 2."""

    simulated = True

    def __init__(self):
        base = {"output": False, "waveform": "sine", "frequency_Hz": 1000.0,
                "amplitude_Vpp": 1.0, "offset_V": 0.0, "phase_deg": 0.0,
                "duty_pct": 50.0, "symmetry_pct": 50.0, "load_ohm": None,
                "mode": "continuous"}
        self.ch = [dict(base), dict(base)]
        self.writes: list[tuple] = []      # every command that changes an output
        self._open = False
        self.t0 = 0.0                      # when the outputs (re)started together
        self.phase_epoch = 0

    # ---- lifecycle -------------------------------------------------------
    def open(self) -> None:
        self._open = True

    def close(self, outputs_off: bool = True) -> None:
        if self._open and outputs_off:
            for i in range(len(self.ch)):
                self.set_output(i, False)
        self._open = False

    def capabilities(self) -> dict:
        return {"model": "Analog Discovery 2 (simulated)", "channels": len(self.ch),
                "waveforms": list(_SHAPES), "phase_align": True,
                "phase_resolution_deg": 0.0, "load_settable": False,
                "ramp_symmetry": True}

    def envelope(self, waveform: str, load_ohm=None) -> dict:
        return ad_envelope(waveform)

    # ---- reading ---------------------------------------------------------
    def read_channel(self, ch: int, full: bool = True) -> dict:
        return dict(self.ch[ch], unread=[])

    def output_value(self, ch: int, t):
        """Volts on output `ch` at time(s) t (scalar or numpy array)."""
        c = self.ch[ch]
        t = np.asarray(t, dtype=float)
        if not c["output"]:
            return np.zeros_like(t)
        return np.vectorize(lambda x: waveforms.value(c, x - self.t0))(t)

    # ---- writing (clipped like the driver) -------------------------------
    def _clip(self, v, lo, hi):
        return min(max(float(v), lo), hi)

    def set_output(self, ch: int, on: bool) -> None:
        self.writes.append(("set_output", ch, bool(on)))
        self.ch[ch]["output"] = bool(on)

    def set_waveform(self, ch: int, waveform: str) -> None:
        if waveform not in _SHAPES:
            raise ValueError(f"cannot select {waveform!r}")
        self.writes.append(("set_waveform", ch, waveform))
        self.ch[ch]["waveform"] = waveform

    def set_frequency(self, ch: int, hz: float) -> None:
        self.writes.append(("set_frequency", ch, float(hz)))
        e = AD2_ENVELOPE
        self.ch[ch]["frequency_Hz"] = self._clip(hz, e["freq_min_Hz"], e["freq_max_Hz"])

    def set_amplitude(self, ch: int, vpp: float) -> None:
        self.writes.append(("set_amplitude", ch, float(vpp)))
        self.ch[ch]["amplitude_Vpp"] = self._clip(vpp, 0.0, AD2_ENVELOPE["amp_max_Vpp"])

    def set_offset(self, ch: int, volts: float) -> None:
        self.writes.append(("set_offset", ch, float(volts)))
        pk = AD2_ENVELOPE["peak_max_V"]
        self.ch[ch]["offset_V"] = self._clip(volts, -pk, pk)

    def set_phase(self, ch: int, deg: float) -> None:
        self.writes.append(("set_phase", ch, float(deg)))
        self.ch[ch]["phase_deg"] = float(deg) % 360.0

    def set_duty(self, ch: int, pct: float) -> None:
        self.writes.append(("set_duty", ch, float(pct)))
        self.ch[ch]["duty_pct"] = self._clip(pct, 0.0, 100.0)

    def set_symmetry(self, ch: int, pct: float) -> None:
        self.writes.append(("set_symmetry", ch, float(pct)))
        self.ch[ch]["symmetry_pct"] = self._clip(pct, 0.0, 100.0)

    def set_load(self, ch: int, load_ohm) -> None:
        raise ValueError("the Analog Discovery's outputs have no load setting")

    def align_phase(self) -> None:
        # W2 slaved to W1: both restart together (dwf FDwfAnalogOutMasterSet)
        self.writes.append(("align_phase",))
        self.phase_epoch += 1

    def drain_errors(self) -> list[str]:
        return []

    def idn(self) -> str:
        return "Digilent,Analog Discovery 2,SIMULATED" if self._open else ""
