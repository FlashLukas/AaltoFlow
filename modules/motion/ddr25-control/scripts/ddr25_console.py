"""Standalone raw-protocol console for the ddr25 rotation-stage service.

Speaks the wire protocol with ONLY pyzmq + json -- no `ddr25` package import --
so you can copy this single file to any machine to poke a running service.

    python scripts/ddr25_console.py                    # interactive REPL
    python scripts/ddr25_console.py --host 192.168.0.5 # remote service
    python scripts/ddr25_console.py status             # one-shot command

REPL examples:
    status
    home                      (needed once before absolute moves)
    move_to 45                (absolute angle, by the wrap policy)
    move_by -2.5              (relative)
    stop            |  stop now     (immediate halt)
    set_velocity 90
    set_acceleration 300
    set_wrap shortest         (literal | shortest | positive | negative)
    set_zero        |  clear_zero
    store_angle 0 polariser-s
    goto_angle 0
    get_angles
    save_angles angles.json  |  load_angles angles.json
    describe
    quit
"""

from __future__ import annotations

import argparse
import json
import sys

import zmq

DEFAULT_CMD_PORT = 5605  # keep in sync with module.toml / protocol.py

_NO_ARGS = ("status", "info", "get_config", "describe", "get_angles", "home",
            "set_zero", "clear_zero", "stream_start", "stream_read",
            "stream_stop", "shutdown")


def build_request(line: str) -> dict:
    parts = line.split()
    verb, a = parts[0], parts[1:]
    if verb in _NO_ARGS:
        return {"cmd": verb}
    if verb == "move_to":
        return {"cmd": "move_to", "angle": float(a[0])}
    if verb == "move_by":
        return {"cmd": "move_by", "delta": float(a[0])}
    if verb == "stop":
        now = bool(a) and a[0].lower() in ("now", "immediate", "1")
        return {"cmd": "stop", "immediate": now}
    if verb in ("set_velocity", "set_acceleration"):
        return {"cmd": verb, "value": float(a[0])}
    if verb == "set_wrap":
        return {"cmd": "set_wrap", "wrap": a[0]}
    if verb == "store_angle":
        return {"cmd": "store_angle", "slot": int(a[0]), "name": " ".join(a[1:])}
    if verb in ("clear_angle", "goto_angle"):
        return {"cmd": verb, "slot": int(a[0])}
    if verb in ("save_angles", "load_angles"):
        return {"cmd": verb, "path": a[0]}
    # fall through: treat the whole line as raw JSON
    return json.loads(line)


def main() -> None:
    ap = argparse.ArgumentParser(description="raw console for the ddr25 rotation-stage service")
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

    print(f"ddr25 console -> tcp://{args.host}:{args.port}  (type 'quit' to exit)")
    while True:
        try:
            line = input("ddr25> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("quit", "exit"):
            break
        send(line)


if __name__ == "__main__":
    main()
