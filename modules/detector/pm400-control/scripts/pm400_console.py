"""A tiny external console for the PM400 service.

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `pm400` imports --
so this one file can be copied to any machine with pyzmq.

    uv run scripts/pm400_console.py                   # interactive, localhost
    uv run scripts/pm400_console.py --connect 192.168.1.42
    uv run scripts/pm400_console.py read              # one command, then exit

Commands
    read                   one live reading (power in W, or pulse energy in J)
    head                   which sensor head is plugged in, and its limits
    acquire                average fresh readings, print the latched sample
    wl <nm>                set the correction wavelength
    auto on|off            auto range on/off (power heads)
    range <value> [unit]   manual range; unit W|mW|uW|nW or J|mJ|uJ (default SI)
    avg <ms>               averaging time per reading (power heads)
    readings <n>           readings per acquisition
    settle <s>             ignore readings in the first <s> seconds of an acquisition
    zero                   zero adjustment -- COVER THE HEAD FIRST
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

CMD_PORT = 5617
PUB_PORT = 5618
TIMEOUT_MS = 3000

_UNITS = {"w": 1.0, "mw": 1e-3, "uw": 1e-6, "nw": 1e-9,
          "j": 1.0, "mj": 1e-3, "uj": 1e-6, "nj": 1e-9}


def fmt(v, unit="W") -> str:
    """0.00123 W -> '1.23 mW' (ASCII: 'u' for micro)."""
    if v is None:
        return "--"
    for scale, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n")):
        if abs(v) >= scale:
            return f"{v / scale:.5g} {prefix}{unit}"
    return f"{v / 1e-12:.5g} p{unit}"


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
        u = s.get("unit") or "W"
        rng = "auto " if s.get("auto_range") else "manual "
        print(f"  {s.get('quantity')}={fmt(s.get('value'), u):>12} {s.get('flag') or ''}"
              f"  head={s.get('head')}  wl={s.get('wavelength_nm')} nm"
              f"  range={rng}{fmt(s.get('range'), u)}"
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
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            s = self.send({"cmd": "status"}).get("status", {})
            # id first, then the flag: a stale "not acquiring" must not fool us
            if s.get("acq_id") == n and not s.get("acquiring"):
                smp = s.get("sample", {})
                u = smp.get("unit") or "W"
                print(f"  #{n}: {fmt(smp.get('value'), u)} +- {fmt(smp.get('std'), u)}"
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
            if cmd == "read":
                s = self.send({"cmd": "status"}).get("status", {})
                print(f"  {fmt(s.get('value'), s.get('unit') or 'W')} {s.get('flag') or ''}")
            elif cmd == "head":
                s = self.send({"cmd": "status"}).get("status", {})
                print(f"  {s.get('head')}: {s.get('sensor') or '-'}, measures {s.get('quantity')}"
                      f"  wavelength {s.get('wavelength_min_nm')}..{s.get('wavelength_max_nm')} nm"
                      f"  zero {'yes' if s.get('zero_supported') else 'no'}")
            elif cmd == "acquire":
                self.acquire()
            elif cmd == "wl":
                print(self.send({"cmd": "set_wavelength", "wavelength_nm": float(args[0])}))
            elif cmd == "auto":
                print(self.send({"cmd": "set_auto_range", "on": args[0].lower() in ("on", "1", "true")}))
            elif cmd == "range":
                scale = _UNITS[args[1].lower()] if len(args) > 1 else 1.0
                print(self.send({"cmd": "set_range", "range": float(args[0]) * scale}))
            elif cmd == "avg":
                print(self.send({"cmd": "set_avg_time", "avg_time_s": float(args[0]) * 1e-3}))
            elif cmd == "readings":
                print(self.send({"cmd": "set_acquisition", "readings": int(args[0])}))
            elif cmd == "settle":
                print(self.send({"cmd": "set_settle", "settle_s": float(args[0])}))
            elif cmd == "zero":
                if input("  head covered? [y/N] ").strip().lower() == "y":
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
    ap = argparse.ArgumentParser(description="External console for the PM400 service")
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
                line = input("pm400> ")
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
