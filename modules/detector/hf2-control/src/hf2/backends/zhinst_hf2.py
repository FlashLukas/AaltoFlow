"""The real HF2LI, through the LabOne Python API (`zhinst-core`).

This is the only file that imports `zhinst`, and it does so LAZILY (inside
open()), so the package imports and the simulator runs on a PC without LabOne.

How the pieces connect on the lab PC:

    HF2LI --USB--> ziServer (the HF2 data server, installed by LabOne)
                      ^ TCP localhost:8005
                      |
                   zhinst.core.ziDAQServer(host, 8005, api_level=1)   <- this file

The HF2 series uses its own data server on port 8005 with API level 1. The
newer instruments (MFLI, UHFLI) use port 8004 and level 6; mixing those up is
the classic first error.

Everything on the instrument is a NODE in a tree, e.g. /dev1234/demods/0/timeconstant.
LabOne's "Tree" tab shows the same paths, so any doubt about a path below can be
settled by clicking there. Every call not yet checked against the real HF2LI is
marked `# VERIFY`.

Checklist for the first hardware session:
  1. LabOne installed, HF2LI visible in the LabOne web UI; note its id (devNNNN).
  2. Uncomment `zhinst-core` in pyproject.toml (same version as LabOne), uv sync.
  3. Put the id in the config (hardware.device_id) or pass --device to run_service.
  4. Run the service with --real and exercise it from hf2_console.py first.
  5. Compare every value with the LabOne UI side by side, especially the
     external-reference (PLL) nodes and the aux-input fields of the sample.
"""

from __future__ import annotations

import math
from typing import Sequence

from ..config import Channel
from ..hwlock import HardwareLock, claim


class ZhinstHF2:
    """Drives a physical HF2LI. Implements the LockInBackend interface."""

    def __init__(self, device_id: str, host: str = "localhost", port: int = 8005,
                 api_level: int = 1, interface: str = "USB"):
        self._dev = device_id.lower()
        self._host = host
        self._port = int(port)
        self._api_level = int(api_level)
        self._interface = interface
        self._daq = None
        self._idn = ""
        self._last_aux = [math.nan, math.nan]
        # The claim on the physical HF2LI (hwlock.py): held from open() to
        # close(), so a second service cannot drive the same lock-in.
        self._lock: HardwareLock | None = None

    # ---- node helpers ---------------------------------------------------------

    def _p(self, rel: str) -> str:
        return f"/{self._dev}/{rel}"

    def _set_d(self, rel: str, value: float) -> None:
        self._daq.setDouble(self._p(rel), float(value))

    def _set_i(self, rel: str, value: int) -> None:
        self._daq.setInt(self._p(rel), int(value))

    # ---- lifecycle ------------------------------------------------------------

    def open(self) -> None:
        # CLAIM THE INSTRUMENT FIRST (Lukas's rule: one physical instrument, one
        # service). The HF2LI is identified by its device id (devNNNN, which is
        # its serial number) -- NOT by the data server's host:port, because one
        # ziServer can serve several lock-ins and two services talking to two
        # different devices through it are fine. Two services on the SAME
        # devNNNN are not: each would change the other's time constants.
        # Claimed before ziDAQServer is even created, so a refused start never
        # sends a byte to the server or the instrument.
        self._lock = claim(self._dev, "hf2")
        try:
            import zhinst.core                      # lazy: only needed for real hw
            self._daq = zhinst.core.ziDAQServer(self._host, self._port, self._api_level)
            # The HF2 data server attaches USB devices on its own; connectDevice is
            # harmless if it already has. VERIFY on the lab PC.
            try:
                self._daq.connectDevice(self._dev, self._interface)
            except RuntimeError:
                pass
            # A read that fails here means a wrong device id -- fail loudly now
            # rather than returning zeros at 20 Hz later.
            devtype = self._daq.getString(self._p("features/devtype"))    # VERIFY node
            serial = self._daq.getString(self._p("features/serial"))      # VERIFY node
            self._idn = f"Zurich Instruments,{devtype or 'HF2'},{serial or self._dev}"
            # Deliberately NOT touching /sigouts: this module never drives an output.
            # And nothing else is written here either: open() is queries only, the
            # brain then READS each channel (read_channel) and adopts it.
        except BaseException:
            # A failed open (no LabOne, server down, wrong id) must not leave
            # the device claimed: drop the server connection and the claim.
            self.close()
            raise

    def close(self) -> None:
        try:
            if self._daq is not None:
                try:
                    self._daq.disconnect()
                finally:
                    self._daq = None
        finally:
            # Released LAST, after the connection is gone, so a new service
            # can never claim the device while this one still talks to it.
            if self._lock is not None:
                self._lock.release()
                self._lock = None

    def idn(self) -> str:
        return self._idn

    # ---- reading the instrument's state (start-up adoption) ---------------------

    def _get_d(self, rel: str) -> float:
        return float(self._daq.getDouble(self._p(rel)))

    def _get_i(self, rel: str) -> int:
        return int(self._daq.getInt(self._p(rel)))

    def read_channel(self, demod: int) -> dict:
        """Queries only (getInt/getDouble). Called at start so the module
        ADOPTS whatever LabOne / the last user left on the instrument, instead
        of overwriting it with the .ini. Every node here is the read twin of a
        node setup_channel()/set_reference() writes; the ones not yet seen on
        the real HF2LI are marked VERIFY like their write twins."""
        d = int(demod)
        i = self._get_i(f"demods/{d}/adcselect")                        # VERIFY
        o = self._get_i(f"demods/{d}/oscselect")                        # VERIFY
        pll_on = False
        ref_input = o
        try:
            pll_on = bool(self._get_i(f"plls/{o}/enable"))              # VERIFY
            ref_input = self._get_i(f"plls/{o}/adcselect")              # VERIFY
        except RuntimeError:
            # No PLL option on this unit: then it cannot be on external reference.
            pass
        # The input front end. adcselect 0/1 are the two signal inputs; other
        # values (aux inputs, VERIFY the numbering) have no sigins node, and a
        # read of one would abort the whole start. Then report None and the
        # brain keeps its configured front-end values (and says so).
        front = {"input_range_V": None, "input_ac": None,
                 "input_50ohm": None, "input_diff": None}
        try:
            front = {
                "input_range_V": self._get_d(f"sigins/{i}/range"),          # VERIFY
                "input_ac": bool(self._get_i(f"sigins/{i}/ac")),             # VERIFY
                "input_50ohm": bool(self._get_i(f"sigins/{i}/imp50")),       # VERIFY
                "input_diff": bool(self._get_i(f"sigins/{i}/diff")),         # VERIFY
            }
        except RuntimeError:
            pass
        return {
            **front,
            "enabled": bool(self._get_i(f"demods/{d}/enable")),         # VERIFY
            "signal_input": i,
            "oscillator": o,
            "harmonic": self._get_i(f"demods/{d}/harmonic"),
            "phase_deg": self._get_d(f"demods/{d}/phaseshift"),
            "rate_Sa_s": self._get_d(f"demods/{d}/rate"),               # VERIFY
            "time_constant_s": self._get_d(f"demods/{d}/timeconstant"),
            "order": self._get_i(f"demods/{d}/order"),
            "frequency_Hz": self._get_d(f"oscs/{o}/freq"),
            "reference": "external" if pll_on else "internal",
            "ref_input": ref_input,
        }

    # ---- per-channel set-up (only on an explicit Apply, never at start) -----------

    def setup_channel(self, ch: Channel, rate_Sa_s: float) -> None:
        d, i = int(ch.demod), int(ch.signal_input)
        # signal input
        self._set_d(f"sigins/{i}/range", ch.input_range_V)             # VERIFY
        self._set_i(f"sigins/{i}/ac", int(ch.input_ac))                 # VERIFY
        self._set_i(f"sigins/{i}/imp50", int(ch.input_50ohm))           # VERIFY
        self._set_i(f"sigins/{i}/diff", int(ch.input_diff))             # VERIFY
        # demodulator routing and data stream
        self._set_i(f"demods/{d}/adcselect", i)                         # VERIFY
        self._set_i(f"demods/{d}/oscselect", int(ch.oscillator))        # VERIFY
        self._set_i(f"demods/{d}/harmonic", max(1, int(ch.harmonic)))
        self._set_d(f"demods/{d}/phaseshift", ch.phase_deg)
        self._set_d(f"demods/{d}/rate", rate_Sa_s)
        self._set_i(f"demods/{d}/enable", 1)
        self._daq.sync()      # block until the server has applied all of the above

    # ---- reference ----------------------------------------------------------------

    def set_reference(self, oscillator: int, external: bool, ref_input: int) -> None:
        # On the HF2 an external reference is a PLL that drives the oscillator.
        # PLL n drives oscillator n; `adcselect` picks the reference input.
        # VERIFY all three nodes and the adcselect numbering (signal inputs 0/1,
        # aux inputs probably 2/3) against LabOne's Tree tab. Some HF2LI units
        # need the PLL option for this.
        p = int(oscillator)
        if external:
            self._set_i(f"plls/{p}/adcselect", int(ref_input))          # VERIFY
            self._set_i(f"plls/{p}/enable", 1)                          # VERIFY
        else:
            self._set_i(f"plls/{p}/enable", 0)                          # VERIFY
        self._daq.sync()

    def pll_locked(self, oscillator: int) -> bool:
        try:
            return bool(self._daq.getInt(self._p(f"plls/{int(oscillator)}/locked")))  # VERIFY
        except RuntimeError:
            return False

    def set_oscillator_frequency(self, oscillator: int, hz: float) -> None:
        self._set_d(f"oscs/{int(oscillator)}/freq", hz)
        self._daq.sync()

    # ---- demodulator filter ------------------------------------------------------

    def set_time_constant(self, demod: int, tc_s: float) -> None:
        self._set_d(f"demods/{int(demod)}/timeconstant", tc_s)
        self._daq.sync()

    def get_time_constant(self, demod: int) -> float:
        return float(self._daq.getDouble(self._p(f"demods/{int(demod)}/timeconstant")))

    def set_order(self, demod: int, order: int) -> None:
        self._set_i(f"demods/{int(demod)}/order", order)
        self._daq.sync()

    def get_order(self, demod: int) -> int:
        return int(self._daq.getInt(self._p(f"demods/{int(demod)}/order")))

    # ---- data -----------------------------------------------------------------------

    def read_demods(self, demods: Sequence[int]) -> list[dict]:
        out = []
        for d in demods:
            # getSample returns the most recent demodulator sample as a dict of
            # one-element arrays: x, y, frequency, phase, auxin0, auxin1, ...
            # VERIFY the key names (and that it is still available in the
            # installed zhinst-core; the fallback is subscribe + poll).
            s = self._daq.getSample(self._p(f"demods/{int(d)}/sample"))
            out.append({"x": _first(s["x"]), "y": _first(s["y"]),
                        "freq_Hz": _first(s["frequency"])})
            # The HF2 puts both aux inputs into every demodulator sample, so
            # the aux reading comes for free with the demod read.
            if "auxin0" in s:
                self._last_aux = [_first(s["auxin0"]), _first(s.get("auxin1", [math.nan]))]
        return out

    def read_aux(self) -> list[float]:
        # Filled in by read_demods (the brain always reads demods first).
        return list(self._last_aux)


def _first(v) -> float:
    """First element of a numpy array / list, or the value itself."""
    try:
        return float(v[0])
    except (TypeError, IndexError):
        return float(v)
