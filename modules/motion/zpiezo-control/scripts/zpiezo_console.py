"""Standalone raw-protocol console for the Z-piezo service (pyzmq + json only).

    python scripts/zpiezo_console.py                 # REPL
    python scripts/zpiezo_console.py status
    python scripts/zpiezo_console.py set_voltage 7.5

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                  (take control if nobody has it)
    take!                 (take it over from whoever has it -- they become a viewer)
    release               (give it back)
    clients               (who holds control, who is connected)
  While a GUI on another PC holds control, this console can read but not set
  the voltage until it takes control (the z piezo has no STOP: nothing moves
  after a set_voltage has been written).
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
    taken elsewhere has no secure.py and talks plain; a secured zpiezo will not
    answer it."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "zpiezo" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("zpiezo_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures zpiezo."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "zpiezo")

DEFAULT_CMD_PORT = 5565

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "zpiezo console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


def build_request(line: str) -> dict:
    parts = line.split()
    verb, a = parts[0], parts[1:]
    # every no-argument verb, including the universal `describe` and `shutdown`
    # (they used to fall through to json.loads and fail as "parse error")
    if verb in ("status", "info", "get_config", "read_voltage", "describe",
                "shutdown", "clients"):
        return {"cmd": verb}
    if verb in ("take", "take!"):
        return {"cmd": "take_control", "force": verb == "take!"}
    if verb == "release":
        return {"cmd": "release_control"}
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
                # zpiezo may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches zpiezo)
                if attempt == 1 and _SECURE is not None and _SECURE.no_answer(args.host, "zpiezo"):
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
