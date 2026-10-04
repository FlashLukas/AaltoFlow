"""A tiny external console to talk to the DynaCool (ppms) service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO `ppms`
imports -- so you can copy this one file to any machine (that has pyzmq) and
drive the cryostat with it. Meant for poking at the communication by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/ppms_console.py                     # connects to localhost
        ppms> field 500
        ppms> temp 10
        ppms> status
        ppms> wait field
        ppms> watch 5
        ppms> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/ppms_console.py field 0
    uv run scripts/ppms_console.py status

Commands
    field <mT>                 drive the magnet to <mT> (at the configured rate)
    frate <mT/s>               field ramp rate (applies from the next setpoint)
    fapproach linear|no_overshoot|oscillate
    temp <K>                   drive the temperature to <K>
    trate <K/min>              temperature rate (applies from the next setpoint)
    tapproach fast_settle|no_overshoot
    status                     print one status snapshot
    wait field|temp [s]        poll until the field / temperature is reached
    info                       print static info (limits, instrument)
    watch [seconds]            stream the live status broadcast (default 5 s)
    help                       show this list
    quit / exit                leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                       take control if nobody has it
    take!                      take it over from whoever has it (they become a viewer)
    release                    give it back
    clients                    who holds control, who is connected
  While a GUI on another PC holds control, this console can read but not
  change anything until it takes control (the PPMS has no safety verb: every
  command moves a setpoint).
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


def _load_secure():
    """The module's secure.py (encryption, README "Encryption and keys"),
    loaded straight from its file when this console sits in its module folder
    -- so the console still imports no package and runs anywhere. A copy
    taken elsewhere has no secure.py and talks plain; a secured ppms will not
    answer it."""
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "ppms" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("ppms_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures ppms."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "ppms")

CMD_PORT = 5579
PUB_PORT = 5580
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "ppms console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.ctx = zmq.Context.instance()
        self.req = self._new_req()

    def _new_req(self):
        s = self.ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        s.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)   # a refused handshake must not block send
        s.setsockopt(zmq.LINGER, 0)
        _secure(s, self.host)
        s.connect(f"tcp://{self.host}:{self.cmd_port}")
        return s

    # ---- send one command, get one reply --------------------------------

    def send(self, msg: dict) -> dict:
        msg.setdefault("client", IDENTITY)       # say who we are (control)
        for attempt in (1, 2):
            try:
                self.req.send_json(msg)
                return self.req.recv_json()
            except zmq.Again:
                # timed out -> REQ socket is stuck; rebuild it so the next call works
                self.req.close(0)
                # ppms may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches ppms)
                flipped = _SECURE is not None and _SECURE.no_answer(self.host, "ppms")
                self.req = self._new_req()
                if not (flipped and attempt == 1):
                    return {"ok": False, "error": "no reply (is the service running?)"}

    # ---- pretty printers -------------------------------------------------

    @staticmethod
    def _n(v, fmt):
        return "   --   " if v is None else format(v, fmt)

    def show_status(self, s: dict):
        n = self._n
        print(f"  B={n(s.get('measured_field_mT'), '10.2f')} mT"
              f" (set {n(s.get('setpoint_field_mT'), '.2f')},"
              f" {s.get('field_status', '')},"
              f" {'REACHED' if s.get('field_stable') else 'not reached'})"
              f"   T={n(s.get('temperature_K'), '8.3f')} K"
              f" (set {n(s.get('setpoint_temperature_K'), '.3f')},"
              f" {s.get('temperature_status', '')},"
              f" {'REACHED' if s.get('temperature_stable') else 'not reached'})"
              f"   chamber: {s.get('chamber', '')}"
              + (f"   HW ERROR: {s['hw_error']}" if s.get("hw_error") else ""))

    # ---- watch the live PUB stream --------------------------------------

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        _secure(sub, self.host)              # telemetry too
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        poller = zmq.Poller(); poller.register(sub, zmq.POLLIN)
        print(f"  watching for {seconds:.0f} s (Ctrl-C to stop early) ...")
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
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

    def wait(self, what: str, timeout_s: float):
        """The set -> wait-until-reached loop, the way scan-core does it: the
        status has to show the setpoint we asked for first (adopted), and only
        then is the 'reached' flag believed. A bare flag check would pass on the
        frame from the previous setpoint."""
        key = {"field": "field_stable", "temp": "temperature_stable"}[what]
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            r = self.send({"cmd": "status"})
            s = r.get("status", {})
            if s.get(key):
                print(f"  {what} reached after {time.monotonic() - t0:.1f} s")
                self.show_status(s)
                return
            time.sleep(0.5)
        print(f"  gave up after {timeout_s:g} s")

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
            if cmd == "field":
                print(self.send({"cmd": "set_field", "field_mT": float(args[0])}))
            elif cmd == "frate":
                print(self.send({"cmd": "set_field_rate", "rate_mT_per_s": float(args[0])}))
            elif cmd == "fapproach":
                print(self.send({"cmd": "set_field_approach", "approach": args[0]}))
            elif cmd == "temp":
                print(self.send({"cmd": "set_temperature", "temperature_K": float(args[0])}))
            elif cmd == "trate":
                print(self.send({"cmd": "set_temperature_rate",
                                 "rate_K_per_min": float(args[0])}))
            elif cmd == "tapproach":
                print(self.send({"cmd": "set_temperature_approach", "approach": args[0]}))
            elif cmd == "status":
                r = self.send({"cmd": "status"})
                self.show_status(r.get("status", {})) if r.get("ok") else print(r)
            elif cmd == "wait":
                self.wait(args[0], float(args[1]) if len(args) > 1 else 3600.0)
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
        except (IndexError, ValueError, KeyError) as exc:
            print(f"  bad arguments for '{cmd}': {exc}  (try 'help')")
        return True

    def start_heartbeat(self):
        """"Still here" in the background, on its OWN socket (a ZeroMQ socket
        belongs to one thread): while you think, control stays yours."""
        self._hb_stop = threading.Event()

        def beat():
            hb = self._new_req()
            while not self._hb_stop.wait(HEARTBEAT_S):
                try:
                    hb.send_json({"cmd": "heartbeat", "client": IDENTITY})
                    hb.recv_json()
                except zmq.Again:                 # stuck REQ: rebuild it
                    hb.close(0)
                    hb = self._new_req()
            hb.close(0)
        threading.Thread(target=beat, daemon=True).start()

    def close(self):
        if getattr(self, "_hb_stop", None) is not None:
            self._hb_stop.set()
        self.req.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="External console for the DynaCool (ppms) service")
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
                line = input("ppms> ")
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
