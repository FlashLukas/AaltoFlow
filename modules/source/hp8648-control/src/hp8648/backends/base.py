"""The hardware interface -- what the rest of the code is allowed to assume.

We use typing.Protocol, Python's "structural interface": any object that has
these methods counts as a SigGenBackend, whether it is the real HP 8648D or the
simulator. The brain (`source.SignalSource`) depends ONLY on this interface, so
swapping real hardware for the simulator changes nothing above this line.

Every setter commands the value directly. Clamping to the safety limits is the
brain's job, not the backend's. Only the brain's worker thread calls these, so a
backend needs no locking of its own.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: Bits of STATus:QUEStionable:POWer:CONDition? (Operation and Service Guide,
#: "Reverse Power Protection Status" and "Unspecified Power Entry Status").
RPP_BIT = 0x01            # reverse-power protection has tripped
UNSPECIFIED_BIT = 0x02    # the level is above the specified range


@runtime_checkable
class SigGenBackend(Protocol):
    """A programmable CW RF signal generator (the HP / Agilent 8648D)."""

    def open(self) -> None:
        """Connect and get ready to READ. Must not change the instrument's
        state (Lukas's rule, 2026-09-27: "all modules should read the instrument
        state on startup, not to change anything"): no *RST, no RF off, no
        modulation off, no unit/reference/attenuator changes. Clearing the
        status/error queue (*CLS) is allowed -- it changes nothing the sample
        can feel. The brain then reads the state back and adopts it."""

    def close(self, rf_off: bool = True) -> None:
        """Turn RF off and disconnect. Safe to call on shutdown/crash.
        rf_off=False: a restart (shutdown{keep_outputs}) -- disconnect and
        release the address, but leave the RF output as it is."""

    def set_output(self, on: bool) -> None:
        """RF output on/off (OUTP:STAT ON|OFF). Turning it ON also re-arms a
        tripped reverse-power protection."""

    def read_output(self) -> bool:
        """True if the RF output is on (OUTP:STAT?)."""

    def set_power(self, dBm: float) -> None:
        """Output level in dBm (POW:AMPL <v> DBM)."""

    def read_power(self) -> float:
        """Programmed output level in dBm (POW:AMPL?)."""

    def set_frequency(self, hz: float) -> None:
        """CW frequency in Hz (FREQ:CW <v> HZ)."""

    def read_frequency(self) -> float:
        """Programmed CW frequency in Hz (FREQ:CW?)."""

    def read_power_condition(self) -> int:
        """The POWer questionable-condition register: RPP_BIT, UNSPECIFIED_BIT."""

    def read_modulation(self) -> dict:
        """{"am": bool, "fm": bool, "pm": bool} -- which modulations are on."""

    def startup_notes(self) -> list[str]:
        """Things found at open() that the operator should know about, e.g.
        "POWer reference mode is ON -- levels are converted in software".
        The brain turns each into a `warn` event. [] when nothing unusual."""

    def drain_errors(self) -> list[str]:
        """Empty the instrument's error queue (SYST:ERR?). [] when clean."""

    def idn(self) -> str:
        """Identification string (*IDN?). '' if unknown."""
