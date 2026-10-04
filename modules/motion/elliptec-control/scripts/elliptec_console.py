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

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                      (take control if nobody has it)
    take!                     (take it over from whoever has it -- they become a viewer)
    release                   (give it back)
    clients                   (who holds control, who is connected)
  While a GUI on another PC holds control, this console can read and
  stop but not change anything until it takes control.
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
    taken elsewhere has no secure.py and talks plain; a secured elliptec will not
    answer it."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "elliptec" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("elliptec_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures elliptec."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "elliptec")

DEFAULT_CMD_PORT = 5607  # keep in sync with module.toml

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "elliptec console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


def build_request(line: str) -> dict:
    parts = line.split()
    verb, a = parts[0], parts[1:]
    if verb == "clients":
        return {"cmd": "clients"}
    if verb in ("take", "take!"):
        return {"cmd": "take_control", "force": verb == "take!"}
    if verb == "release":
        return {"cmd": "release_control"}

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
                # elliptec may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches elliptec)
                if attempt == 1 and _SECURE is not None and _SECURE.no_answer(args.host, "elliptec"):
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
