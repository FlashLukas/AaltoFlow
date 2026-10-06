"""The hardware interface -- what the rest of the code may assume of a scope.

typing.Protocol, Python's structural interface: anything with these methods
counts as a ScopeBackend -- the simulator, the Siglent SDS1000CML+ (the lab's
RS PRO RSDS1102CML+), later the Digilent Analog Discovery 3. The brain
depends only on this.

GENERIC ON PURPOSE (docs/ROADMAP.md, "Oscilloscope module"): the backend says
what it has in `capabilities()` -- how many channels, an external trigger
input, how many generator outputs -- and the brain, describe and the GUI
follow. A scope without a generator shows the Generator tab greyed out.

Channels are named "ch1", "ch2" (front-panel names). Volts are at the probe
tip (the scope's probe factor applied). The backend does no threading of its
own: the brain's ONE trace thread calls it (plus settings, under one lock).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ScopeBackend(Protocol):

    def open(self) -> None:
        """Connect and CHANGE NOTHING (Lukas's rule 2026-09-27): only queries
        may go out -- a scope that was showing something keeps showing it."""

    def close(self) -> None:
        """Disconnect. A scope drives nothing, so there is nothing to make safe;
        the front panel is handed back (Go To Local where the bus has it)."""

    def capabilities(self) -> dict:
        """{"model", "channels": ["ch1", "ch2"], "ext_trigger": bool,
            "generator_channels": 0, "max_points": int}"""

    def read_settings(self) -> dict:
        """The scope's own settings, queries only::

            {"channels": {"ch1": {"enabled", "vdiv_V", "offset_V", "coupling",
                                  "probe"}, ...},
             "tdiv_s", "delay_s", "sample_rate_Hz",
             "trigger": {"source", "level_V", "slope", "mode"},
             "unread": [...]}

        A value that could not be read is None and named in `unread`."""

    # ---- settings (each may be snapped by the instrument; read back after) ----
    def set_channel(self, ch: str, **values) -> None:
        """Any of enabled, vdiv_V, offset_V, coupling, probe."""

    def set_timebase(self, tdiv_s: float | None = None, delay_s: float | None = None) -> None: ...

    def set_trigger(self, **values) -> None:
        """Any of source, level_V, slope, mode."""

    # ---- traces -----------------------------------------------------------------
    def new_trace_ready(self) -> bool:
        """True once the scope has triggered and acquired a NEW record since the
        last call (the call itself re-arms the question). Never blocks long."""

    def read_traces(self, channels: list[str], max_points: int) -> tuple:
        """(t_s, {ch: volts}) of the latest record, every array the same length,
        t = 0 at the trigger. At most about `max_points` points per channel."""

    def idn(self) -> str:
        """One-line identification; '' if unknown."""
