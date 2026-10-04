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
    zero                   ramp to 0 A (the safety verb: works also while a GUI has control)
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

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                   take control if nobody has it
    take!                  take it over from whoever has it (they become a viewer)
    release                give it back
    clients                who holds control, who is connected
  While a GUI on another PC holds control, this console can read and 'zero'
  but not change anything until it takes control.
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
    taken elsewhere has no secure.py and talks plain; a secured clMag will
    not answer it."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "clMag" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("clMag_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures clMag."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "clMag")


CMD_PORT = 5555
PUB_PORT = 5556
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "clMag console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.ctx = zmq.Context.instance()
        self.req = self.make_req()

    def make_req(self):
        s = self.ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        s.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)   # a refused handshake must not block send
        s.setsockopt(zmq.LINGER, 0)
        _secure(s, self.host)
        s.connect(f"tcp://{self.host}:{self.cmd_port}")
        return s

    # ---- send one command, get one reply --------------------------------

    def send(self, msg: dict) -> dict:
        msg.setdefault("client", IDENTITY)       # say who we are (control)
        for attempt in (1, 2):
            try:
                self.req.send_json(msg)
                return self.req.recv_json()
            except zmq.Again:
                # timed out -> REQ socket is stuck; rebuild it so the next call works
                self.req.close(0)
                # clMag may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches clMag)
                flipped = (attempt == 1 and _SECURE is not None
                           and _SECURE.no_answer(self.host, "clMag"))
                self.req = self.make_req()
                if not flipped:
                    return {"ok": False, "error": "no reply (is the service running?)"}
        return {"ok": False, "error": "no reply (is the service running?)"}

    # ---- pretty printers -------------------------------------------------

    @staticmethod
    def show_status(s: dict):
        sp = s.get("setpoint_field_mT")
        sp_txt = "—" if sp is None else f"{sp:.3f} mT"
        print(f"  state={s.get('state','?'):9}  field={s.get('measured_field_mT',0):8.3f} mT"
              f"  current={s.get('current_A',0):7.3f} A  setpoint={sp_txt}"
              f"  stable={s.get('field_stable')}  locked={s.get('locked')}")
        # hardware / loop failures (2026-09-28); the values above are then
        # the last good ones
        for key in ("hw_error", "loop_error"):
            if s.get(key):
                print(f"  {key.upper()}: {s[key]}")

    # ---- watch the live PUB stream --------------------------------------

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        _secure(sub, self.host)                  # telemetry is encrypted too
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
            elif cmd == "zero":
                print(self.send({"cmd": "ramp_to_zero"}))
            elif cmd in ("take", "take!"):
                print(self.send({"cmd": "take_control", "force": cmd == "take!"}))
            elif cmd == "release":
                print(self.send({"cmd": "release_control"}))
            elif cmd == "clients":
                r = self.send({"cmd": "clients"})
                print("  " + json.dumps(r, indent=2).replace("\n", "\n  "))
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

    def start_heartbeat(self):
        """"Still here" in the background, on its OWN socket (a ZeroMQ socket
        belongs to one thread): while you think, control stays yours."""
        self._hb_stop = threading.Event()

        def beat():
            hb = self.make_req()
            while not self._hb_stop.wait(HEARTBEAT_S):
                try:
                    hb.send_json({"cmd": "heartbeat", "client": IDENTITY})
                    hb.recv_json()
                except zmq.Again:                 # stuck REQ: rebuild it
                    hb.close(0)
                    hb = self.make_req()          # in the mode send() found working
            hb.close(0)
        threading.Thread(target=beat, daemon=True).start()

    def close(self):
        if getattr(self, "_hb_stop", None) is not None:
            self._hb_stop.set()
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
        con.start_heartbeat()
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
