"""A tiny external console to talk to the SuperK control service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO `superk`
imports -- so you can copy this one file to any machine (that has pyzmq) and
drive the laser with it. Meant for poking at the communication by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/superk_console.py                     # connects to localhost
        superk> power 20
        superk> filter nIR2
        superk> line 1 1064 80
        superk> rf on
        superk> emission on
        superk> status
        superk> watch 5
        superk> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/superk_console.py status
    uv run scripts/superk_console.py emission off

Commands
    emission on|off        CLASS 4 LASER: switch emission (on asks to confirm;
                           'off' = the safety verb emission_off: works also
                           while a GUI has control)
    reset                  reset (acknowledge) a closed interlock
    power <pct>            EXTREME power level in %
    rf on|off              AOTF RF drive on/off
    filter <name>          select the AOTF crystal (VIS-nIR, nIR2, IR, ...)
    wl <line> <nm>         set the wavelength of line 1..8
    amp <line> <pct>       set the RF amplitude of line 1..8 (0 = line off)
    line <line> <nm> <pct> set both at once
    status                 print one status snapshot
    info                   print static info (limits, filters)
    watch [seconds]        stream the live status broadcast (default 5 s)
    help                   show this list
    quit / exit            leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                   take control if nobody has it
    take!                  take it over from whoever has it (they become a viewer)
    release                give it back
    clients                who holds control, who is connected
  While a GUI on another PC holds control, this console can read and
  switch the emission off but change nothing else until it takes control.
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
    taken elsewhere has no secure.py and talks plain; a secured superk will
    not answer it."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "superk" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("superk_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures superk."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "superk")

CMD_PORT = 5611
PUB_PORT = 5612
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "superk console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


def _on(word: str) -> bool:
    return word.lower() in ("on", "1", "true", "yes")


class Console:
    def __init__(self, host, cmd_port, pub_port, interactive=True):
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.interactive = interactive
        self.ctx = zmq.Context.instance()
        self.req = self._make_req()

    def _make_req(self):
        req = self.ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        req.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)   # a refused handshake must not block send
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
                # superk may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches superk)
                flipped = (attempt == 1 and _SECURE is not None
                           and _SECURE.no_answer(self.host, "superk"))
                self.req = self._make_req()
                if not flipped:
                    return {"ok": False, "error": "no reply (is the service running?)"}
        return {"ok": False, "error": "no reply (is the service running?)"}

    # ---- pretty printers -------------------------------------------------

    @staticmethod
    def show_status(s: dict):
        print(f"  emission={s.get('emission_state')}  interlock={s.get('interlock')}"
              f"  power={s.get('power_pct', 0):5.1f} %"
              f"  inlet={s.get('inlet_temp_C', 0):4.1f} C"
              f"  connected={s.get('connected')}")
        print(f"  RF={'ON ' if s.get('rf_on') else 'off'}  crystal={s.get('filter')}"
              f" ({s.get('filter_min_nm', 0):g}..{s.get('filter_max_nm', 0):g} nm)"
              f"  T={s.get('crystal_temp_C', 0):4.1f} C")
        wl, amp = s.get("wavelength_nm", []), s.get("amplitude_pct", [])
        for i, (w, a) in enumerate(zip(wl, amp)):
            if a > 0 or i == 0:
                print(f"    line {i + 1}: {w:9.3f} nm  {a:5.1f} %")
        if s.get("hw_error"):
            print(f"  HARDWARE ERROR: {s['hw_error']}")

    # ---- watch the live PUB stream --------------------------------------

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        _secure(sub, self.host)                  # telemetry is encrypted too
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        poller = zmq.Poller(); poller.register(sub, zmq.POLLIN)
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
            if cmd == "emission":
                on = _on(args[0])
                if on and self.interactive:
                    ans = input("  CLASS 4 LASER - switch emission ON? type yes: ")
                    if ans.strip().lower() != "yes":
                        print("  not switched on")
                        return True
                # off = the safety verb, allowed also while a GUI has control
                print(self.send({"cmd": "set_emission", "on": True} if on
                                else {"cmd": "emission_off"}))
            elif cmd in ("take", "take!"):
                print(self.send({"cmd": "take_control", "force": cmd == "take!"}))
            elif cmd == "release":
                print(self.send({"cmd": "release_control"}))
            elif cmd == "clients":
                print("  " + json.dumps(self.send({"cmd": "clients"}), indent=2).replace("\n", "\n  "))
            elif cmd == "reset":
                print(self.send({"cmd": "reset_interlock"}))
            elif cmd == "power":
                print(self.send({"cmd": "set_power", "power_pct": float(args[0])}))
            elif cmd == "rf":
                print(self.send({"cmd": "set_rf", "on": _on(args[0])}))
            elif cmd == "filter":
                print(self.send({"cmd": "set_filter", "filter": args[0]}))
            elif cmd == "wl":
                print(self.send({"cmd": "set_wavelength", "line": int(args[0]),
                                 "wavelength_nm": float(args[1])}))
            elif cmd == "amp":
                print(self.send({"cmd": "set_amplitude", "line": int(args[0]),
                                 "amplitude_pct": float(args[1])}))
            elif cmd == "line":
                print(self.send({"cmd": "set_line", "line": int(args[0]),
                                 "wavelength_nm": float(args[1]),
                                 "amplitude_pct": float(args[2])}))
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
                s.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)   # a refused handshake must not block send
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
    ap = argparse.ArgumentParser(description="External console for the SuperK service")
    ap.add_argument("--connect", default="localhost", help="service host (default: localhost)")
    ap.add_argument("--cmd-port", type=int, default=CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=PUB_PORT)
    ap.add_argument("words", nargs="*", help="a single command to run, then exit")
    args = ap.parse_args()

    # one-shot mode is for scripts: typing the command IS the confirmation
    con = Console(args.connect, args.cmd_port, args.pub_port, interactive=not args.words)
    try:
        if args.words:
            con.run_line(" ".join(args.words))
            return 0
        con.start_heartbeat()            # interactive: keep control while you think
        print(f"connected to tcp://{args.connect}:{args.cmd_port}   (type 'help' or 'quit')")
        while True:
            try:
                line = input("superk> ")
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
