"""A tiny external console to talk to the SMB100A control service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO `smb`
imports -- so you can copy this one file to any machine (that has pyzmq) and
drive the generator with it. Meant for poking at the communication by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/rf_console.py                     # connects to localhost
    uv run scripts/rf_console.py --connect 192.168.1.42
        rf> freq 1.5 GHz
        rf> power -10
        rf> rf on
        rf> status
        rf> watch 5
        rf> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/rf_console.py freq 1e9
    uv run scripts/rf_console.py rf on
    uv run scripts/rf_console.py status

Commands
    rf on|off              turn the RF output on or off
    power <dBm>            set the output level in dBm
    freq <value> [unit]    set frequency; unit = Hz|kHz|MHz|GHz (default Hz)
    phase <deg>            set the phase in degrees
    status                 print one status snapshot
    info                   print static info (limits, *IDN?)
    watch [seconds]        stream the live status broadcast (default 5 s)
    help                   show this list
    quit / exit            leave
"""

from __future__ import annotations

import argparse
import json
import sys

import zmq

CMD_PORT = 5557
PUB_PORT = 5558
TIMEOUT_MS = 3000

_UNITS = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}


def parse_freq(tokens) -> float:
    """'1.5 GHz' / '1e9' / '100 MHz' -> Hz as a float."""
    if not tokens:
        raise ValueError("frequency needs a value")
    value = float(tokens[0])
    if len(tokens) > 1:
        unit = tokens[1].lower()
        if unit not in _UNITS:
            raise ValueError(f"unknown unit {tokens[1]!r} (use Hz/kHz/MHz/GHz)")
        value *= _UNITS[unit]
    return value


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
    def show_status(s: dict):
        f_hz = s.get("frequency_Hz", 0.0)
        print(f"  RF={'ON ' if s.get('rf_on') else 'off'}"
              f"  freq={f_hz/1e6:12.6f} MHz"
              f"  power={s.get('power_dBm', 0):8.2f} dBm"
              f"  phase={s.get('phase_deg', 0):8.2f} deg"
              f"  connected={s.get('connected')}")

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
            if cmd == "rf":
                on = args[0].lower() in ("on", "1", "true")
                print(self.send({"cmd": "set_rf", "on": on}))
            elif cmd == "power":
                print(self.send({"cmd": "set_power", "power_dBm": float(args[0])}))
            elif cmd == "freq":
                print(self.send({"cmd": "set_frequency", "frequency_Hz": parse_freq(args)}))
            elif cmd == "phase":
                print(self.send({"cmd": "set_phase", "phase_deg": float(args[0])}))
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
    ap = argparse.ArgumentParser(description="External console for the SMB100A service")
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
                line = input("rf> ")
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
