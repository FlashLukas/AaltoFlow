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
"""

from __future__ import annotations

import argparse
import json
import time

import zmq

CMD_PORT = 5579
PUB_PORT = 5580
TIMEOUT_MS = 3000


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
            else:
                print(f"  unknown command: {cmd!r}  (try 'help')")
        except (IndexError, ValueError, KeyError) as exc:
            print(f"  bad arguments for '{cmd}': {exc}  (try 'help')")
        return True

    def close(self):
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
