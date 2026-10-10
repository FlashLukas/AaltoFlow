"""The hardware interface -- what the rest of the code is allowed to assume.

typing.Protocol is Python's "structural interface": any object that has these
methods counts as a TGSource, whether it is the simulator or the client of the
signalhound service. The Generator depends ONLY on this interface, so swapping
one for the other changes nothing above this line.

Why the interface looks different from smb's (one setter per knob there):

* ONE setter, `set_cw(on=, freq_hz=, level_dbm=)`, because that is the shape of
  the owner's `tg_cw` verb: a missing argument means "keep". A frequency change
  therefore never re-sends the level, and cannot switch the output by accident.

* ONE reader, `read_state()`, returning a whole snapshot. The frequency echo and
  the "an SNA sweep holds the TG" flag MUST come from the same moment: a scan
  accepts a frequency only when the echo matches AND the TG is not busy, and two
  separate reads could pair a fresh echo with a stale busy flag.

Clamping to the safety limits is the Generator's job, not the backend's.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TGSource(Protocol):
    """A tracking generator used as a CW source."""

    def open(self) -> None:
        """Connect. Must NOT change the TG's state (adopt-on-start rule)."""

    def close(self) -> None:
        """Disconnect. Changes nothing on the TG (switching off on a clean
        stop is the Generator's decision, see Hardware.off_on_shutdown)."""

    def set_cw(self, on: bool | None = None, freq_hz: float | None = None,
               level_dbm: float | None = None) -> dict | None:
        """Command the CW output; None = keep that part as it is.

        RAISES when the command is refused (TG busy with a sweep, no TG
        attached, out of range, owner not reachable). Returning means the
        command was ACCEPTED -- the applied value shows up in read_state().
        May return the owner's reply (a dict) or None; the only key anyone
        reads is `deferred` (True: accepted, applied a little later -- the
        owner's hardware was busy). The simulator applies at once (None)."""

    def read_state(self) -> dict:
        """One consistent snapshot. Never raises. Keys:

        rf_on (bool), frequency_Hz (float), power_dBm (float) -- the APPLIED
            values (for the remote backend: the analyser service's echo);
        parked (bool), park_Hz, park_dBm -- "off" is a PARK: the TG44A cannot
            be silenced, so it is moved to a park frequency at its minimum
            level; rf_on is False while parked;
        tg_busy (bool) -- a network-analyser sweep holds the TG right now;
        tg_unknown (bool) -- the TG's state could not be read (it may be
            emitting, left on by another program): the three values above
            are NOT known, nothing is adopted, and it stays unknown until
            someone sets a state explicitly;
        reachable (bool) -- the backend can talk to the TG at all;
        hw_error (str) -- "" when the values above are the TG's, else why not.
        """

    def idn(self) -> str:
        """A short identification string. '' if unknown."""
