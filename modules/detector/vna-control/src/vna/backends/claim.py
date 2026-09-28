"""Which physical analyser a VISA address names -- for the hardware lock.

WHY: Lukas's rule (2026-09-27) -- "the same instrument has to be defined by the
same physical address". Two services must never drive one analyser at the same
time, so a real backend claims the analyser's address in `vna.hwlock` BEFORE it
sends the first byte, and holds the claim until `close()`. `hwlock.normalize`
already makes "TCPIP0::10.0.0.5::hislip0::INSTR" and "10.0.0.5" the same box.
Two cases need one more step, and that step lives here:

1. A VISA ALIAS. The PNA's default address is "N5222A", a name the VISA
   library (Keysight Connection Expert / NI MAX) maps to the real address.
   Claimed as the text "N5222A", it would NOT conflict with a second service
   given the real "TCPIP0::a-n5222a-1234::hislip0::INSTR" -- one box, two
   locks. So the alias is resolved to its real address first. Resolving is a
   lookup in the VISA library's own table on this PC; nothing goes to the
   instrument.

2. A SOCKET ON THIS PC (127.0.0.1 / localhost). The C1209 is not on the
   network: S2VNA, running on this PC, owns it over USB and serves SCPI on a
   local port (5025). The host "127.0.0.1" therefore names THIS PC, not the
   analyser -- and other programs on the same PC serve on it too (the
   DynaCool's MultiPyVu server, say). Claimed as just the host, a C1209 and a
   PPMS would lock each other out. So for a loopback address the PORT is the
   instrument: we claim "LOCALHOST:<port>". Both spellings of loopback give the
   same claim, so two vna services on one S2VNA still collide as they must.
"""

from __future__ import annotations

import re

from .. import hwlock

MODULE = "vna"   # the name another service sees in "already in use by vna (pid N)"

_LOOPBACK = {"127.0.0.1", "LOCALHOST", "::1", "[::1]"}


def lock_address(resource: str, rm=None) -> str:
    """The address to claim for VISA `resource` (see the module docstring).

    `rm` = an open pyvisa ResourceManager, used only to resolve an alias; None
    (tests, or no VISA) claims the text as given."""
    name = str(resource).strip()
    if rm is not None:
        try:
            # VERIFY: pyvisa's resource_info() returns the resolved
            # `resource_name` for a Keysight/NI alias ("N5222A" ->
            # "TCPIP0::...::INSTR"). It is viParseRsrcEx, a local table
            # lookup -- no I/O to the instrument. pyvisa-py knows no aliases
            # and may raise; then the text as given is the best we have.
            info = rm.resource_info(name)
            resolved = getattr(info, "resource_name", None)
            if resolved:
                name = str(resolved)
        except Exception:
            pass
    u = name.upper()
    # TCPIP0::<host>::<port>::SOCKET or <host>:<port> -- is the host this PC?
    m = (re.fullmatch(r"TCPIP\d*::(\[?[^:\]]+\]?|\[[^\]]+\])::(\d+)::SOCKET", u)
         or re.fullmatch(r"(LOCALHOST|127\.0\.0\.1|\[::1\]):(\d+)", u))
    if m and m.group(1) in _LOOPBACK:
        return f"LOCALHOST:{int(m.group(2))}"
    return name


def claim(resource: str, rm=None) -> hwlock.HardwareLock:
    """Claim the analyser at `resource` for this module, or raise
    hwlock.HardwareBusy naming the service that holds it."""
    return hwlock.claim(lock_address(resource, rm), MODULE)
