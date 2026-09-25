"""Standalone raw-protocol console for the Z-piezo service (pyzmq + json only).

    python scripts/zpiezo_console.py                 # REPL
    python scripts/zpiezo_console.py status
    python scripts/zpiezo_console.py set_voltage 7.5
"""

from __future__ import annotations

import argparse
import json
import sys

import zmq

DEFAULT_CMD_PORT = 5565


def build_request(line: str) -> dict:
    parts = line.split()
    verb, a = parts[0], parts[1:]
    if verb in ("status", "info", "get_config", "read_voltage"):
        return {"cmd": verb}
    if verb == "set_voltage":
        return {"cmd": "set_voltage", "volts": float(a[0])}
    return json.loads(line)


def main() -> None:
    ap = argparse.ArgumentParser(description="raw console for the z-piezo service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("oneshot", nargs="*")
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
    print(f"z-piezo console -> tcp://{args.host}:{args.port}  (type 'quit' to exit)")
    while True:
        try:
            line = input("z> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("quit", "exit"):
            break
        send(line)


if __name__ == "__main__":
    main()
