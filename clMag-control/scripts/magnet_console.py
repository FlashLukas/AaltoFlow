"""A tiny external console to talk to the magnet control service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO `clMag`
imports -- so you can copy this one file to any machine (that has pyzmq) and
drive the magnet with it. It is meant for poking at the communication by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/magnet_console.py                 # connects to localhost
    uv run scripts/magnet_console.py --connect 192.168.1.42
        magnet> field 50
        magnet> status
        magnet> watch 5
        magnet> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/magnet_console.py field 50
    uv run scripts/magnet_console.py status
    uv run scripts/magnet_console.py current 0

Commands
    field <mT> [nopid]     set field (add 'nopid' to skip the PID fine-tune)
    current <A>            set coil current directly
    demag <A>              demagnetise with the given amplitude
    calibrate [pts] [dwell] run a calibration sweep
    stab on|off            long-term stabilizer on/off
    lock on|off            external-control lock flag
    ao <ch> <V>            set an analog output (ch = 0..3 or full Dev1/ao0)
    ai <ch>                read one analog input (ch = 1..3 or full Dev1/ai1)
    do <line> on|off       set a digital output (line = 0..2 or full name)
    status                 print one status snapshot
    info                   print static info (field range, limits)
    watch [seconds]        stream the live status broadcast (default 5 s)
    help                   show this list
    quit / exit            leave
"""

from __future__ import annotations

import argparse
import json
import sys

import zmq

CMD_PORT = 5555
PUB_PORT = 5556
TIMEOUT_MS = 3000


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host = host
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
            self.req.connect(f"tcp://{self.host}:{CMD_PORT}")
            return {"ok": False, "error": "no reply (is the service running?)"}

    # ---- pretty printers -------------------------------------------------

    @staticmethod
    def show_status(s: dict):
        sp = s.get("setpoint_field_mT")
        sp_txt = "—" if sp is None else f"{sp:.3f} mT"
        print(f"  state={s.get('state','?'):9}  field={s.get('measured_field_mT',0):8.3f} mT"
              f"  current={s.get('current_A',0):7.3f} A  setpoint={sp_txt}"
              f"  stable={s.get('field_stable')}  locked={s.get('locked')}")

    # ---- watch the live PUB stream --------------------------------------

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        poller = zmq.Poller(); poller.register(sub, zmq.POLLIN)
        print(f"  watching for {seconds:.0f} s (Ctrl-C to stop early) …")
        # we can't use wall-clock timing portably here, so count poll ticks
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
            if cmd == "field":
                use_pid = not (len(args) > 1 and args[1].lower() == "nopid")
                print(self.send({"cmd": "set_field", "field_mT": float(args[0]), "use_pid": use_pid}))
            elif cmd == "current":
                print(self.send({"cmd": "set_current", "current_A": float(args[0])}))
            elif cmd == "demag":
                print(self.send({"cmd": "demag", "amplitude_A": float(args[0])}))
            elif cmd == "calibrate":
                m = {"cmd": "calibrate"}
                if len(args) > 0: m["n_per_leg"] = int(args[0])
                if len(args) > 1: m["dwell_s"] = float(args[1])
                print(self.send(m))
            elif cmd == "stab":
                print(self.send({"cmd": "set_stabilizer", "enabled": args[0].lower() in ("on", "1", "true")}))
            elif cmd == "lock":
                print(self.send({"cmd": "set_lock", "locked": args[0].lower() in ("on", "1", "true")}))
            elif cmd == "ao":
                ch = args[0] if "/" in args[0] else f"Dev1/ao{args[0]}"
                print(self.send({"cmd": "aux_set_ao", "channel": ch, "volts": float(args[1])}))
            elif cmd == "ai":
                ch = args[0] if "/" in args[0] else f"Dev1/ai{args[0]}"
                r = self.send({"cmd": "aux_read_ai", "channel": ch})
                print(f"  {ch} = {r['volts']:.4f} V" if r.get("ok") else r)
            elif cmd == "do":
                ln = args[0] if "/" in args[0] else f"Dev1/port0/line{args[0]}"
                print(self.send({"cmd": "aux_set_do", "line": ln,
                                 "state": args[1].lower() in ("on", "1", "true", "high")}))
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

    def close(self):
        self.req.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="External console for the magnet service")
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
        # interactive mode
        print(f"connected to tcp://{args.connect}:{args.cmd_port}   (type 'help' or 'quit')")
        while True:
            try:
                line = input("magnet> ")
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
