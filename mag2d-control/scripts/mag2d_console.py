"""A tiny external console for the vector-magnet service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO `mag2d`
imports -- so you can copy this one file to any machine that has pyzmq and poke
the magnet by hand.

Start the service first:
    uv run scripts/run_service.py

Then interactively:
    uv run scripts/mag2d_console.py                    # localhost
    uv run scripts/mag2d_console.py --connect 192.168.1.42
        mag2d> field 150 45
        mag2d> watch 5
        mag2d> quit

or one command and exit (handy in scripts):
    uv run scripts/mag2d_console.py field 20
    uv run scripts/mag2d_console.py status

Commands
    field <mT> [deg]       set the field magnitude (and angle; omitted = keep it)
    angle <deg>            rotate, keeping the magnitude
    vector <bx> <by>       set Bx and By in mT
    bx <mT> / by <mT>      set one component, keep the other
    zero                   field 0 mT (angle kept)
    output on|off          energize / ramp down and switch off
    bypass on|off          water interlock bypass (DANGER)
    clear                  clear a fault (refused while the cause is present)
    status                 print one status snapshot
    info                   limits and settings
    describe               the parameter manifest (ids only)
    watch [seconds]        stream the live status broadcast (default 5 s)
    shutdown               ask the SERVICE to ramp down and exit
    help                   show this list
    quit / exit            leave the console (the service keeps running)
"""

from __future__ import annotations

import argparse
import json
import sys

import zmq

CMD_PORT = 5575
PUB_PORT = 5576
TIMEOUT_MS = 3000


def _on(word: str) -> bool:
    return word.lower() in ("on", "1", "true", "yes")


def _f(v, spec="8.2f"):
    return "      --" if v is None else format(v, spec)


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host, self.cmd_port, self.pub_port = host, cmd_port, pub_port
        self.ctx = zmq.Context.instance()
        self._make_req()

    def _make_req(self):
        self.req = self.ctx.socket(zmq.REQ)
        self.req.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        self.req.setsockopt(zmq.LINGER, 0)
        self.req.connect(f"tcp://{self.host}:{self.cmd_port}")

    def send(self, msg: dict) -> dict:
        try:
            self.req.send_json(msg)
            return self.req.recv_json()
        except zmq.Again:
            # timed out -> the REQ socket is stuck; rebuild it so the next call works
            self.req.close(0)
            self._make_req()
            return {"ok": False, "error": "no reply (is the service running?)"}

    @staticmethod
    def show_status(s: dict):
        print(f"  {s.get('state', '?'):10} energized={s.get('energized')}  "
              f"stable={s.get('field_stable')}  water_ok={s.get('water_ok')}"
              f"{'  BYPASS' if s.get('water_bypass') else ''}")
        print(f"  setpoint  |B| {_f(s.get('setpoint_field_mT'))} mT  angle "
              f"{_f(s.get('setpoint_angle_deg'), '7.2f')} deg   Bx {_f(s.get('setpoint_bx_mT'))}"
              f"  By {_f(s.get('setpoint_by_mT'))}")
        print(f"  measured  |B| {_f(s.get('measured_magnitude_mT'))} mT  angle "
              f"{_f(s.get('measured_angle_deg'), '7.2f')} deg   Bx {_f(s.get('measured_bx_mT'))}"
              f"  By {_f(s.get('measured_by_mT'))}  error {_f(s.get('error_mT'), '.3f')}")
        out = s.get("output_V") or [None, None]
        temps = s.get("temp_C") or [None, None]
        print(f"  drive X {_f(out[0], '+.3f')} V  Y {_f(out[1], '+.3f')} V   "
              f"T1 {_f(temps[0], '.1f')} C  T2 {_f(temps[1], '.1f')} C")
        if s.get("fault"):
            print(f"  FAULT: {s['fault']}")

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        poller = zmq.Poller(); poller.register(sub, zmq.POLLIN)
        print(f"  watching for {seconds:.0f} s (Ctrl-C to stop early) ...")
        try:
            for _ in range(max(1, int(seconds / 0.2))):
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
                m = {"cmd": "set_field", "field_mT": float(args[0])}
                if len(args) > 1:
                    m["angle_deg"] = float(args[1])
                print(self.send(m))
            elif cmd == "angle":
                print(self.send({"cmd": "set_angle", "angle_deg": float(args[0])}))
            elif cmd == "vector":
                print(self.send({"cmd": "set_vector", "bx_mT": float(args[0]), "by_mT": float(args[1])}))
            elif cmd == "bx":
                print(self.send({"cmd": "set_bx", "bx_mT": float(args[0])}))
            elif cmd == "by":
                print(self.send({"cmd": "set_by", "by_mT": float(args[0])}))
            elif cmd == "zero":
                print(self.send({"cmd": "zero"}))
            elif cmd == "output":
                print(self.send({"cmd": "set_output", "enabled": _on(args[0])}))
            elif cmd == "bypass":
                print(self.send({"cmd": "set_water_bypass", "enabled": _on(args[0])}))
            elif cmd == "clear":
                print(self.send({"cmd": "clear_fault"}))
            elif cmd == "status":
                r = self.send({"cmd": "status"})
                self.show_status(r["status"]) if r.get("ok") else print(r)
            elif cmd == "info":
                r = self.send({"cmd": "info"})
                print("  " + json.dumps(r.get("info", r), indent=2).replace("\n", "\n  "))
            elif cmd == "describe":
                r = self.send({"cmd": "describe"})
                for p in r.get("describe", {}).get("parameters", []):
                    print(f"  {p['kind']:9} {p['id']:20} {p.get('unit', '')}")
            elif cmd == "watch":
                self.watch(float(args[0]) if args else 5.0)
            elif cmd == "shutdown":
                print(self.send({"cmd": "shutdown"}))
            else:
                print(f"  unknown command: {cmd!r}  (try 'help')")
        except (IndexError, ValueError) as exc:
            print(f"  bad arguments for '{cmd}': {exc}  (try 'help')")
        return True

    def close(self):
        self.req.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="External console for the mag2d service")
    ap.add_argument("--connect", default="localhost", help="service host (default: localhost)")
    ap.add_argument("--cmd-port", type=int, default=CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=PUB_PORT)
    ap.add_argument("words", nargs="*", help="a single command to run, then exit")
    args = ap.parse_args()

    con = Console(args.connect, args.cmd_port, args.pub_port)
    try:
        if args.words:
            con.run_line(" ".join(args.words))
            return 0
        print(f"connected to tcp://{args.connect}:{args.cmd_port}   (type 'help' or 'quit')")
        while True:
            try:
                line = input("mag2d> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not con.run_line(line):
                break
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
