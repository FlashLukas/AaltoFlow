"""A tiny external console for the gaussmeter service.

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `ls455` imports --
so this one file can be copied to any machine with pyzmq.

    uv run scripts/ls455_console.py                   # interactive, localhost
    uv run scripts/ls455_console.py --connect 192.168.1.42
    uv run scripts/ls455_console.py field              # one command, then exit

Commands (every field in mT)
    field                  one live reading
    acquire                average fresh, settled readings; print the latched sample
    mode dc|rms|peak       measurement mode (peak keeps the front panel's peak settings)
    digits 3|4|5           DC resolution (= the meter's filter)
    band wide|narrow       RMS band
    auto on|off            auto range on/off
    range <mT>             manual full-scale range (switches auto off)
    unit G|T|Oe|A/m        what the meter's own display shows
    rel on|off [mT]        relative mode, optionally with a setpoint
    relhere                relative to the field measured now
    readings <n>           readings per acquisition
    zero                   zero the probe -- ZERO-GAUSS CHAMBER FIRST
    clearzero              forget the stored probe zero
    probe                  re-read the probe from the meter (after swapping it)
    status                 print one status snapshot
    info                   static info (probe, ranges)
    watch [seconds]        stream the live status broadcast (default 5 s)
    help                   show this list
    quit / exit            leave
"""

from __future__ import annotations

import argparse
import json
import time

import zmq

CMD_PORT = 5615
PUB_PORT = 5616
TIMEOUT_MS = 3000


def fmt_mt(v) -> str:
    """ASCII only (this goes to stdout, gotcha #14)."""
    if v is None:
        return "--"
    a = abs(v)
    if a >= 1000:
        return f"{v / 1000:.6g} T"
    if a >= 1 or a == 0:
        return f"{v:.6g} mT"
    return f"{v * 1000:.6g} uT"


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
        mode = s.get("mode", "dc")
        detail = (f"{s.get('dc_digits')} digits" if mode == "dc" else
                  f"{s.get('rms_band')} band" if mode == "rms" else
                  f"{s.get('peak_mode')} {s.get('peak_display')}")
        print(f"  B={fmt_mt(s.get('field_mT')):>14} {s.get('flag') or ''}"
              f"  {mode.upper()} {detail}"
              f"  range={'auto ' if s.get('auto_range') else 'manual '}{fmt_mt(s.get('range_mT'))}"
              f"{'  rel ' + fmt_mt(s.get('field_rel_mT')) if s.get('relative') else ''}"
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
                print(f"  #{n}: {fmt_mt(smp.get('field_mT'))} +- {fmt_mt(smp.get('std_mT'))}"
                      f"  (n={smp.get('n')}) {smp.get('flag') or ''}")
                return
            time.sleep(0.05)
        print(f"  acquisition {n} timed out")

    def run_line(self, line: str) -> bool:
        parts = line.split()
        if not parts:
            return True
        cmd, args = parts[0].lower(), parts[1:]
        on = lambda a: a.lower() in ("on", "1", "true", "yes")
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd in ("help", "?"):
            print(__doc__.split("Commands")[1])
            return True
        try:
            if cmd == "field":
                s = self.send({"cmd": "status"}).get("status", {})
                print(f"  {fmt_mt(s.get('field_mT'))} {s.get('flag') or ''}")
            elif cmd == "acquire":
                self.acquire()
            elif cmd == "mode":
                print(self.send({"cmd": "set_mode", "mode": args[0].lower()}))
            elif cmd == "digits":
                print(self.send({"cmd": "set_dc_digits", "digits": int(args[0])}))
            elif cmd == "band":
                print(self.send({"cmd": "set_rms_band", "band": args[0].lower()}))
            elif cmd == "auto":
                print(self.send({"cmd": "set_auto_range", "on": on(args[0])}))
            elif cmd == "range":
                print(self.send({"cmd": "set_range", "range_mT": float(args[0])}))
            elif cmd == "unit":
                print(self.send({"cmd": "set_display_unit", "unit": args[0]}))
            elif cmd == "rel":
                msg = {"cmd": "set_relative", "on": on(args[0])}
                if len(args) > 1:
                    msg["setpoint_mT"] = float(args[1])
                print(self.send(msg))
            elif cmd == "relhere":
                print(self.send({"cmd": "relative_here"}))
            elif cmd == "readings":
                print(self.send({"cmd": "set_acquisition", "readings": int(args[0])}))
            elif cmd == "zero":
                if input("  probe in the zero-gauss chamber? [y/N] ").strip().lower() == "y":
                    print(self.send({"cmd": "zero"}))
            elif cmd == "clearzero":
                print(self.send({"cmd": "clear_zero"}))
            elif cmd == "probe":
                r = self.send({"cmd": "reread_probe"})
                if not r.get("ok"):
                    print(" ", r)
                else:
                    s = self.send({"cmd": "status"}).get("status", {})
                    print(f"  probe: {s.get('probe_desc')}  ranges (mT): {s.get('ranges_mT')}")
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
    ap = argparse.ArgumentParser(description="External console for the gaussmeter service")
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
                line = input("ls455> ")
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
