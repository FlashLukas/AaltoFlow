"""A tiny external console to talk to the optical-chopper control service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO
`chopper` imports -- so you can copy this one file to any machine (that has
pyzmq) and drive the chopper with it. Meant for poking at it by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/chopper_console.py                     # connects to localhost
    uv run scripts/chopper_console.py --connect 192.168.1.42
        chopper> freq 500
        chopper> start
        chopper> status
        chopper> watch 5
        chopper> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/chopper_console.py freq 1000
    uv run scripts/chopper_console.py status

Commands
    start | stop            run the wheel / standby
    freq <Hz>               internal-reference chopping frequency
    phase <deg>             phase adjust (0..360)
    blade <name>            e.g. MC1F60, MC1F10HP        (standby only)
    ref <mode>              e.g. internal, int-inner      (standby only)
    output <mode>           e.g. actual, inner, target    (standby only)
    harm <N> <D>            external harmonics, 1..15     (standby only)
    status                  print one status snapshot
    info                    print static info (blades, modes, limits)
    watch [seconds]         stream the live status broadcast (default 5 s)
    help                    show this list
    quit / exit             leave
"""

from __future__ import annotations

import argparse
import json

import zmq

CMD_PORT = 5609
PUB_PORT = 5610
TIMEOUT_MS = 3000


def _f(v, dec=2):
    return "--" if v is None else f"{v:.{dec}f}"


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.ctx = zmq.Context.instance()
        self.req = self._new_req()

    def _new_req(self):
        req = self.ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        req.setsockopt(zmq.LINGER, 0)
        req.connect(f"tcp://{self.host}:{self.cmd_port}")
        return req

    # ---- send one command, get one reply --------------------------------

    def send(self, msg: dict) -> dict:
        try:
            self.req.send_json(msg)
            return self.req.recv_json()
        except zmq.Again:
            # timed out -> REQ socket is stuck; rebuild it so the next call works
            self.req.close(0)
            self.req = self._new_req()
            return {"ok": False, "error": "no reply (is the service running?)"}

    # ---- pretty printers -------------------------------------------------

    @staticmethod
    def show_status(s: dict):
        print(f"  {'RUN    ' if s.get('enabled') else 'STANDBY'}"
              f"  {'LOCKED' if s.get('locked') else 'locking' if s.get('enabled') else '      '}"
              f"  blade={s.get('blade')} ref={s.get('ref_mode')} out={s.get('output_mode')}"
              f"  target={_f(s.get('target_frequency_Hz'))} Hz"
              f"  measured={_f(s.get('frequency_Hz'))} Hz"
              f"  phase={_f(s.get('phase_deg'), 0)} deg"
              + (f"  HW ERROR: {s.get('hw_error')}" if s.get("hw_error") else ""))

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
            if cmd in ("start", "stop"):
                print(self.send({"cmd": cmd}))
            elif cmd == "freq":
                print(self.send({"cmd": "set_frequency", "frequency_Hz": float(args[0])}))
            elif cmd == "phase":
                print(self.send({"cmd": "set_phase", "phase_deg": float(args[0])}))
            elif cmd == "blade":
                print(self.send({"cmd": "set_blade", "blade": args[0]}))
            elif cmd == "ref":
                print(self.send({"cmd": "set_ref_mode", "mode": args[0]}))
            elif cmd == "output":
                print(self.send({"cmd": "set_output_mode", "mode": args[0]}))
            elif cmd == "harm":
                print(self.send({"cmd": "set_harmonics", "n": int(args[0]), "d": int(args[1])}))
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
    ap = argparse.ArgumentParser(description="External console for the optical-chopper service")
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
                line = input("chopper> ")
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
