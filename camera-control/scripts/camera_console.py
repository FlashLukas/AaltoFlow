"""Standalone raw-protocol console for the camera service.

Speaks the wire protocol with ONLY pyzmq + json -- no `camera` package import --
so you can copy this single file to any machine to poke a running service.

    python scripts/camera_console.py                 # interactive REPL
    python scripts/camera_console.py --host 10.0.0.5  # remote
    python scripts/camera_console.py status           # one-shot

REPL examples:
    status
    info
    autofocus
    set_tracking 1
    set_stabilize 1
    set_selected_index 2 2
    move_xy 60 60
    read_xy
    set_z 7.5
    read_position_px
    set_objective 50x - Zeiss NA 0.8
    snapshot
    load_pattern pattern.png
    save_pattern pattern.png
    quit
"""

from __future__ import annotations

import argparse
import json
import sys

import zmq

DEFAULT_CMD_PORT = 5563  # keep in sync with protocol.py


def build_request(line: str) -> dict:
    parts = line.split()
    verb = parts[0]
    a = parts[1:]

    def truthy(v):
        return v.lower() in ("1", "true", "yes", "on")

    simple = ("status", "info", "get_config", "autofocus", "kill_af",
              "read_xy", "read_z", "read_position_px", "list_objectives", "get_frame")
    if verb in simple:
        return {"cmd": verb}
    if verb in ("set_tracking", "set_stabilize", "set_continuous_focus"):
        return {"cmd": verb, "on": truthy(a[0])}
    if verb == "set_selected_index":
        return {"cmd": "set_selected_index", "ix": int(a[0]), "iy": int(a[1])}
    if verb == "move_xy":
        return {"cmd": "move_xy", "x": float(a[0]), "y": float(a[1])}
    if verb == "set_z":
        return {"cmd": "set_z", "volts": float(a[0])}
    if verb == "set_position_px":
        return {"cmd": "set_position_px", "x": float(a[0]), "y": float(a[1])}
    if verb == "click_to_go":
        return {"cmd": "click_to_go", "px": float(a[0]), "py": float(a[1])}
    if verb == "snapshot":
        return {"cmd": "snapshot", "path": a[0] if a else None}
    if verb == "load_pattern":
        return {"cmd": "load_pattern", "path": a[0]}
    if verb == "save_pattern":
        return {"cmd": "save_pattern", "path": a[0]}
    if verb == "set_objective":
        return {"cmd": "set_objective", "name": " ".join(a)}
    if verb == "capture_reference":       # cx cy w h
        return {"cmd": "capture_reference", "roi": [float(x) for x in a[:4]]}
    return json.loads(line)   # fall through: raw JSON


def main() -> None:
    ap = argparse.ArgumentParser(description="raw console for the camera service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("oneshot", nargs="*")
    args = ap.parse_args()

    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 5000)
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
            reply = sock.recv_json()
        except zmq.Again:
            print("! timeout (is the service running?)")
            sock.close(0)
            sys.exit(1)
        # Frames are huge base64 blobs -- summarise instead of dumping.
        if isinstance(reply, dict) and "png_b64" in reply:
            reply["png_b64"] = f"<{len(reply['png_b64'])} b64 chars>"
        print(json.dumps(reply, indent=2))

    if args.oneshot:
        send(" ".join(args.oneshot))
        return

    print(f"camera console -> tcp://{args.host}:{args.port}  (type 'quit' to exit)")
    while True:
        try:
            line = input("camera> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("quit", "exit"):
            break
        send(line)


if __name__ == "__main__":
    main()
