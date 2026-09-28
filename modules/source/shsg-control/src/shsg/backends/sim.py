"""Simulated hardware: a fake USB-TG44A tracking generator.

Like the real TG44A it cannot be silenced: "off" means PARKED at 10 kHz and
-30 dBm (the owner's default park), and read_state() says so.

It implements the TGSource interface from `base`, so the Generator cannot tell
it apart from the real one. It runs standalone -- no signalhound service
needed -- which is what lets the module, its GUI and its tests run anywhere.

The "physics" is deliberately trivial: a CW source just remembers what you told
it. But it refuses exactly what the signalhound service would refuse, so the
brain's error paths are exercised offline too:
  * a command while a network-analyser sweep holds the TG (`simulate_sweep`),
  * a command when no TG is attached (`attached=False`),
  * a value outside the TG's HARDWARE range (only reachable if someone widened
    the config limits past it -- the brain clamps to the config first).

`startup` is the state the fake TG is ALREADY in when we connect; open()
changes none of it, so adopt-on-start is tested for real.
"""

from __future__ import annotations

from ..config import Signal

# The TG44A's own range (datasheet). # VERIFY on the rig. The config Limits
# default to the same numbers; these are the hardware's, not the user's.
HW_FREQ_HZ = (10.0, 4.4e9)
HW_LEVEL_DBM = (-30.0, -10.0)       # VERIFY -10 (Lukas tested -30 and -20)

# The TG44A has NO off: "off" parks it here (the owner's default park).
PARK_HZ = 10_000.0
PARK_DBM = HW_LEVEL_DBM[0]


class SimulatedTG44A:
    """Pretends to be a USB-TG44A reached through the analyser."""

    def __init__(self, startup: Signal | None = None, attached: bool = True):
        s = startup or Signal()
        self._freq = float(s.frequency_Hz)
        self._level = float(s.power_dBm)
        self._on = bool(s.rf_on)
        self._attached = bool(attached)
        self._busy = False
        self._unknown = False
        self._open = False
        self.commands: list[dict] = []     # every accepted set_cw, for tests

    # ---- lifecycle -------------------------------------------------------
    def open(self) -> None:
        # Connecting is not a command: the output stays as it is.
        self._open = True

    def close(self) -> None:
        self._open = False                 # changes nothing on the "TG"

    # ---- test / demo hooks ------------------------------------------------
    def simulate_sweep(self, running: bool) -> None:
        """Pretend a scalar network analyser sweep took (or gave back) the TG."""
        self._busy = bool(running)

    def simulate_unknown(self) -> None:
        """Pretend the TG state cannot be read (left on by another program).
        Like the real owner, the state is known again after the next command."""
        self._unknown = True

    def set_attached(self, attached: bool) -> None:
        self._attached = bool(attached)

    # ---- the interface -----------------------------------------------------
    def set_cw(self, on=None, freq_hz=None, level_dbm=None) -> None:
        # Same refusals, same order, as the signalhound service's tg_cw verb.
        if not self._attached:
            raise RuntimeError("no tracking generator attached")
        if self._busy:
            raise RuntimeError("busy: TG sweep running")
        if freq_hz is not None and not HW_FREQ_HZ[0] <= float(freq_hz) <= HW_FREQ_HZ[1]:
            raise ValueError(f"frequency {freq_hz:g} Hz outside the TG range "
                             f"{HW_FREQ_HZ[0]:g}..{HW_FREQ_HZ[1]:g} Hz")
        if level_dbm is not None and not HW_LEVEL_DBM[0] <= float(level_dbm) <= HW_LEVEL_DBM[1]:
            raise ValueError(f"level {level_dbm:g} dBm outside the TG range "
                             f"{HW_LEVEL_DBM[0]:g}..{HW_LEVEL_DBM[1]:g} dBm")
        if freq_hz is not None:
            self._freq = float(freq_hz)
        if level_dbm is not None:
            self._level = float(level_dbm)
        if on is not None:
            self._on = bool(on)
        self._unknown = False          # we just SET it: now it is known
        self.commands.append({"on": on, "freq_hz": freq_hz, "level_dbm": level_dbm})

    def read_state(self) -> dict:
        return {
            "rf_on": self._on,
            "parked": not self._on,          # the TG cannot be silenced: off = parked
            "park_Hz": PARK_HZ,
            "park_dBm": PARK_DBM,
            "frequency_Hz": self._freq,
            "power_dBm": self._level,
            "tg_busy": self._busy,
            "tg_unknown": self._unknown,
            "reachable": self._open,
            "hw_error": "" if self._attached else "no tracking generator attached",
        }

    def idn(self) -> str:
        return "Signal Hound USB-TG44A (SIMULATED)" if self._open else ""
