"""Standalone raw-protocol console for the SmarAct positioner service.

Speaks the wire protocol with ONLY pyzmq + json -- no `smaract` package import
-- so this single file can be copied to any machine to poke a running service.

    python scripts/smaract_console.py                     # interactive REPL
    python scripts/smaract_console.py --host 192.168.0.5  # remote service
    python scripts/smaract_console.py status              # one-shot command

REPL examples:
    status
    find_reference          (drives a few mm over the reference marks)
    move_to 12.5            (absolute mm; needs a referenced axis)
    move_by -0.05           (relative step, mm)
    set_zero | clear_zero
    move_from_zero 1.0      (1 mm from the zero)
    set_velocity 1.5        (mm/s)
    set_hold_time 500       (ms; 0 = let go at the target)
    stop
    store_position 0 sample A
    goto_position 0
    save_positions positions.json
    {"cmd": "describe"}     (anything else is sent as raw JSON)
    quit

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                  (take control if nobody has it)
    take!                 (take it over from whoever has it -- they become a viewer)
    release               (give it back)
    clients               (who holds control, who is connected)
  While a GUI on another PC holds control, this console can read and STOP
  but not change anything until it takes control.
"""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import sys
import threading
import uuid

import zmq

DEFAULT_CMD_PORT = 5597  # keep in sync with module.toml

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "smaract console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


def build_request(line: str) -> dict:
    parts = line.split()
    verb, a = parts[0], parts[1:]
    if verb in ("status", "info", "get_config", "describe", "get_positions",
                "find_reference", "stop", "set_zero", "clear_zero",
                "stream_start", "stream_read", "stream_stop", "shutdown", "clients"):
        return {"cmd": verb}
    if verb in ("take", "take!"):
        return {"cmd": "take_control", "force": verb == "take!"}
    if verb == "release":
        return {"cmd": "release_control"}
    if verb == "move_to":
        return {"cmd": "move_to", "position": float(a[0])}
    if verb == "move_by":
        return {"cmd": "move_by", "delta": float(a[0])}
    if verb == "move_from_zero":
        return {"cmd": "move_from_zero", "value": float(a[0])}
    if verb == "set_velocity":
        return {"cmd": "set_velocity", "value": float(a[0])}
    if verb == "set_hold_time":
        return {"cmd": "set_hold_time", "value": int(a[0])}
    if verb == "store_position":
        return {"cmd": "store_position", "slot": int(a[0]), "name": " ".join(a[1:])}
    if verb in ("clear_position", "goto_position"):
        return {"cmd": verb, "slot": int(a[0])}
    if verb in ("save_positions", "load_positions"):
        return {"cmd": verb, "path": a[0]}
    # fall through: treat the whole line as raw JSON
    return json.loads(line)


def main() -> None:
    ap = argparse.ArgumentParser(description="raw console for the SmarAct positioner service")
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
        req.setdefault("client", IDENTITY)       # say who we are (control)
        sock.send_json(req)
        try:
            # ensure_ascii keeps the printed text ASCII (gotcha #14)
            print(json.dumps(sock.recv_json(), indent=2, ensure_ascii=True))
        except zmq.Again:
            print("! timeout (is the service running?)")
            sock.close(0)
            sys.exit(1)

    if args.oneshot:
        send(" ".join(args.oneshot))
        return

    # "still here" in the background, on its OWN socket (a ZeroMQ socket
    # belongs to one thread): while you think, control stays yours
    def heartbeat() -> None:
        def make():
            hb = ctx.socket(zmq.REQ)
            hb.setsockopt(zmq.RCVTIMEO, 3000)
            hb.setsockopt(zmq.LINGER, 0)
            hb.connect(f"tcp://{args.host}:{args.port}")
            return hb
        hb = make()
        while not stop.wait(HEARTBEAT_S):
            try:
                hb.send_json({"cmd": "heartbeat", "client": IDENTITY})
                hb.recv_json()
            except zmq.Again:                     # stuck REQ: rebuild it
                hb.close(0)
                hb = make()
        hb.close(0)

    stop = threading.Event()
    threading.Thread(target=heartbeat, daemon=True).start()
    print(f"smaract console -> tcp://{args.host}:{args.port}  (type 'quit' to exit)")
    while True:
        try:
            line = input("smaract> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("quit", "exit"):
            break
        send(line)


if __name__ == "__main__":
    main()
