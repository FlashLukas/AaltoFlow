"""Claiming clMag's physical instruments before anything talks to them.

WHY (Lukas's rule): "the same instrument has to be defined by the same
physical address." The Kepco BOP on GPIB0::6 can also be driven by the kepco
module, and the NI USB-6259 (Dev1) by another DAQ module. If two services
drove one box at once, each would undo the other's commands without knowing.
So before the first byte goes to the hardware, clMag claims every address it
will use through `hwlock` (an OS file lock, freed automatically when this
process ends, even on a crash).

WHY ONE CLAIM FOR THE WHOLE MODULE, not one per backend: clMag opens TWO
physical things, and the Hall probe and the AUX I/O are both on the SAME DAQ
card. If the Hall backend and the AUX backend each claimed "Dev1", the second
would be refused by the first -- inside one process. So the real system
(when it is written) claims all addresses here, ONCE, all-or-nothing, before
it constructs or opens any backend:

    claims = HardwareClaims.claim(physical_addresses(cfg))   # may raise HardwareBusy
    try:
        ... build + open the real Kepco / Hall / AUX backends ...
    except Exception:
        claims.release(); raise
    # keep `claims` alive as long as the instruments are open; release() in close()

A simulator claims NOTHING (build_sim_system never calls this).
"""

from __future__ import annotations

from typing import Iterable, List

from ..hwlock import HardwareBusy, HardwareLock, claim, normalize

MODULE = "clMag"  # the name another service sees in "already in use by clMag"

__all__ = ["MODULE", "HardwareBusy", "HardwareClaims", "physical_addresses", "daq_device"]


def daq_device(channel: str) -> str:
    """'Dev1/ai0' -> 'Dev1'; 'Dev1/port0/line2' -> 'Dev1'.

    An NI-DAQmx channel name starts with the DEVICE name (as shown in NI MAX);
    the device is the physical box, the rest is a pin on it."""
    return str(channel).strip().split("/", 1)[0].strip()


def physical_addresses(cfg) -> List[str]:
    """Every physical box the real clMag would open, from its config.

    - the Kepco's VISA resource (cfg.hardware.kepco_visa), and
    - every DAQ DEVICE named by the Hall channel and the AUX channel lists
      (normally all 'Dev1'; if someone moves the AUX lines to a second card,
      that card is claimed too).
    Duplicates -- after normalisation, so 'dev1' and 'Dev1' are one entry --
    are dropped, because claiming the same address twice in one process is
    refused by hwlock."""
    raw = [cfg.hardware.kepco_visa, daq_device(cfg.hardware.daq_channel)]
    raw += [daq_device(ch) for ch in (cfg.aux.ao_list() + cfg.aux.ai_list() + cfg.aux.do_list())]
    out, seen = [], set()
    for a in raw:
        if not a:
            continue
        n = normalize(a)
        if n not in seen:
            seen.add(n)
            out.append(a)
    return out


class HardwareClaims:
    """A set of held claims, taken all-or-nothing."""

    def __init__(self, locks: List[HardwareLock]):
        self._locks = locks

    @classmethod
    def claim(cls, addresses: Iterable[str], module: str = MODULE,
              wait_s: float = 2.0) -> "HardwareClaims":
        """Claim every address, or none: if one is busy, the ones already
        taken are released before HardwareBusy propagates. Otherwise a failed
        start would leave e.g. the Kepco marked "in use by clMag" while clMag
        is not running it."""
        locks: List[HardwareLock] = []
        try:
            for a in addresses:
                locks.append(claim(a, module, wait_s=wait_s))
        except BaseException:
            for lk in locks:
                lk.release()
            raise
        return cls(locks)

    @property
    def addresses(self) -> List[str]:
        """Normalised addresses still held (empty after release())."""
        return [lk.normalized for lk in self._locks if lk.held]

    def release(self) -> None:
        """Give every address back. Safe to call twice."""
        for lk in self._locks:
            lk.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
