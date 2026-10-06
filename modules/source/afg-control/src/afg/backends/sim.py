"""Simulated hardware: a fake Tektronix AFG1062.

It implements the WaveGen interface from `base`, so the brain cannot tell it
apart from the real instrument, and it misbehaves the way the real one does:

  * it REFUSES a setting outside its range the way the AFG does: the value is
    clipped to what it can make and an error ("-222,\"Data out of range\"")
    goes into the error queue -- so the brain's readback check is exercised;
  * the amplitude / offset range follows the LOAD setting (50 ohm vs high-Z),
    and |offset| + amplitude/2 may not pass the peak limit;
  * it starts in a state of its own (SIM_BOOT_STATE), not the config defaults,
    so a test can tell "adopted at start" from "pushed the defaults".

`output_value(ch, t)` gives the voltage on a connector at time t: the scope
module's simulator will watch these two outputs, wired as on the lab bench
(CH1 -> scope CH1, CH2 -> scope CH2 and EXT TRIG).
"""

from __future__ import annotations

from .tek_afg import afg1062_envelope
from .. import waveforms

_SHAPES = ("sine", "square", "pulse", "ramp", "noise", "dc")

#: What the pretend AFG is doing BEFORE the service starts -- deliberately not
#: the config defaults: CH1 left driving a 30 Hz sine, CH2 a square at the same
#: frequency (the bench setup), CH2's output off, both at Load 50 ohm.
SIM_BOOT_STATE = {
    "channels": [
        {"output": True, "waveform": "sine", "frequency_Hz": 30.0,
         "amplitude_Vpp": 2.0, "offset_V": 0.0, "phase_deg": 0.0,
         "duty_pct": 50.0, "symmetry_pct": 50.0, "load_ohm": 50.0},
        {"output": False, "waveform": "square", "frequency_Hz": 30.0,
         "amplitude_Vpp": 1.5, "offset_V": 0.75, "phase_deg": 0.0,
         "duty_pct": 50.0, "symmetry_pct": 50.0, "load_ohm": 50.0},
    ],
}


class SimulatedAFG:
    """Pretends to be a Tektronix AFG1062 (two channels)."""

    def __init__(self, boot: dict | None = None):
        boot = SIM_BOOT_STATE if boot is None else boot
        self.ch = [dict(c, mode="continuous") for c in boot["channels"]]
        # every command that CHANGES the instrument, in order -- so a test can
        # prove that start-up sends none (the read-only start rule)
        self.writes: list[tuple] = []
        self._errors: list[str] = []
        self._open = False
        self.phase_epoch = 0              # bumped by align_phase (tests)
        self.fail_reads = False           # tests: make every read raise

    # ---- lifecycle -------------------------------------------------------

    def open(self) -> None:
        self._open = True                 # connect, change nothing

    def close(self, outputs_off: bool = True) -> None:
        if self._open and outputs_off:
            for i in range(len(self.ch)):
                self.set_output(i, False)
        self._open = False

    def capabilities(self) -> dict:
        return {"model": "AFG1062 (simulated)", "channels": len(self.ch),
                "waveforms": list(_SHAPES), "phase_align": True,
                "load_settable": True}

    def envelope(self, waveform: str, load_ohm):
        return afg1062_envelope(waveform, load_ohm)

    # ---- reading ---------------------------------------------------------

    def read_channel(self, ch: int) -> dict:
        if self.fail_reads:
            raise OSError("simulated USB timeout")
        return dict(self.ch[ch], unread=[])

    def output_value(self, ch: int, t: float) -> float:
        """Volts on the connector at time t, into the declared load."""
        return waveforms.value(self.ch[ch], t)

    # ---- writing (each one clips like the instrument) --------------------

    def _err(self, what: str) -> None:
        self._errors.append(f'-222,"Data out of range; {what}"')

    def _env(self, c: dict) -> dict:
        return afg1062_envelope(c["waveform"], c["load_ohm"])

    def set_output(self, ch: int, on: bool) -> None:
        self.writes.append(("set_output", ch, bool(on)))
        self.ch[ch]["output"] = bool(on)

    def set_waveform(self, ch: int, waveform: str) -> None:
        if waveform not in _SHAPES:
            raise ValueError(f"cannot select {waveform!r}")
        self.writes.append(("set_waveform", ch, waveform))
        c = self.ch[ch]
        c["waveform"] = waveform
        env = self._env(c)
        if env["freq_max_Hz"] is not None and c["frequency_Hz"] > env["freq_max_Hz"]:
            # the AFG pulls the frequency down to what the new shape can make
            c["frequency_Hz"] = env["freq_max_Hz"]
            self._err("frequency reduced for the new waveform")

    def set_frequency(self, ch: int, hz: float) -> None:
        self.writes.append(("set_frequency", ch, float(hz)))
        c = self.ch[ch]
        env = self._env(c)
        if env["freq_max_Hz"] is None:
            c["frequency_Hz"] = float(hz)
            return
        lo, hi = env["freq_min_Hz"], env["freq_max_Hz"]
        if not lo <= hz <= hi:
            self._err("frequency")
        c["frequency_Hz"] = min(max(float(hz), lo), hi)

    def set_amplitude(self, ch: int, vpp: float) -> None:
        self.writes.append(("set_amplitude", ch, float(vpp)))
        c = self.ch[ch]
        env = self._env(c)
        hi = min(env["amp_max_Vpp"], 2.0 * (env["peak_max_V"] - abs(c["offset_V"])))
        if not env["amp_min_Vpp"] <= vpp <= hi:
            self._err("amplitude")
        c["amplitude_Vpp"] = min(max(float(vpp), env["amp_min_Vpp"]), hi)

    def set_offset(self, ch: int, volts: float) -> None:
        self.writes.append(("set_offset", ch, float(volts)))
        c = self.ch[ch]
        env = self._env(c)
        room = env["peak_max_V"] - (0.0 if c["waveform"] == "dc" else c["amplitude_Vpp"] / 2)
        if abs(volts) > room:
            self._err("offset")
        c["offset_V"] = min(max(float(volts), -room), room)

    def set_phase(self, ch: int, deg: float) -> None:
        self.writes.append(("set_phase", ch, float(deg)))
        self.ch[ch]["phase_deg"] = waveforms.wrap_phase(deg)

    def set_duty(self, ch: int, pct: float) -> None:
        self.writes.append(("set_duty", ch, float(pct)))
        env = self._env(self.ch[ch])
        self.ch[ch]["duty_pct"] = min(max(float(pct), env["duty_min_pct"]),
                                      env["duty_max_pct"])

    def set_symmetry(self, ch: int, pct: float) -> None:
        self.writes.append(("set_symmetry", ch, float(pct)))
        self.ch[ch]["symmetry_pct"] = min(max(float(pct), 0.0), 100.0)

    def set_load(self, ch: int, load_ohm) -> None:
        self.writes.append(("set_load", ch, load_ohm))
        c = self.ch[ch]
        # like the AFG: the displayed volts are rescaled for the new load
        # (the output stage itself is unchanged)
        k = waveforms.load_factor(load_ohm) / waveforms.load_factor(c["load_ohm"])
        c["load_ohm"] = None if load_ohm is None else float(load_ohm)
        c["amplitude_Vpp"] *= k
        c["offset_V"] *= k

    def align_phase(self) -> None:
        self.writes.append(("align_phase",))
        self.phase_epoch += 1

    def drain_errors(self) -> list[str]:
        out, self._errors = self._errors, []
        return out

    def idn(self) -> str:
        return "TEKTRONIX,AFG1062,SIMULATED,FV:1.0" if self._open else ""
