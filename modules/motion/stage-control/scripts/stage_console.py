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
    stop                 (all axes)   |   stop X
    quit

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                  (take control if nobody has it)
    take!                 (take it over from whoever has it -- they become a viewer)
    release               (give it back)
    clients               (who holds control, who is connected)
  While a GUI on another PC holds control, this console can read and STOP but
  not change anything until it takes control.
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


def _load_secure():
    """The module's secure.py (encryption, README "Encryption and keys"),
    loaded straight from its file when this console sits in its module folder
    -- so the console still imports no package and runs anywhere. A copy
    taken elsewhere has no secure.py and talks plain; a secured stage will not
    answer it."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "stage" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("stage_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures stage."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "stage")

DEFAULT_CMD_PORT = 5559  # keep in sync with protocol.py

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "stage console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control

# How to turn "verb arg arg" typed at the prompt into a JSON command dict.
_AXIS = {"x": "X", "y": "Y", "z": "Z", "0": "X", "1": "Y", "2": "Z"}


def build_request(line: str) -> dict:
    parts = line.split()
    verb = parts[0]
    a = parts[1:]

    def axis(v):  # pass through X/Y/Z or 0/1/2
        return _AXIS.get(v.lower(), v)

    if verb in ("status", "info", "get_config", "get_positions", "get_matrix", "clients"):
        return {"cmd": verb}
    if verb in ("take", "take!"):
        return {"cmd": "take_control", "force": verb == "take!"}
    if verb == "release":
        return {"cmd": "release_control"}
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

    def make_req():
        s = ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, 3000)
        s.setsockopt(zmq.SNDTIMEO, 3000)    # a refused handshake must not block send
        s.setsockopt(zmq.LINGER, 0)
        _secure(s, args.host)
        s.connect(f"tcp://{args.host}:{args.port}")
        return s

    sock = [make_req()]

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
        for attempt in (1, 2):
            try:
                sock[0].send_json(req)
                print(json.dumps(sock[0].recv_json(), indent=2))
                return
            except zmq.Again:
                sock[0].close(0)
                # stage may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches stage)
                if attempt == 1 and _SECURE is not None and _SECURE.no_answer(args.host, "stage"):
                    sock[0] = make_req()
                    continue
                print("! timeout (is the service running?)")
                sys.exit(1)

    if args.oneshot:
        send(" ".join(args.oneshot))
        return

    # "still here" in the background, on its OWN socket (a ZeroMQ socket
    # belongs to one thread): while you think, control stays yours
    def heartbeat() -> None:
        hb = make_req()
        while not stop.wait(HEARTBEAT_S):
            try:
                hb.send_json({"cmd": "heartbeat", "client": IDENTITY})
                hb.recv_json()
            except zmq.Again:                     # stuck REQ: rebuild it
                hb.close(0)
                hb = make_req()                   # in the mode send() found working
        hb.close(0)

    stop = threading.Event()
    threading.Thread(target=heartbeat, daemon=True).start()
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
