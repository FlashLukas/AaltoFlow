"""A tiny external console to talk to the Cornerstone 260 monochromator service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO `cs260`
imports -- so you can copy this one file to any machine (that has pyzmq) and
drive the monochromator with it. Meant for poking at the communication by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/mono_console.py                     # connects to localhost
    uv run scripts/mono_console.py --connect 192.168.1.42
        mono> wave 632.8
        mono> wait
        mono> grating 2
        mono> shutter close
        mono> status
        mono> watch 5
        mono> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/mono_console.py wave 500
    uv run scripts/mono_console.py status

Commands
    wave <nm>              go to a wavelength (reply = accepted, not arrived)
    wait [timeout_s]       block until the move has arrived (default 60 s)
    grating <n>            swap grating (1..3)
    shutter open|close     the built-in shutter ('close' = the safety verb
                           close_shutter: works also while a GUI has control)
    filter <n>             filter wheel position 1..6 (if fitted)
    port <1|2>             exit port: 1 axial, 2 lateral (if fitted)
    step <n>               nudge the drive by n motor steps
    abort                  stop motion, drop queued moves (safety: always allowed)
    status                 print one status snapshot
    info                   print static info (gratings, ranges, INFO?)
    watch [seconds]        stream the live status broadcast (default 5 s)
    help                   show this list
    quit / exit            leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                   take control if nobody has it
    take!                  take it over from whoever has it (they become a viewer)
    release                give it back
    clients                who holds control, who is connected
  While a GUI on another PC holds control, this console can read, abort and
  close the shutter but change nothing else until it takes control.
"""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import threading
import time
import uuid

import zmq

CMD_PORT = 5601
PUB_PORT = 5602
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "cs260 console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.ctx = zmq.Context.instance()
        self.req = None
        self._new_req()

    def _new_req(self):
        if self.req is not None:
            self.req.close(0)
        self.req = self.ctx.socket(zmq.REQ)
        self.req.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        self.req.setsockopt(zmq.LINGER, 0)
        self.req.connect(f"tcp://{self.host}:{self.cmd_port}")

    # ---- send one command, get one reply --------------------------------

    def send(self, msg: dict) -> dict:
        msg.setdefault("client", IDENTITY)       # say who we are (control)
        try:
            self.req.send_json(msg)
            return self.req.recv_json()
        except zmq.Again:
            # timed out -> REQ socket is stuck; rebuild it so the next call works
            self._new_req()
            return {"ok": False, "error": "no reply (is the service running?)"}

    # ---- pretty printers -------------------------------------------------

    @staticmethod
    def show_status(s: dict):
        mv = f"MOVING ({s.get('busy')})" if s.get("moving") else "idle"
        line = (f"  wl={s.get('wavelength_nm', float('nan')):9.3f} nm"
                f"  target={s.get('target_nm', float('nan')):9.3f}"
                f"  grating={s.get('grating')} ({s.get('grating_lines')} l/mm)"
                f"  shutter={'OPEN' if s.get('shutter_open') else 'closed'}"
                f"  {mv}")
        if s.get("filter_fitted"):
            line += f"  filter={s.get('filter')}"
        if s.get("port_fitted"):
            line += f"  port={s.get('port')}"
        if s.get("error_text"):
            line += f"  last error: {s.get('error_text')}"
        print(line)

    # ---- waiting for arrival ------------------------------------------------

    def wait(self, timeout_s: float):
        """The adopt-then-flag rule, by hand: the target must be adopted
        (moving was set together with it) and moving must be False."""
        t_end = time.monotonic() + timeout_s
        while time.monotonic() < t_end:
            r = self.send({"cmd": "status"})
            s = r.get("status", {}) if r.get("ok") else {}
            if s and not s.get("moving"):
                self.show_status(s)
                return
            time.sleep(0.1)
        print("  timed out waiting for the move")

    # ---- watch the live PUB stream --------------------------------------

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        poller = zmq.Poller(); poller.register(sub, zmq.POLLIN)
        print(f"  watching for {seconds:.0f} s (Ctrl-C to stop early) ...")
        t_end = time.monotonic() + seconds
        try:
            while time.monotonic() < t_end:
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
            if cmd in ("wave", "wl", "go"):
                print(self.send({"cmd": "set_wavelength", "wavelength_nm": float(args[0])}))
            elif cmd == "wait":
                self.wait(float(args[0]) if args else 60.0)
            elif cmd in ("grating", "grat"):
                print(self.send({"cmd": "set_grating", "grating": int(args[0])}))
            elif cmd == "shutter":
                open_ = args[0].lower() in ("open", "o", "on", "1")
                # close = the safety verb, allowed also while a GUI has control
                print(self.send({"cmd": "set_shutter", "open": True} if open_
                                else {"cmd": "close_shutter"}))
            elif cmd in ("take", "take!"):
                print(self.send({"cmd": "take_control", "force": cmd == "take!"}))
            elif cmd == "release":
                print(self.send({"cmd": "release_control"}))
            elif cmd == "clients":
                print("  " + json.dumps(self.send({"cmd": "clients"}), indent=2).replace("\n", "\n  "))
            elif cmd == "filter":
                print(self.send({"cmd": "set_filter", "filter": int(args[0])}))
            elif cmd == "port":
                print(self.send({"cmd": "set_port", "port": int(args[0])}))
            elif cmd == "step":
                print(self.send({"cmd": "step", "steps": int(args[0])}))
            elif cmd == "abort":
                print(self.send({"cmd": "abort"}))
            elif cmd == "status":
                r = self.send({"cmd": "status"})
                self.show_status(r.get("status", {})) if r.get("ok") else print(r)
            elif cmd == "info":
                r = self.send({"cmd": "info"})
                print("  " + json.dumps(r.get("info", r), indent=2).replace("\n", "\n  "))
            elif cmd == "watch":
                self.watch(float(args[0]) if args else 5.0)
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
    ap = argparse.ArgumentParser(description="External console for the Cornerstone 260 service")
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
        con.start_heartbeat()            # interactive: keep control while you think
        print(f"connected to tcp://{args.connect}:{args.cmd_port}   (type 'help' or 'quit')")
        while True:
            try:
                line = input("mono> ")
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
