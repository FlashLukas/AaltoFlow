"""A tiny external console for the power meter service.

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `pm16` imports --
so this one file can be copied to any machine with pyzmq.

    uv run scripts/pm16_console.py                   # interactive, localhost
    uv run scripts/pm16_console.py --connect 192.168.1.42
    uv run scripts/pm16_console.py power              # one command, then exit

Commands
    power                  one live reading
    acquire                average fresh readings, print the latched sample
    wl <nm>                set the wavelength
    auto on|off            auto range on/off
    range <value> [W|mW|uW] manual range (switches auto off)
    readings <n>           readings per acquisition
    zero                   dark adjustment -- COVER THE SENSOR FIRST
    status                 print one status snapshot
    info                   static info (limits, identity)
    watch [seconds]        stream the live status broadcast (default 5 s)
    help                   show this list
    quit / exit            leave
"""

from __future__ import annotations

import argparse
import json
import time

import zmq

CMD_PORT = 5571
PUB_PORT = 5572
TIMEOUT_MS = 3000

_UNITS = {"w": 1.0, "mw": 1e-3, "uw": 1e-6, "nw": 1e-9}


def fmt_w(w) -> str:
    if w is None:
        return "--"
    for scale, unit in ((1.0, "W"), (1e-3, "mW"), (1e-6, "uW"), (1e-9, "nW")):
        if abs(w) >= scale:
            return f"{w / scale:.5g} {unit}"
    return f"{w / 1e-12:.5g} pW"


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host, self.cmd_port, self.pub_port = host, cmd_port, pub_port
        self.ctx = zmq.Context.instance()
        self.req = self._new_req()

    def _new_req(self):
        s = self.ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(f"tcp://{self.host}:{self.cmd_port}")
        return s

    def send(self, msg: dict) -> dict:
        try:
            self.req.send_json(msg)
            return self.req.recv_json()
        except zmq.Again:
            self.req.close(0)                  # a timed-out REQ socket is stuck: rebuild
            self.req = self._new_req()
            return {"ok": False, "error": "no reply (is the service running?)"}

    @staticmethod
    def show_status(s: dict):
        print(f"  power={fmt_w(s.get('power_W')):>12} {s.get('flag') or ''}"
              f"  wl={s.get('wavelength_nm')} nm"
              f"  range={'auto ' if s.get('auto_range') else 'manual '}{fmt_w(s.get('range_W'))}"
              f"  acq#{s.get('acq_id')}{' busy' if s.get('acquiring') else ''}"
              f"{'  ZEROING' if s.get('zeroing') else ''}"
              f"{'  ERROR ' + s['hw_error'] if s.get('hw_error') else ''}")

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        poller = zmq.Poller(); poller.register(sub, zmq.POLLIN)
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                if poller.poll(200):
                    topic, payload = sub.recv_multipart()
                    d = json.loads(payload)
                    if topic == b"status":
                        self.show_status(d)
                    else:
                        print(f"  event [{d.get('level')}] {d.get('msg')}")
        except KeyboardInterrupt:
            print("  (stopped)")
        finally:
            sub.close(0)

    def acquire(self):
        r = self.send({"cmd": "acquire"})
        if not r.get("ok"):
            print(" ", r)
            return
        n = r["acq_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            s = self.send({"cmd": "status"}).get("status", {})
            # id first, then the flag: a stale "not acquiring" must not fool us
            if s.get("acq_id") == n and not s.get("acquiring"):
                smp = s.get("sample", {})
                print(f"  #{n}: {fmt_w(smp.get('power_W'))} +- {fmt_w(smp.get('std_W'))}"
                      f"  (n={smp.get('n')}) {smp.get('flag') or ''}")
                return
            time.sleep(0.05)
        print(f"  acquisition {n} timed out")

    def run_line(self, line: str) -> bool:
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
            if cmd == "power":
                s = self.send({"cmd": "status"}).get("status", {})
                print(f"  {fmt_w(s.get('power_W'))} {s.get('flag') or ''}")
            elif cmd == "acquire":
                self.acquire()
            elif cmd == "wl":
                print(self.send({"cmd": "set_wavelength", "wavelength_nm": float(args[0])}))
            elif cmd == "auto":
                print(self.send({"cmd": "set_auto_range", "on": args[0].lower() in ("on", "1", "true")}))
            elif cmd == "range":
                scale = _UNITS[args[1].lower()] if len(args) > 1 else 1.0
                print(self.send({"cmd": "set_range", "range_W": float(args[0]) * scale}))
            elif cmd == "readings":
                print(self.send({"cmd": "set_acquisition", "readings": int(args[0])}))
            elif cmd == "zero":
                if input("  sensor covered? [y/N] ").strip().lower() == "y":
                    print(self.send({"cmd": "zero"}))
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
        except (IndexError, ValueError, KeyError) as exc:
            print(f"  bad arguments for '{cmd}': {exc}  (try 'help')")
        return True

    def close(self):
        self.req.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="External console for the power meter service")
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
                line = input("pm16> ")
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
