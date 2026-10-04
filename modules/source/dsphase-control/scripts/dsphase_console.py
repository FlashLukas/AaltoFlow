"""A tiny external console for the phase-shifter service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO `dsphase`
imports -- so you can copy this one file to any machine that has pyzmq and
drive the phase shifter with it. Meant for poking at the communication by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/dsphase_console.py                     # connects to localhost
    uv run scripts/dsphase_console.py --connect 192.168.1.42
        ps> phase 90
        ps> att 6.5
        ps> out on
        ps> status
        ps> watch 5
        ps> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/dsphase_console.py phase -45
    uv run scripts/dsphase_console.py status

Commands
    phase <deg>            set the phase shift in degrees (rounded to the step)
    att <dB>               set the output attenuator in dB
    out on|off             turn the RF output on or off ('off' = the safety
                           verb output_off: works also while a GUI has control)
    freq <MHz>             set the carrier frequency (bookkeeping unless configured)
    status                 print one status snapshot
    info                   print static info (limits, step sizes, *IDN?)
    describe               print the parameter manifest
    watch [seconds]        stream the live status broadcast (default 5 s)
    help                   show this list
    quit / exit            leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                   take control if nobody has it
    take!                  take it over from whoever has it (they become a viewer)
    release                give it back
    clients                who holds control, who is connected
  While a GUI on another PC holds control, this console can read and switch
  the RF output off but change nothing else until it takes control.
"""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import sys
import threading
import uuid

import zmq


def _load_secure():
    """The module's secure.py (encryption, README "Encryption and keys"),
    loaded straight from its file when this console sits in its module folder
    -- so the console still imports no package and runs anywhere. A copy
    taken elsewhere has no secure.py and talks plain; a secured dsphase will not
    answer it."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "dsphase" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("dsphase_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures dsphase."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "dsphase")


def _other_mode(host: str) -> bool:
    """No answer: dsphase may run in the other mode than the policy now says
    (it was started before the policy changed). True when the next socket
    will try that mode (secure.no_answer; a wrong-mode request never
    reaches dsphase, so resending once is safe)."""
    return _SECURE is not None and _SECURE.no_answer(host, "dsphase")


CMD_PORT = 5589
PUB_PORT = 5590
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "dsphase console",
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

    # ---- pretty printer --------------------------------------------------

    @staticmethod
    def show_status(s: dict):
        print(f"  OUT={'ON ' if s.get('output_on') else 'off'}"
              f"  phase={s.get('phase_deg', 0):8.2f} deg"
              f" (device {s.get('phase_device_deg', 0):7.2f})"
              f"  att={s.get('attenuation_dB', 0):6.2f} dB"
              f"  carrier={s.get('frequency_MHz', 0):8.1f} MHz"
              f"  connected={s.get('connected')}"
              + (f"  ERROR: {s['hw_error']}" if s.get("hw_error") else ""))

    # ---- watch the live PUB stream --------------------------------------

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        _secure(sub, self.host)              # telemetry too
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
            if cmd == "phase":
                print(self.send({"cmd": "set_phase", "phase_deg": float(args[0])}))
            elif cmd == "att":
                print(self.send({"cmd": "set_attenuation", "attenuation_dB": float(args[0])}))
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
            elif cmd == "freq":
                print(self.send({"cmd": "set_frequency", "frequency_MHz": float(args[0])}))
            elif cmd == "status":
                r = self.send({"cmd": "status"})
                self.show_status(r.get("status", {})) if r.get("ok") else print(r)
            elif cmd in ("info", "describe"):
                r = self.send({"cmd": cmd})
                print("  " + json.dumps(r.get(cmd, r), indent=2).replace("\n", "\n  "))
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
            hb = self._new_req()
            while not self._hb_stop.wait(HEARTBEAT_S):
                try:
                    hb.send_json({"cmd": "heartbeat", "client": IDENTITY})
                    hb.recv_json()
                except zmq.Again:                 # stuck REQ: rebuild it
                    hb.close(0)
                    hb = self._new_req()
            hb.close(0)
        threading.Thread(target=beat, daemon=True).start()

    def close(self):
        if getattr(self, "_hb_stop", None) is not None:
            self._hb_stop.set()
        self.req.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="External console for the phase-shifter service")
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
                line = input("ps> ")
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
