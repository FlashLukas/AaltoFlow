"""A tiny external console to talk to the Keithley 2450 control service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO `k2450`
imports -- so you can copy this one file to any machine that has pyzmq and
drive the SMU with it. Meant for poking at the communication by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/k2450_console.py                     # connects to localhost
    uv run scripts/k2450_console.py --connect 192.168.1.42
        smu> limit 1 mA
        smu> volt 0.5
        smu> out on
        smu> acquire
        smu> out off
        smu> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/k2450_console.py status
    uv run scripts/k2450_console.py out off

Commands
    func v|i                 source voltage or current (switches the output off)
    volt <value> [unit]      source voltage level; unit V|mV (default V)
    curr <value> [unit]      source current level; unit A|mA|uA|nA (default A)
    limit <value> <unit>     compliance: a current (A|mA|uA|nA) when sourcing V,
                             a voltage (V|mV) when sourcing I
    out on|off               output relay ('off' = the safety verb output_off:
                             works also while a GUI has control)
    nplc <n>                 integration time in power-line cycles
    wire 2|4                 2-wire or 4-wire (remote sense)
    srange auto|<value>      source range (value in V or A)
    mrange auto|<value>      measure range
    readings <n>             readings averaged per acquisition
    acquire                  take a scan-safe sample and print it
    status                   print one status snapshot
    info                     print static info
    watch [seconds]          stream the live status broadcast (default 5 s)
    help                     show this list
    quit / exit              leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                   take control if nobody has it
    take!                  take it over from whoever has it (they become a viewer)
    release                give it back
    clients                who holds control, who is connected
  While a GUI on another PC holds control, this console can read and
  switch the output off but change nothing else until it takes control.
"""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import threading
import time
import uuid

import zmq


def _load_secure():
    """The module's secure.py (encryption, README "Encryption and keys"),
    loaded straight from its file when this console sits in its module folder
    -- so the console still imports no package and runs anywhere. A copy
    taken elsewhere has no secure.py and talks plain; a secured k2450 will not
    answer it."""
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "k2450" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("k2450_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures k2450."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "k2450")

CMD_PORT = 5623
PUB_PORT = 5624
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "k2450 console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control

_UNITS = {"v": 1.0, "mv": 1e-3, "a": 1.0, "ma": 1e-3, "ua": 1e-6, "na": 1e-9}


def parse_value(tokens) -> tuple[float, str]:
    """'1 mA' -> (0.001, 'a'); '0.5' -> (0.5, '')."""
    if not tokens:
        raise ValueError("needs a value")
    value = float(tokens[0])
    unit = ""
    if len(tokens) > 1:
        u = tokens[1].lower()
        if u not in _UNITS:
            raise ValueError(f"unknown unit {tokens[1]!r}")
        value *= _UNITS[u]
        unit = u[-1]
    return value, unit


def si(v, unit):
    """Engineering notation, ASCII only."""
    if v is None:
        return "--"
    for scale, p in ((1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= scale:
            return f"{v / scale:.5g} {p}{unit}"
    return f"{v:.3g} {unit}"


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host = host
        self.cmd_port = cmd_port
        self.pub_port = pub_port
        self.ctx = zmq.Context.instance()
        self.req = self._new_req()

    def _new_req(self):
        s = self.ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        s.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)   # a refused handshake must not block send
        s.setsockopt(zmq.LINGER, 0)
        _secure(s, self.host)
        s.connect(f"tcp://{self.host}:{self.cmd_port}")
        return s

    def send(self, msg: dict) -> dict:
        msg.setdefault("client", IDENTITY)       # say who we are (control)
        for attempt in (1, 2):
            try:
                self.req.send_json(msg)
                return self.req.recv_json()
            except zmq.Again:
                # timed out -> REQ socket is stuck; rebuild it so the next call works
                self.req.close(0)
                # k2450 may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches k2450)
                flipped = _SECURE is not None and _SECURE.no_answer(self.host, "k2450")
                self.req = self._new_req()
                if not (flipped and attempt == 1):
                    return {"ok": False, "error": "no reply (is the service running?)"}

    @staticmethod
    def show_status(s: dict):
        fn = s.get("source_function", "?")
        lim = (si(s.get("current_limit_A"), "A") if fn == "voltage"
               else si(s.get("voltage_limit_V"), "V"))
        print(f"  OUT={'ON ' if s.get('output') else 'off'}  source={fn}"
              f"  limit={lim}  V={si(s.get('voltage_V'), 'V')}"
              f"  I={si(s.get('current_A'), 'A')}"
              f"  R={si(s.get('resistance_ohm'), 'ohm')}"
              f"  {'COMPLIANCE' if s.get('tripped') else ''}")

    def acquire(self):
        r = self.send({"cmd": "acquire"})
        if not r.get("ok"):
            print(" ", r)
            return
        n = r["acq_id"]
        t0 = time.monotonic()
        while time.monotonic() - t0 < 60:
            st = self.send({"cmd": "status"}).get("status", {})
            # the id FIRST, then the busy flag: the frame from before the
            # trigger also says "not acquiring"
            if st.get("acq_id") == n and not st.get("acquiring"):
                smp = st.get("sample") or {}
                print(f"  #{n}: V={si(smp.get('voltage_V'), 'V')} "
                      f"+- {si(smp.get('voltage_std_V'), 'V')}  "
                      f"I={si(smp.get('current_A'), 'A')} "
                      f"+- {si(smp.get('current_std_A'), 'A')}  "
                      f"R={si(smp.get('resistance_ohm'), 'ohm')}  n={smp.get('n')}"
                      f"  {smp.get('flag') or ''}")
                return
            time.sleep(0.05)
        print("  timed out")

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        _secure(sub, self.host)              # telemetry too
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
            if cmd == "func":
                fn = {"v": "voltage", "i": "current"}.get(args[0].lower()[0], args[0])
                print(self.send({"cmd": "set_source_function", "function": fn}))
            elif cmd == "volt":
                print(self.send({"cmd": "set_voltage", "voltage_V": parse_value(args)[0]}))
            elif cmd == "curr":
                print(self.send({"cmd": "set_current", "current_A": parse_value(args)[0]}))
            elif cmd == "limit":
                value, unit = parse_value(args)
                if unit == "a":
                    print(self.send({"cmd": "set_current_limit", "current_limit_A": value}))
                elif unit == "v":
                    print(self.send({"cmd": "set_voltage_limit", "voltage_limit_V": value}))
                else:
                    print("  give a unit: 'limit 1 mA' (sourcing V) or 'limit 5 V' (sourcing I)")
            elif cmd == "out":
                on = args[0].lower() in ("on", "1")
                # off = the safety verb, allowed also while a GUI has control
                print(self.send({"cmd": "set_output", "on": True} if on
                                else {"cmd": "output_off"}))
            elif cmd in ("take", "take!"):
                print(self.send({"cmd": "take_control", "force": cmd == "take!"}))
            elif cmd == "release":
                print(self.send({"cmd": "release_control"}))
            elif cmd == "clients":
                print("  " + json.dumps(self.send({"cmd": "clients"}), indent=2).replace("\n", "\n  "))
            elif cmd == "nplc":
                print(self.send({"cmd": "set_nplc", "nplc": float(args[0])}))
            elif cmd == "wire":
                print(self.send({"cmd": "set_four_wire", "on": args[0] == "4"}))
            elif cmd in ("srange", "mrange"):
                which = "source" if cmd == "srange" else "measure"
                if args[0].lower() == "auto":
                    print(self.send({"cmd": f"set_{which}_auto_range", "on": True}))
                else:
                    print(self.send({"cmd": f"set_{which}_range",
                                     "range": parse_value(args)[0]}))
            elif cmd == "readings":
                print(self.send({"cmd": "set_acquisition", "readings": int(args[0])}))
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
    ap = argparse.ArgumentParser(description="External console for the Keithley 2450 service")
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
                line = input("smu> ")
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
