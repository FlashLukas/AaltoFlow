"""The hardware interface -- what the rest of the code is allowed to assume.

We use typing.Protocol, Python's "structural interface": any object that has
these methods counts as a WaveGen, whether it is the real Tektronix AFG1062 or
the simulator. The brain (Generator) depends ONLY on this interface, so
swapping real hardware for the simulator changes nothing above this line.

GENERIC ON PURPOSE. A waveform generator is a waveform generator: the same
brain will drive the two generator outputs of the Digilent Analog Discovery 3
inside the planned scope module (docs/ROADMAP.md). So the backend says what
it CAN do -- `capabilities()` and `envelope()` -- and the brain builds its
clamps, describe and GUI from that, never from model numbers of its own.

Channels are numbered from 0 here (0 = CH1); the wire and the GUI say
"ch1"/"ch2" and the brain translates.

Clamping to the LAB's safety limits is the brain's job; the envelope is the
instrument's own range. The backend does no threading of its own: exactly ONE
thread (the brain's worker) ever calls it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class WaveGen(Protocol):
    """An N-channel function / arbitrary waveform generator."""

    def open(self) -> None:
        """Connect -- and CHANGE NOTHING. Only queries may go out here
        (Lukas's rule 2026-09-27: every module reads the instrument's state at
        start and adopts it; a restart of the PC or of the service must never
        switch an output or change a waveform)."""

    def close(self) -> None:
        """Every output OFF, then disconnect. Safe on shutdown / crash, and
        after a failed open (then it sends nothing). Shutdown is NOT covered
        by the read-only start rule: outputs off on the way out, deliberately
        (CH1 may drive a magnet)."""

    def capabilities(self) -> dict:
        """What this instrument offers::

            {"model": "AFG1062", "channels": 2,
             "waveforms": ["sine", "square", "pulse", "ramp", "noise", "dc"],
             "phase_align": True,        # can re-align the channels' phases
             "load_settable": True}      # has a "load" (50 ohm / high-Z) setting
        """

    def envelope(self, waveform: str, load_ohm: float | None) -> dict:
        """The instrument's own range for one waveform at one load setting
        (volts into that load)::

            {"freq_min_Hz", "freq_max_Hz", "amp_min_Vpp", "amp_max_Vpp",
             "peak_max_V", "duty_min_pct", "duty_max_pct"}
        """

    def read_channel(self, ch: int) -> dict:
        """What one channel is doing right now, read with queries only::

            {"output": bool, "waveform": "sine"|...|"arb",
             "frequency_Hz", "amplitude_Vpp", "offset_V", "phase_deg",
             "duty_pct", "symmetry_pct",
             "load_ohm": float | None (None = high-Z),
             "mode": "continuous" | "burst" | "sweep" | "modulated",
             "unread": ["amplitude_Vpp", ...],
             "not_read_back": ["symmetry_pct", ...]}     # optional

        A value whose read FAILED this time is None and named in `unread`
        (the brain then decides nothing from this read). A value the
        instrument CANNOT report at all (a firmware without that query) is
        left OUT and named in `not_read_back`: the brain keeps the value it
        last set, and status says so. Never poll such a query -- each failed
        query leaves an error in the instrument's queue."""

    def set_output(self, ch: int, on: bool) -> None: ...
    def set_waveform(self, ch: int, waveform: str) -> None: ...
    def set_frequency(self, ch: int, hz: float) -> None: ...
    def set_amplitude(self, ch: int, vpp: float) -> None: ...
    def set_offset(self, ch: int, volts: float) -> None: ...
    def set_phase(self, ch: int, deg: float) -> None: ...
    def set_duty(self, ch: int, pct: float) -> None: ...
    def set_symmetry(self, ch: int, pct: float) -> None: ...
    def set_load(self, ch: int, load_ohm: float | None) -> None:
        """None = high-Z."""

    def align_phase(self) -> None:
        """Restart both channels' phase together, so a phase offset set
        between two channels at the same frequency is what comes out."""

    def drain_errors(self) -> list[str]:
        """Empty the instrument's error queue; [] when there were none."""

    def idn(self) -> str:
        """A one-line identification (model, serial, firmware). '' if unknown."""
