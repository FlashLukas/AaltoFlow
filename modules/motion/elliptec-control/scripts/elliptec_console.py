"""Standalone raw-protocol console for the elliptec service.

Speaks the wire protocol with ONLY pyzmq + json -- no `elliptec` package
import -- so this single file can be copied to any machine to poke a running
service.

    python scripts/elliptec_console.py                     # interactive REPL
    python scripts/elliptec_console.py --host 192.168.0.5  # remote service
    python scripts/elliptec_console.py status              # one-shot command

REPL examples (an axis is its index 0..n-1, or @<address> such as @A):
    status
    info
    move_abs 0 45          (turn axis 0 to 45 deg)
    move_rel 0 -10         (turn axis 0 by -10 deg)
    home                   (all)   |   home 0   |   home 0 ccw
    stop                   (all)   |   stop @1
    set_velocity 0 60      (percent of maximum speed)
    set_zero 0             (call the current angle 0 deg)
    set_offset 0 12.5      |   clear_zero 0
    describe
    {"cmd": "status"}      (any raw JSON)
    quit
"""

from __future__ import annotations

import argparse
import json
import sys

import zmq

DEFAULT_CMD_PORT = 5607  # keep in sync with module.toml


def build_request(line: str) -> dict:
    parts = line.split()
    verb, a = parts[0], parts[1:]

    def axis(v):  # "0" -> 0, "@A" stays a string for the service to resolve
        return v if v.startswith("@") else int(v)

    if verb.startswith("{"):
        return json.loads(line)
    if verb in ("status", "info", "get_config", "describe", "shutdown"):
        return {"cmd": verb}
    if verb == "move_abs":
        return {"cmd": "move_abs", "axis": axis(a[0]), "angle_deg": float(a[1])}
    if verb == "move_rel":
        return {"cmd": "move_rel", "axis": axis(a[0]), "delta_deg": float(a[1])}
    if verb == "home":
        req = {"cmd": "home", "axis": axis(a[0]) if a else None}
        if len(a) > 1:
            req["direction"] = a[1]
        return req
    if verb == "stop":
        return {"cmd": "stop", "axis": axis(a[0]) if a else None}
    if verb in ("set_velocity", "set_offset"):
        return {"cmd": verb, "axis": axis(a[0]), "value": float(a[1])}
    if verb in ("set_zero", "clear_zero"):
        return {"cmd": verb, "axis": axis(a[0])}
    # anything else: a bare verb (e.g. an action id such as home_0)
    return {"cmd": verb}


def main() -> None:
    ap = argparse.ArgumentParser(description="raw console for the elliptec service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("oneshot", nargs="*", help="optional single command to run then exit")
    args = ap.parse_args()

    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 3000)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(f"tcp://{args.host}:{args.port}")

    def send(line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            req = build_request(line)
        except Exception as exc:
            print(f"! parse error: {exc}")
            return
        sock.send_json(req)
        try:
            print(json.dumps(sock.recv_json(), indent=2))
        except zmq.Again:
            print("! timeout (is the service running?)")
            sock.close(0)
            sys.exit(1)

    if args.oneshot:
        send(" ".join(args.oneshot))
        return

    print(f"elliptec console -> tcp://{args.host}:{args.port}  (type 'quit' to exit)")
    while True:
        try:
            line = input("elliptec> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("quit", "exit"):
            break
        send(line)


if __name__ == "__main__":
    main()
