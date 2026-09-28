"""Where is the signalhound service? Ask the launcher first.

When Mission Control starts a service it sets AALTOFLOW_ENDPOINTS, a JSON map of
where every module listens: {"signalhound": ["localhost", 5587, 5588], ...}.
If a port was changed in the launcher (a per-PC override), only this variable
knows -- the shsg.ini would still name the old port and every TG command would
go nowhere. Same mechanism as camera-control uses to find kim.

Started by hand (no variable), the config is used exactly as it is.
"""

from __future__ import annotations

import json
import os

#: the module key of the owner of the Signal Hound USB devices
OWNER_KEY = "signalhound"


def apply_launcher_endpoints(cfg, environ=None) -> str:
    """Point cfg.hardware.owner_* at the launcher's signalhound endpoint.

    Returns a one-line ASCII note of what was applied ("" if nothing), for the
    service's start-up print. `environ` is for tests (default os.environ).
    """
    env = os.environ if environ is None else environ
    # AALTOFLOW_ENDPOINTS since the 2026-09-24 rename; the old name still works
    raw = env.get("AALTOFLOW_ENDPOINTS") or env.get("TRMOKE_ENDPOINTS")
    if not raw:
        return ""
    try:
        endpoints = json.loads(raw)
    except ValueError:
        return "AALTOFLOW_ENDPOINTS is not valid JSON, ignored"
    ep = endpoints.get(OWNER_KEY) if isinstance(endpoints, dict) else None
    if not ep or len(ep) != 3:
        return ""
    host, cmd, pub = ep
    hw = cfg.hardware
    hw.owner_host = "127.0.0.1" if host == "localhost" else str(host)
    hw.owner_cmd_port = int(cmd)
    hw.owner_pub_port = int(pub)
    return f"signalhound at {host}:{cmd}/{pub} (from the launcher)"
