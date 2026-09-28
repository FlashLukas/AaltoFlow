"""Standalone raw-protocol console for the KIM101 stage service.

Speaks the wire protocol with ONLY pyzmq + json -- no `kim` package import --
so you can copy this single file to any machine to poke a running service.

    python scripts/kim_console.py                    # interactive REPL
    python scripts/kim_console.py --host 192.168.0.5 # remote service
    python scripts/kim_console.py status             # one-shot command

REPL examples (STEP language):
    status
    move_to_step X 5000            (absolute, in steps)
    move_steps X 200               (relative: step 200 from here)
    set_step_rate X 1500           (steps/s)
    set_acceleration X 20000       (steps/s^2)
    set_voltage X 115              (V -- sets the physical step size)

REPL examples (MICROMETRE language, via the calibration):
    move_to_um X 120               (absolute, in um)
    move_relative_um X 10          (relative: move 10 um from here)
    set_velocity_um X 50           (um/s)
    set_calibration X 0.021        (um per step)
    set_speed fast   | set_speed slow          (movement preset, all axes)
    set_step_size large | set_step_size small  (voltage preset, all axes)

Other:
    zero_counter          (datum all)   |   zero_counter X
    set_zero              (display 0)   |   set_zero X   |   clear_zero
    stop                  (all)         |   stop Y
    store_position 0 corner  |  goto_position 0  |  save_positions p.json
    quit
"""

from __future__ import annotations

import argparse
import json
import sys

import zmq

DEFAULT_CMD_PORT = 5567  # keep in sync with protocol.py

_AXIS = {"x": "X", "y": "Y", "z": "Z", "0": "X", "1": "Y", "2": "Z"}


def build_request(line: str) -> dict:
    parts = line.split()
    verb = parts[0]
    a = parts[1:]

    def axis(v):  # pass through X/Y/Z or 0/1/2
        return _AXIS.get(v.lower(), v)

    if verb in ("status", "info", "get_config", "get_positions"):
        return {"cmd": verb}
    if verb == "move_to_step":
        return {"cmd": "move_to_step", "axis": axis(a[0]), "position": int(a[1])}
    if verb == "move_steps":
        return {"cmd": "move_steps", "axis": axis(a[0]), "delta": int(a[1])}
    if verb == "move_to_um":
        return {"cmd": "move_to_um", "axis": axis(a[0]), "position": float(a[1])}
    if verb == "move_relative_um":
        return {"cmd": "move_relative_um", "axis": axis(a[0]), "delta": float(a[1])}
    if verb == "set_step_rate":
        return {"cmd": "set_step_rate", "axis": axis(a[0]), "value": float(a[1])}
    if verb == "set_velocity_um":
        return {"cmd": "set_velocity_um", "axis": axis(a[0]), "value": float(a[1])}
    if verb == "set_acceleration":
        return {"cmd": "set_acceleration", "axis": axis(a[0]), "value": float(a[1])}
    if verb == "set_voltage":
        return {"cmd": "set_voltage", "axis": axis(a[0]), "value": float(a[1])}
    if verb == "set_calibration":
        return {"cmd": "set_calibration", "axis": axis(a[0]), "value": float(a[1])}
    if verb == "set_speed":
        return {"cmd": "set_speed", "fast": a[0].lower() in ("fast", "1", "true", "on")}
    if verb == "set_step_size":
        return {"cmd": "set_step_size", "large": a[0].lower() in ("large", "big", "1", "true", "on")}
    if verb == "zero_counter":
        return {"cmd": "zero_counter", "axis": axis(a[0]) if a else None}
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
    ap = argparse.ArgumentParser(description="raw console for the KIM101 stage service")
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

    print(f"kim console -> tcp://{args.host}:{args.port}  (type 'quit' to exit)")
    while True:
        try:
            line = input("kim> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("quit", "exit"):
            break
        send(line)


if __name__ == "__main__":
    main()
