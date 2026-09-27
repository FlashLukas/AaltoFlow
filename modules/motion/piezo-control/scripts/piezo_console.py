"""Standalone raw-protocol console for the piezo service.

Speaks the wire protocol with ONLY pyzmq + json -- no `piezo` package import --
so you can copy this single file to any machine to poke a running service.

    python scripts/piezo_console.py                    # interactive REPL
    python scripts/piezo_console.py --host 192.168.0.5 # remote service
    python scripts/piezo_console.py status             # one-shot command

REPL examples:
    status
    move_axis X 50
    move_axis 1 25.5
    move_xy 100 80
    move_relative X 10           (move to 10 um measured from the zero)
    set_closed_loop X 1          (1 = closed loop / servo on, 0 = open loop)
    set_closed_loop Y 0
    set_velocity X 100           (um/s -- native slew rate or software ramp)
    set_ramp_mode software       (software | hardware | off)
    set_zero                     (zero all)   |   set_zero X
    clear_zero                   (back to absolute)   |   clear_zero X
    stop                         (all axes)   |   stop X
    store_position 0 corner
    goto_position 0
    save_positions positions.json
    quit
"""

from __future__ import annotations

import argparse
import json
import sys

import zmq

DEFAULT_CMD_PORT = 5561  # keep in sync with protocol.py

# How to turn "verb arg arg" typed at the prompt into a JSON command dict.
_AXIS = {"x": "X", "y": "Y", "0": "X", "1": "Y"}


def build_request(line: str) -> dict:
    parts = line.split()
    verb = parts[0]
    a = parts[1:]

    def axis(v):  # pass through X/Y or 0/1
        return _AXIS.get(v.lower(), v)

    def truthy(v):
        return 1 if v.lower() in ("1", "true", "yes", "on", "closed", "cl") else 0

    if verb in ("status", "info", "get_config", "get_positions"):
        return {"cmd": verb}
    if verb == "move_axis":
        return {"cmd": "move_axis", "axis": axis(a[0]), "position": float(a[1])}
    if verb == "move_xy":
        return {"cmd": "move_xy", "x": float(a[0]), "y": float(a[1])}
    if verb == "move_relative":
        return {"cmd": "move_relative", "axis": axis(a[0]), "value": float(a[1])}
    if verb == "set_closed_loop":
        return {"cmd": "set_closed_loop", "axis": axis(a[0]), "enabled": bool(truthy(a[1]))}
    if verb == "set_velocity":
        return {"cmd": "set_velocity", "axis": axis(a[0]), "value": float(a[1])}
    if verb == "set_ramp_mode":
        return {"cmd": "set_ramp_mode", "mode": a[0]}
    if verb == "set_zero":
        return {"cmd": "set_zero", "axis": axis(a[0]) if a else None}
    if verb == "clear_zero":
        return {"cmd": "clear_zero", "axis": axis(a[0]) if a else None}
    if verb == "stop":
        return {"cmd": "stop", "axis": axis(a[0]) if a else None}
    if verb == "store_position":
        return {"cmd": "store_position", "slot": int(a[0]), "name": " ".join(a[1:])}
    if verb == "clear_position":
        return {"cmd": "clear_position", "slot": int(a[0])}
    if verb == "goto_position":
        return {"cmd": "goto_position", "slot": int(a[0])}
    if verb == "save_positions":
        return {"cmd": "save_positions", "path": a[0]}
    if verb == "load_positions":
        return {"cmd": "load_positions", "path": a[0]}
    # fall through: treat the whole line as raw JSON
    return json.loads(line)


def main() -> None:
    ap = argparse.ArgumentParser(description="raw console for the piezo service")
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

    print(f"piezo console -> tcp://{args.host}:{args.port}  (type 'quit' to exit)")
    while True:
        try:
            line = input("piezo> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("quit", "exit"):
            break
        send(line)


if __name__ == "__main__":
    main()
