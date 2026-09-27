"""Standalone raw-protocol console for the Agilis stage service.

Speaks the wire protocol with ONLY pyzmq + json -- no `agilis` package import --
so you can copy this single file to any machine to poke a running service.

    python scripts/agilis_console.py                    # interactive REPL
    python scripts/agilis_console.py --host 192.168.0.5 # remote service
    python scripts/agilis_console.py status             # one-shot command

REPL examples (STEP language):
    status
    move_to_step X 5000            (absolute step counter)
    move_steps X 200               (relative: 200 steps from here)
    jog X 3 | jog X -1 | jog X 0   (continuous; ends by itself unless repeated)
    set_amplitude X 30             (both directions, 1..50)
    set_amplitude X 20 -1          (backward only)
    set_step_size large | set_step_size small

REPL examples (MICROMETRE language, via the measured step size):
    move_to_um X 120               (absolute, um estimate)
    move_relative_um X 10          (relative: 10 um from here)
    set_calibration X 0.048        (um per step, both directions)
    set_calibration X 0.041 -1     (backward only)

Other:
    zero_counter          (datum all)   |   zero_counter X
    set_zero              (display 0)   |   set_zero X   |   clear_zero
    stop                  (all)         |   stop Y
    leash on 20000 | leash off
    store_position 0 corner  |  goto_position 0  |  save_positions p.json
    {"cmd": "describe"}   (any raw JSON)
    quit
"""

from __future__ import annotations

import argparse
import json
import sys

import zmq

DEFAULT_CMD_PORT = 5595  # keep in sync with protocol.py / module.toml

_AXIS = {"x": "X", "y": "Y", "0": "X", "1": "Y"}


def build_request(line: str) -> dict:
    parts = line.split()
    verb = parts[0]
    a = parts[1:]

    def axis(v):
        return _AXIS.get(v.lower(), v)

    if verb in ("status", "info", "get_config", "get_positions", "describe",
                "stream_start", "stream_read", "stream_stop"):
        return {"cmd": verb}
    if verb == "move_to_step":
        return {"cmd": verb, "axis": axis(a[0]), "position": int(a[1])}
    if verb == "move_steps":
        return {"cmd": verb, "axis": axis(a[0]), "delta": int(a[1])}
    if verb == "move_to_um":
        return {"cmd": verb, "axis": axis(a[0]), "position": float(a[1])}
    if verb == "move_relative_um":
        return {"cmd": verb, "axis": axis(a[0]), "delta": float(a[1])}
    if verb == "jog":
        return {"cmd": verb, "axis": axis(a[0]), "speed": int(a[1])}
    if verb in ("set_amplitude", "set_calibration"):
        req = {"cmd": verb, "axis": axis(a[0]), "value": float(a[1])}
        if len(a) > 2:
            req["direction"] = int(a[2])
        return req
    if verb == "set_step_size":
        return {"cmd": verb, "large": a[0].lower() in ("large", "big", "1", "true", "on")}
    if verb == "leash":
        req = {"cmd": "set_leash", "enabled": a[0].lower() in ("on", "1", "true")}
        if len(a) > 1:
            req["leash_steps"] = int(a[1])
        return req
    if verb in ("zero_counter", "set_zero", "clear_zero", "stop"):
        return {"cmd": verb, "axis": axis(a[0]) if a else None}
    if verb == "store_position":
        return {"cmd": verb, "slot": int(a[0]), "name": " ".join(a[1:])}
    if verb in ("clear_position", "goto_position"):
        return {"cmd": verb, "slot": int(a[0])}
    if verb in ("save_positions", "load_positions"):
        return {"cmd": verb, "path": a[0]}
    # fall through: treat the whole line as raw JSON
    return json.loads(line)


def main() -> None:
    ap = argparse.ArgumentParser(description="raw console for the Agilis stage service")
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

    print(f"agilis console -> tcp://{args.host}:{args.port}  (type 'quit' to exit)")
    while True:
        try:
            line = input("agilis> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("quit", "exit"):
            break
        send(line)


if __name__ == "__main__":
    main()
