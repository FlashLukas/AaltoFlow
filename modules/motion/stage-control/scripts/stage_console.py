"""Standalone raw-protocol console for the stage service.

Speaks the wire protocol with ONLY pyzmq + json -- no `stage` package import --
so you can copy this single file to any machine to poke a running service.

    python scripts/stage_console.py                    # interactive REPL
    python scripts/stage_console.py --host 192.168.0.5 # remote service
    python scripts/stage_console.py status             # one-shot command

REPL examples:
    status
    move_axis X 5
    move_axis 2 1.5
    home                 (all axes)   |   home Y
    set_zero             (zero all)   |   set_zero X    (zero one axis here)
    move_relative X 5              (move to 5 mm measured from the zero)
    clear_zero           (back to absolute)   |   clear_zero X
    set_velocity X 3
    set_matrix 0 -1 1 0            (90° rotation of the XY logical frame)
    get_matrix
    set_offset Z 0.25
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

DEFAULT_CMD_PORT = 5559  # keep in sync with protocol.py

# How to turn "verb arg arg" typed at the prompt into a JSON command dict.
_AXIS = {"x": "X", "y": "Y", "z": "Z", "0": "X", "1": "Y", "2": "Z"}


def build_request(line: str) -> dict:
    parts = line.split()
    verb = parts[0]
    a = parts[1:]

    def axis(v):  # pass through X/Y/Z or 0/1/2
        return _AXIS.get(v.lower(), v)

    if verb in ("status", "info", "get_config", "get_positions", "get_matrix"):
        return {"cmd": verb}
    if verb == "move_axis":
        return {"cmd": "move_axis", "axis": axis(a[0]), "position": float(a[1])}
    if verb == "move_logical":
        return {"cmd": "move_logical", "u": float(a[0]), "v": float(a[1]), "w": float(a[2])}
    if verb == "move_relative":
        return {"cmd": "move_relative", "axis": axis(a[0]), "value": float(a[1])}
    if verb == "set_zero":
        return {"cmd": "set_zero", "axis": axis(a[0]) if a else None}
    if verb == "clear_zero":
        return {"cmd": "clear_zero", "axis": axis(a[0]) if a else None}
    if verb == "home":
        return {"cmd": "home", "axis": axis(a[0]) if a else None}
    if verb == "stop":
        return {"cmd": "stop", "axis": axis(a[0]) if a else None}
    if verb == "set_velocity":
        return {"cmd": "set_velocity", "axis": axis(a[0]), "value": float(a[1])}
    if verb == "set_acceleration":
        return {"cmd": "set_acceleration", "axis": axis(a[0]), "value": float(a[1])}
    if verb == "set_offset":
        return {"cmd": "set_offset", "axis": axis(a[0]), "value": float(a[1])}
    if verb == "set_matrix":
        return {"cmd": "set_matrix", "matrix": [float(x) for x in a[:4]]}
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
    ap = argparse.ArgumentParser(description="raw console for the stage service")
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

    print(f"stage console -> tcp://{args.host}:{args.port}  (type 'quit' to exit)")
    while True:
        try:
            line = input("stage> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("quit", "exit"):
            break
        send(line)


if __name__ == "__main__":
    main()
