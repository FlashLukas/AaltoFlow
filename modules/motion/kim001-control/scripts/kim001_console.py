"""Standalone raw-protocol console for the KIM001 focus service.

Speaks the wire protocol with ONLY pyzmq + json -- no `kim001` package import --
so you can copy this single file to any machine to poke a running service.

    python scripts/kim001_console.py                    # interactive REPL
    python scripts/kim001_console.py --host 192.168.0.5 # remote service
    python scripts/kim001_console.py status             # one-shot command

The ONE axis is Z (the objective focus). X / Y exist only in the status lists
(kim's 3-wide wire shape) and every command for them is refused.

REPL examples (STEP language):
    status
    move_to_step Z 5000            (absolute, in steps)
    move_steps Z 200               (relative: step 200 from here)
    set_step_rate Z 1500           (steps/s)
    set_acceleration Z 20000       (steps/s^2)
    set_voltage Z 115              (V -- sets the physical step size)

REPL examples (MICROMETRE language, via the calibration):
    move_to_um Z 12                (absolute, in um)
    move_relative_um Z -2          (relative: move 2 um down from here)
    set_velocity_um Z 5            (um/s)
    set_calibration Z 0.021        (um per step)
    set_speed fast   | set_speed slow          (movement preset)
    set_step_size large | set_step_size small  (voltage preset)

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                  (take control if nobody has it)
    take!                 (take it over from whoever has it -- they become a viewer)
    release               (give it back)
    clients               (who holds control, who is connected)
  While a GUI holds control, this console can read and STOP but not change
  anything until it takes control.

Other:
    zero_counter          (datum)       |   zero_counter Z
    set_zero              (display 0)   |   set_zero Z   |   clear_zero
    stop                  (all)         |   stop Z
    store_position 0 focus  |  goto_position 0  |  save_positions p.json
    quit
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
    taken elsewhere has no secure.py and talks plain; a secured kim001 will not
    answer it."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "kim001" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("kim_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures kim001."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "kim001")

DEFAULT_CMD_PORT = 5637  # keep in sync with protocol.py

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "kim001 console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control

_AXIS = {"x": "X", "y": "Y", "z": "Z", "0": "X", "1": "Y", "2": "Z"}


def build_request(line: str) -> dict:
    parts = line.split()
    verb = parts[0]
    a = parts[1:]

    def axis(v):  # pass through X/Y/Z or 0/1/2
        return _AXIS.get(v.lower(), v)

    if verb in ("status", "info", "get_config", "get_positions", "clients"):
        return {"cmd": verb}
    if verb in ("take", "take!"):
        return {"cmd": "take_control", "force": verb == "take!"}
    if verb == "release":
        return {"cmd": "release_control"}
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
    ap = argparse.ArgumentParser(description="raw console for the KIM001 focus service")
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
        req.setdefault("client", IDENTITY)
        for attempt in (1, 2):
            try:
                sock[0].send_json(req)
                print(json.dumps(sock[0].recv_json(), indent=2))
                return
            except zmq.Again:
                sock[0].close(0)
                # kim001 may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches kim001)
                if attempt == 1 and _SECURE is not None and _SECURE.no_answer(args.host, "kim001"):
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
    print(f"kim001 console -> tcp://{args.host}:{args.port}  (type 'quit' to exit)")
    while True:
        try:
            line = input("kim001> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("quit", "exit"):
            break
        send(line)


if __name__ == "__main__":
    main()
