"""A tiny external console to talk to the Kepco BOP control service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO `kepco`
imports -- so you can copy this one file to any machine (that has pyzmq) and
drive the supply with it. Meant for poking at the communication by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/kepco_console.py                     # connects to localhost
    uv run scripts/kepco_console.py --connect 192.168.1.42
        bop> mode current
        bop> vlim 5
        bop> i 1.5
        bop> out on
        bop> watch 5
        bop> out off
        bop> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/kepco_console.py status
    uv run scripts/kepco_console.py i 0.5

Commands
    mode current|voltage   select the mode (only with the output off)
    out on|off             output on (ramps up) / off (ramps to 0 first; 'off'
                           = the safety verb output_off: works also while a
                           GUI has control)
    kill                   output off NOW, no ramp (emergency; also a safety verb)
    i <A>                  current setpoint (current mode)
    v <V>                  voltage setpoint (voltage mode)
    ilim <A>               current limit (voltage mode)
    vlim <V>               voltage limit / compliance (current mode)
    rate i <A/s>           ramp rate in current mode
    rate v <V/s>           ramp rate in voltage mode
    acquire                average fresh readings, print the sample
    status                 print one status snapshot
    info                   print static info (limits, *IDN?)
    watch [seconds]        stream the live status broadcast (default 5 s)
    help                   show this list
    quit / exit            leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                   take control if nobody has it
    take!                  take it over from whoever has it (they become a viewer)
    release                give it back
    clients                who holds control, who is connected
  While a GUI on another PC holds control, this console can read and switch
  the output off but change nothing else until it takes control.
"""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import sys
import threading
import time
import uuid

import zmq


def _load_secure():
    """The module's secure.py (encryption, README "Encryption and keys"),
    loaded straight from its file when this console sits in its module folder
    -- so the console still imports no package and runs anywhere. A copy
    taken elsewhere has no secure.py and talks plain; a secured kepco will not
    answer it."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "kepco" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("kepco_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures kepco."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "kepco")


def _other_mode(host: str) -> bool:
    """No answer: kepco may run in the other mode than the policy now says
    (it was started before the policy changed). True when the next socket
    will try that mode (secure.no_answer; a wrong-mode request never
    reaches kepco, so resending once is safe)."""
    return _SECURE is not None and _SECURE.no_answer(host, "kepco")


CMD_PORT = 5581
PUB_PORT = 5582
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "kepco console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


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
        req.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)    # a refused handshake must not block send
        req.setsockopt(zmq.LINGER, 0)
        _secure(req, self.host)
        req.connect(f"tcp://{self.host}:{self.cmd_port}")
        return req

    # ---- send one command, get one reply --------------------------------

    def send(self, msg: dict) -> dict:
        msg.setdefault("client", IDENTITY)       # say who we are (control)
        for attempt in (1, 2):
            try:
                self.req.send_json(msg)
                return self.req.recv_json()
            except zmq.Again:
                # timed out -> the REQ socket is stuck; rebuild it so the next call works
                self.req.close(0)
                # the service may run in the other mode than the policy now
                # says: the rebuilt socket tries that mode, once
                flipped = attempt == 1 and _other_mode(self.host)
                self.req = self._new_req()
                if not flipped:
                    return {"ok": False, "error": "no reply (is the service running?)"}

    # ---- pretty printers -------------------------------------------------

    @staticmethod
    def show_status(s: dict):
        def f(key, fmt):
            v = s.get(key)
            return "   --   " if v is None else format(v, fmt)
        mode = s.get("mode", "?")
        unit = "A" if mode == "current" else "V"
        target = s.get("current_set_A") if mode == "current" else s.get("voltage_set_V")
        limit = (f"vlim={f('voltage_limit_V', '6.3f')} V" if mode == "current"
                 else f"ilim={f('current_limit_A', '6.4f')} A")
        print(f"  {mode:7s} out={'ON ' if s.get('output') else 'off'}"
              f" target={target if target is not None else 0:+8.4f} {unit}"
              f" prog={f('programmed', '+8.4f')} {unit}"
              f" {'RAMP' if s.get('ramping') else '    '}"
              f"  V={f('voltage_V', '+8.4f')} I={f('current_A', '+8.5f')}"
              f"  {limit}{'  AT LIMIT' if s.get('at_limit') else ''}")

    # ---- watch the live PUB stream --------------------------------------

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        _secure(sub, self.host)              # telemetry too
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        poller = zmq.Poller(); poller.register(sub, zmq.POLLIN)
        print(f"  watching for {seconds:.0f} s (Ctrl-C to stop early) ...")
        t_end = time.monotonic() + seconds
        try:
            while time.monotonic() < t_end:
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

    def acquire(self):
        r = self.send({"cmd": "acquire"})
        if not r.get("ok"):
            print(r); return
        n = r["acq_id"]
        t_end = time.monotonic() + 30
        while time.monotonic() < t_end:
            s = self.send({"cmd": "status"}).get("status", {})
            if s.get("acq_id") == n and not s.get("acquiring"):
                print("  " + json.dumps(s.get("sample", {})))
                return
            time.sleep(0.1)
        print("  acquisition timed out")

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
            if cmd == "mode":
                print(self.send({"cmd": "set_mode", "mode": args[0]}))
            elif cmd == "out":
                on = args[0].lower() in ("on", "1", "true")
                # off = the safety verb, allowed also while a GUI has control
                print(self.send({"cmd": "set_output", "on": True} if on
                                else {"cmd": "output_off"}))
            elif cmd in ("take", "take!"):
                print(self.send({"cmd": "take_control", "force": cmd == "take!"}))
            elif cmd == "release":
                print(self.send({"cmd": "release_control"}))
            elif cmd == "clients":
                print("  " + json.dumps(self.send({"cmd": "clients"}), indent=2).replace("\n", "\n  "))
            elif cmd == "kill":
                print(self.send({"cmd": "output_off_now"}))
            elif cmd == "i":
                print(self.send({"cmd": "set_current", "current_A": float(args[0])}))
            elif cmd == "v":
                print(self.send({"cmd": "set_voltage", "voltage_V": float(args[0])}))
            elif cmd == "ilim":
                print(self.send({"cmd": "set_current_limit", "current_A": float(args[0])}))
            elif cmd == "vlim":
                print(self.send({"cmd": "set_voltage_limit", "voltage_V": float(args[0])}))
            elif cmd == "rate":
                key = {"i": "rate_A_per_s", "v": "rate_V_per_s"}[args[0].lower()]
                print(self.send({"cmd": "set_ramp", key: float(args[1])}))
            elif cmd == "acquire":
                self.acquire()
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

    def start_heartbeat(self):
        """"Still here" in the background, on its OWN socket (a ZeroMQ socket
        belongs to one thread): while you think, control stays yours."""
        self._hb_stop = threading.Event()

        def beat():
            def make():
                s = self.ctx.socket(zmq.REQ)
                s.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
                s.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)    # a refused handshake must not block send
                s.setsockopt(zmq.LINGER, 0)
                _secure(s, self.host)
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
    ap = argparse.ArgumentParser(description="External console for the Kepco BOP service")
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
                line = input("bop> ")
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
