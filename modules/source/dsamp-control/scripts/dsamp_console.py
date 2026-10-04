"""A tiny external console for the RF-amplifier control service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO `dsamp`
imports -- so you can copy this one file to any machine with pyzmq and drive
the amplifier with it. Meant for poking at the communication by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/dsamp_console.py                     # connects to localhost
    uv run scripts/dsamp_console.py --connect 192.168.1.42
        amp> gain 6
        amp> freq 2.4 GHz
        amp> input -20
        amp> amp on
        amp> status
        amp> watch 5
        amp> off
        amp> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/dsamp_console.py gain 3.5
    uv run scripts/dsamp_console.py status

Commands
    amp on|off             switch the amplifier stage on or off
    off                    stage off (the panic button; 'amp off' and 'off' send
                           the safety verb amp_off: works also while a GUI
                           has control)
    gain <dB>              set the gain (clamped to the safety ceiling, 0.5 dB steps)
    freq <value> [unit]    signal frequency for the estimate; unit Hz|kHz|MHz|GHz (default Hz)
    input <dBm>            expected input level for the estimate
    status                 print one status snapshot
    info                   print static info (ranges, *IDN?)
    watch [seconds]        stream the live status broadcast (default 5 s)
    help                   show this list
    quit / exit            leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                   take control if nobody has it
    take!                  take it over from whoever has it (they become a viewer)
    release                give it back
    clients                who holds control, who is connected
  While a GUI on another PC holds control, this console can read and
  switch the amplifier off but change nothing else until it takes control.
"""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import threading
import uuid

import zmq

CMD_PORT = 5593
PUB_PORT = 5594
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "dsamp console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control

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
        self.req = self._new_req()

    def _new_req(self):
        req = self.ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        req.setsockopt(zmq.LINGER, 0)
        req.connect(f"tcp://{self.host}:{self.cmd_port}")
        return req

    # ---- send one command, get one reply --------------------------------

    def send(self, msg: dict) -> dict:
        msg.setdefault("client", IDENTITY)       # say who we are (control)
        try:
            self.req.send_json(msg)
            return self.req.recv_json()
        except zmq.Again:
            # timed out -> the REQ socket is stuck; rebuild it so the next call works
            self.req.close(0)
            self.req = self._new_req()
            return {"ok": False, "error": "no reply (is the service running?)"}

    # ---- pretty printers -------------------------------------------------

    @staticmethod
    def show_status(s: dict):
        print(f"  amp={'ON ' if s.get('amp_on') else 'off'}"
              f"  gain={s.get('gain_dB', 0):6.2f} dB"
              f"  (allowed {s.get('gain_min_dB', 0):g}..{s.get('gain_max_dB', 0):g})"
              f"  f={s.get('frequency_Hz', 0)/1e6:10.3f} MHz"
              f"  in={s.get('input_dBm', 0):6.2f} dBm"
              f"  est.out={s.get('est_output_dBm', 0):6.2f} dBm"
              f"  T={s.get('temperature_C', 0):5.1f} C"
              f"  USB={s.get('supply_V', 0):5.2f} V"
              f"  connected={s.get('connected')}"
              + (f"  HW ERROR: {s['hw_error']}" if s.get("hw_error") else ""))

    # ---- watch the live PUB stream --------------------------------------

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
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
            if cmd == "amp":
                on = args[0].lower() in ("on", "1", "true")
                # off = the safety verb, allowed also while a GUI has control
                print(self.send({"cmd": "set_amp", "on": True} if on
                                else {"cmd": "amp_off"}))
            elif cmd == "off":
                print(self.send({"cmd": "amp_off"}))
            elif cmd in ("take", "take!"):
                print(self.send({"cmd": "take_control", "force": cmd == "take!"}))
            elif cmd == "release":
                print(self.send({"cmd": "release_control"}))
            elif cmd == "clients":
                print("  " + json.dumps(self.send({"cmd": "clients"}), indent=2).replace("\n", "\n  "))
            elif cmd == "gain":
                print(self.send({"cmd": "set_gain", "gain_dB": float(args[0])}))
            elif cmd == "freq":
                print(self.send({"cmd": "set_frequency", "frequency_Hz": parse_freq(args)}))
            elif cmd == "input":
                print(self.send({"cmd": "set_input_power", "input_dBm": float(args[0])}))
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

    def start_heartbeat(self):
        """"Still here" in the background, on its OWN socket (a ZeroMQ socket
        belongs to one thread): while you think, control stays yours."""
        self._hb_stop = threading.Event()

        def beat():
            def make():
                s = self.ctx.socket(zmq.REQ)
                s.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
                s.setsockopt(zmq.LINGER, 0)
                s.connect(f"tcp://{self.host}:{self.cmd_port}")
                return s
            hb = make()
            while not self._hb_stop.wait(HEARTBEAT_S):
                try:
                    hb.send_json({"cmd": "heartbeat", "client": IDENTITY})
                    hb.recv_json()
                except zmq.Again:                 # stuck REQ: rebuild it
                    hb.close(0)
                    hb = make()
            hb.close(0)
        threading.Thread(target=beat, daemon=True).start()

    def close(self):
        if getattr(self, "_hb_stop", None) is not None:
            self._hb_stop.set()
        self.req.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="External console for the RF-amplifier service")
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
        con.start_heartbeat()            # interactive: keep control while you think
        print(f"connected to tcp://{args.connect}:{args.cmd_port}   (type 'help' or 'quit')")
        while True:
            try:
                line = input("amp> ")
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
