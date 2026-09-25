"""Headless client demo: drive a running service over the network.

Start the service in one terminal:
    uv run scripts/run_service.py
Then run this in another:
    uv run scripts/client_demo.py --connect localhost

It connects, seeks to +40 mT, waits for STABLE, then demagnetises -- all over
ZeroMQ. Same code works across the lab network; just change the host.

This used to hand-roll its own polling loop, and that loop was the reference
implementation of the settle primitive. It now lives on ClMagClient itself
(`set_field_blocking` / `wait_idle`), so there is one copy of it, tested, and
scan-core wraps the same method. What is left here is a demo of the API.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from clMag.net.client import ClMagClient
from clMag.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--connect", default="localhost")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    args = ap.parse_args()

    client = ClMagClient(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port)
    client._on_event = lambda lvl, msg: print(f"  [{lvl:5}] {msg}")
    info = client.start()
    print(f"connected: {info}\n")

    try:
        # A miniature scan: three points, each read only once the field is there.
        # This is exactly what scan-core's engine does through a Settable.
        for target in (40.0, 0.0, -40.0):
            print(f"-- seek to {target:+.1f} mT --")
            t0 = time.monotonic()
            st = client.set_field_blocking(target, timeout_s=30.0)
            print(f"   settled in {time.monotonic()-t0:5.2f} s: "
                  f"measured={st.measured_field_mT:+.3f} mT  current={st.current_A:+.3f} A")

        print("\n-- demagnetise --")
        t0 = time.monotonic()
        client.demag(1.5)
        st = client.wait_idle(timeout_s=60.0)
        print(f"   done in {time.monotonic()-t0:5.2f} s: measured={st.measured_field_mT:+.3f} mT")
    except TimeoutError as exc:
        # The helpers raise rather than returning a flag, so a stuck magnet is
        # impossible to miss. The service's event stream (printed above) says why.
        print(f"\nTIMEOUT: {exc}")
        return 1
    finally:
        client.shutdown()

    print("\nclient done (service still running)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
