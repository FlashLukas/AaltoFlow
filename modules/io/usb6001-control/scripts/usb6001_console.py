"""A tiny external console to talk to the USB-6001 DAQ service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO usb6001
imports -- so you can copy this one file to any machine that has pyzmq.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/usb6001_console.py                     # connects to localhost
    uv run scripts/usb6001_console.py --connect 192.168.1.42
        daq> ao 0 1.25
        daq> do p0.4 on
        daq> ai
        daq> di p0.0
        daq> status
        daq> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/usb6001_console.py ao 1 -0.5
    uv run scripts/usb6001_console.py ai ai2

Commands
    ao <0|1|name> <V>        set an analog output (clamped to its limits)
    do <line> on|off         drive a digital line configured as OUTPUT (e.g. p0.4)
    ai [channel]             a FRESH averaged reading (all enabled inputs, or one)
    di [line]                a FRESH reading of the input lines (all, or one)
    status                   print one status snapshot
    info                     print the layout and limits
    watch [seconds]          stream the live status broadcast (default 5 s)
    help                     show this list
    quit / exit              leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                     take control if nobody has it
    take!                    take it over from whoever has it (they become a viewer)
    release                  give it back
    clients                  who holds control, who is connected
  While a GUI on another PC holds control, this console can read ('ai', 'di',
  'status') but not set an output until it takes control (a general DAQ has no
  safety verb: no output value is safe for every setup).
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

CMD_PORT = 5629
PUB_PORT = 5630
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "usb6001 console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control

LINES = [f"p0.{i}" for i in range(8)] + [f"p1.{i}" for i in range(4)] + ["p2.0"]


def _f(v) -> str:
    return "   --   " if v is None or v != v else f"{v:+8.4f}"


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.ctx = zmq.Context.instance()
        self.req = self.ctx.socket(zmq.REQ)
        self.req.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        self.req.setsockopt(zmq.LINGER, 0)
        self.req.connect(f"tcp://{host}:{cmd_port}")

    # ---- send one command, get one reply --------------------------------

    def send(self, msg: dict) -> dict:
        msg.setdefault("client", IDENTITY)       # say who we are (control)
        try:
            self.req.send_json(msg)
            return self.req.recv_json()
        except zmq.Again:
            # timed out -> REQ socket is stuck; rebuild it so the next call works
            self.req.close(0)
            self.req = self.ctx.socket(zmq.REQ)
            self.req.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
            self.req.setsockopt(zmq.LINGER, 0)
            self.req.connect(f"tcp://{self.host}:{self.cmd_port}")
            return {"ok": False, "error": "no reply (is the service running?)"}

    # ---- pretty printers -------------------------------------------------

    @staticmethod
    def show_status(s: dict):
        ai = [_f(v) for v, on in zip(s.get("ai_V", []), s.get("ai_enabled", [])) if on]
        ao = [(_f(v) if k else "unknown") for v, k in zip(s.get("ao_V", []), s.get("ao_known", []))]
        dio = "".join({True: "1", False: "0", None: "?"}.get(v, "?") if d != "unused" else "-"
                      for v, d in zip(s.get("dio", []), s.get("dio_dir", [])))
        print(f"  AI[V] {' '.join(ai)}   AO {ao}   DIO {dio}"
              f"   connected={s.get('connected')}"
              + (f"   HW ERROR: {s.get('hw_error')}" if s.get("hw_error") else ""))

    # ---- watch the live PUB stream --------------------------------------

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        poller = zmq.Poller(); poller.register(sub, zmq.POLLIN)
        print(f"  watching for {seconds:.0f} s (Ctrl-C to stop early) ...")
        ticks = int(seconds / 0.2)
        try:
            for _ in range(max(1, ticks)):
                if poller.poll(200):
                    topic, payload = sub.recv_multipart()
                    d = json.loads(payload)
                    if topic == b"status":
                        self.show_status(d)
                    elif topic == b"event":
                        print(f"  event [{d.get('level')}] {d.get('msg')}")
        except KeyboardInterrupt:
            print("  (stopped)")
        finally:
            sub.close(0)

    # ---- turn a typed line into a command -------------------------------

    def run_line(self, line: str) -> bool:
        """Return False to quit, True to keep going."""
        parts = line.split()
        if not parts:
            return True
        cmd, args = parts[0].lower(), parts[1:]

        if cmd in ("quit", "exit", "q"):
            return False
        if cmd in ("help", "?"):
            print(__doc__.split("Commands")[1])
            return True

        try:
            if cmd == "ao":
                ch = int(args[0]) if args[0].isdigit() else args[0]
                print(self.send({"cmd": "set_ao", "channel": ch, "volts": float(args[1])}))
            elif cmd == "do":
                on = args[1].lower() in ("on", "1", "true", "high")
                print(self.send({"cmd": "set_do", "line": args[0], "state": on}))
            elif cmd == "ai":
                print(self.send({"cmd": "read_ai", "channel": args[0] if args else None}))
            elif cmd == "di":
                print(self.send({"cmd": "read_di", "line": args[0] if args else None}))
            elif cmd == "status":
                r = self.send({"cmd": "status"})
                self.show_status(r.get("status", {})) if r.get("ok") else print(r)
            elif cmd == "info":
                r = self.send({"cmd": "info"})
                print("  " + json.dumps(r.get("info", r), indent=2).replace("\n", "\n  "))
            elif cmd == "watch":
                self.watch(float(args[0]) if args else 5.0)
            elif cmd in ("take", "take!"):
                print(self.send({"cmd": "take_control", "force": cmd == "take!"}))
            elif cmd == "release":
                print(self.send({"cmd": "release_control"}))
            elif cmd == "clients":
                r = self.send({"cmd": "clients"})
                print("  " + json.dumps(r, indent=2).replace("\n", "\n  "))
            else:
                print(f"  unknown command: {cmd!r}  (try 'help')")
        except (IndexError, ValueError) as exc:
            print(f"  bad arguments for '{cmd}': {exc}  (try 'help')")
        return True

    def start_heartbeat(self):
        """"Still here" in the background, on its OWN socket (a ZeroMQ socket
        belongs to one thread): while you think, control stays yours."""
        self._hb_stop = threading.Event()

        def beat():
            def make():
                s = self.ctx.socket(zmq.REQ)
                s.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
                s.setsockopt(zmq.LINGER, 0)
                s.connect(f"tcp://{self.host}:{self.cmd_port}")
                return s
            hb = make()
            while not self._hb_stop.wait(HEARTBEAT_S):
                try:
                    hb.send_json({"cmd": "heartbeat", "client": IDENTITY})
                    hb.recv_json()
                except zmq.Again:                 # stuck REQ: rebuild it
                    hb.close(0)
                    hb = make()
            hb.close(0)
        threading.Thread(target=beat, daemon=True).start()

    def close(self):
        if getattr(self, "_hb_stop", None) is not None:
            self._hb_stop.set()
        self.req.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="External console for the USB-6001 DAQ service")
    ap.add_argument("--connect", default="localhost", help="service host (default: localhost)")
    ap.add_argument("--cmd-port", type=int, default=CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=PUB_PORT)
    ap.add_argument("words", nargs="*", help="a single command to run, then exit")
    args = ap.parse_args()

    con = Console(args.connect, args.cmd_port, args.pub_port)
    try:
        if args.words:                       # one-shot mode
            con.run_line(" ".join(args.words))
            return 0
        con.start_heartbeat()
        print(f"connected to tcp://{args.connect}:{args.cmd_port}   (type 'help' or 'quit')")
        while True:
            try:
                line = input("daq> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not con.run_line(line):
                break
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
