"""A tiny external console to talk to the TC200 heater service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO `tc200`
imports -- so you can copy this one file to any machine (that has pyzmq) and
drive the heater with it. Meant for poking at the communication by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/tc200_console.py                     # connects to localhost
        tc200> temp 45
        tc200> on
        tc200> wait 600
        tc200> status
        tc200> off
        tc200> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/tc200_console.py temp 40
    uv run scripts/tc200_console.py status

Commands
    temp <C>                   new setpoint in degC (the heater must be ON to heat)
    on | off                   heater output on / off
    pid <P> <I> <D>            the box's PID gains
    pmax <W>                   output power limit
    tmax <C>                   the box's over-temperature trip
    sensor ptc100|ptc1000|th10k   sensor type (refused while heating)
    status                     print one status snapshot
    wait [s]                   poll until the setpoint is reached (default 3600 s)
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

CMD_PORT = 5613
PUB_PORT = 5614
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
        return "  --  " if v is None else format(v, fmt)

    def show_status(self, s: dict):
        n = self._n
        alarms = [a for a, k in (("SENSOR ALARM", "sensor_alarm"), ("TMAX ALARM", "tmax_alarm"))
                  if s.get(k)]
        if s.get("sensor") and not s.get("sensor_ok"):
            alarms.append(f"WRONG SENSOR SETTING ({s.get('sensor')})")
        print(f"  T={n(s.get('temperature_C'), '7.2f')} C"
              f" (set {n(s.get('setpoint_C'), '.1f')},"
              f" {'REACHED' if s.get('temperature_stable') else 'not reached'})"
              f"   heater {'ON' if s.get('enabled') else 'off'}"
              f"   PID {s.get('p_gain')}/{s.get('i_gain')}/{s.get('d_gain')}"
              f"   PMAX {n(s.get('pmax_W'), '.1f')} W   TMAX {n(s.get('tmax_C'), '.1f')} C"
              + ("   " + ", ".join(alarms) if alarms else "")
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

    def wait(self, timeout_s: float):
        """The set -> wait-until-reached loop, the way scan-core does it. Here
        the setpoint was sent earlier, so the flag alone is checked; a scan
        also checks that the status shows ITS setpoint first (adopt_then_flag)."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            r = self.send({"cmd": "status"})
            s = r.get("status", {})
            if s.get("temperature_stable"):
                print(f"  reached after {time.monotonic() - t0:.1f} s")
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
            if cmd == "temp":
                print(self.send({"cmd": "set_temperature", "temperature_C": float(args[0])}))
            elif cmd == "on":
                print(self.send({"cmd": "set_enabled", "enabled": True}))
            elif cmd == "off":
                print(self.send({"cmd": "set_enabled", "enabled": False}))
            elif cmd == "pid":
                print(self.send({"cmd": "set_pid", "p": int(args[0]), "i": int(args[1]),
                                 "d": int(args[2])}))
            elif cmd == "pmax":
                print(self.send({"cmd": "set_pmax", "pmax_W": float(args[0])}))
            elif cmd == "tmax":
                print(self.send({"cmd": "set_tmax", "tmax_C": float(args[0])}))
            elif cmd == "sensor":
                print(self.send({"cmd": "set_sensor", "sensor": args[0]}))
            elif cmd == "status":
                r = self.send({"cmd": "status"})
                self.show_status(r.get("status", {})) if r.get("ok") else print(r)
            elif cmd == "wait":
                self.wait(float(args[0]) if args else 3600.0)
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
    ap = argparse.ArgumentParser(description="External console for the TC200 heater service")
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
                line = input("tc200> ")
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
