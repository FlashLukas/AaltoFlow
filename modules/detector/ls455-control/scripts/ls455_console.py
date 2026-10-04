"""A tiny external console for the gaussmeter service.

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `ls455` imports --
so this one file can be copied to any machine with pyzmq.

    uv run scripts/ls455_console.py                   # interactive, localhost
    uv run scripts/ls455_console.py --connect 192.168.1.42
    uv run scripts/ls455_console.py field              # one command, then exit

Commands (every field in mT)
    field                  one live reading
    acquire                average fresh, settled readings; print the latched sample
    mode dc|rms|peak       measurement mode (peak keeps the front panel's peak settings)
    digits 3|4|5           DC resolution (= the meter's filter)
    band wide|narrow       RMS band
    auto on|off            auto range on/off
    range <mT>             manual full-scale range (switches auto off)
    unit G|T|Oe|A/m        what the meter's own display shows
    rel on|off [mT]        relative mode, optionally with a setpoint
    relhere                relative to the field measured now
    readings <n>           readings per acquisition
    zero                   zero the probe -- ZERO-GAUSS CHAMBER FIRST
    clearzero              forget the stored probe zero
    probe                  re-read the probe from the meter (after swapping it)
    status                 print one status snapshot
    info                   static info (probe, ranges)
    watch [seconds]        stream the live status broadcast (default 5 s)
    help                   show this list
    quit / exit            leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                   take control if nobody has it
    take!                  take it over from whoever has it (they become a viewer)
    release                give it back
    clients                who holds control, who is connected
  While a GUI on another PC holds control, this console can read (status,
  info, watch, probe) but change nothing until it takes control.
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
    taken elsewhere has no secure.py and talks plain; a secured ls455 will not
    answer it."""
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "ls455" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("ls455_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures ls455."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "ls455")

CMD_PORT = 5615
PUB_PORT = 5616
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "ls455 console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


def fmt_mt(v) -> str:
    """ASCII only (this goes to stdout, gotcha #14)."""
    if v is None:
        return "--"
    a = abs(v)
    if a >= 1000:
        return f"{v / 1000:.6g} T"
    if a >= 1 or a == 0:
        return f"{v:.6g} mT"
    return f"{v * 1000:.6g} uT"


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host, self.cmd_port, self.pub_port = host, cmd_port, pub_port
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
        msg.setdefault("client", IDENTITY)      # say who we are (control)
        for attempt in (1, 2):
            try:
                self.req.send_json(msg)
                return self.req.recv_json()
            except zmq.Again:
                # timed out -> REQ socket is stuck; rebuild it so the next call works
                self.req.close(0)
                # ls455 may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches ls455)
                flipped = _SECURE is not None and _SECURE.no_answer(self.host, "ls455")
                self.req = self._new_req()
                if not (flipped and attempt == 1):
                    return {"ok": False, "error": "no reply (is the service running?)"}

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

    @staticmethod
    def show_status(s: dict):
        mode = s.get("mode", "dc")
        detail = (f"{s.get('dc_digits')} digits" if mode == "dc" else
                  f"{s.get('rms_band')} band" if mode == "rms" else
                  f"{s.get('peak_mode')} {s.get('peak_display')}")
        print(f"  B={fmt_mt(s.get('field_mT')):>14} {s.get('flag') or ''}"
              f"  {mode.upper()} {detail}"
              f"  range={'auto ' if s.get('auto_range') else 'manual '}{fmt_mt(s.get('range_mT'))}"
              f"{'  rel ' + fmt_mt(s.get('field_rel_mT')) if s.get('relative') else ''}"
              f"  acq#{s.get('acq_id')}{' busy' if s.get('acquiring') else ''}"
              f"{'  ZEROING' if s.get('zeroing') else ''}"
              f"{'  ERROR ' + s['hw_error'] if s.get('hw_error') else ''}")

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        _secure(sub, self.host)              # telemetry too
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        poller = zmq.Poller(); poller.register(sub, zmq.POLLIN)
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                if poller.poll(200):
                    topic, payload = sub.recv_multipart()
                    d = json.loads(payload)
                    if topic == b"status":
                        self.show_status(d)
                    else:
                        print(f"  event [{d.get('level')}] {d.get('msg')}")
        except KeyboardInterrupt:
            print("  (stopped)")
        finally:
            sub.close(0)

    def acquire(self):
        r = self.send({"cmd": "acquire"})
        if not r.get("ok"):
            print(" ", r)
            return
        n = r["acq_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            s = self.send({"cmd": "status"}).get("status", {})
            # id first, then the flag: a stale "not acquiring" must not fool us
            if s.get("acq_id") == n and not s.get("acquiring"):
                smp = s.get("sample", {})
                print(f"  #{n}: {fmt_mt(smp.get('field_mT'))} +- {fmt_mt(smp.get('std_mT'))}"
                      f"  (n={smp.get('n')}) {smp.get('flag') or ''}")
                return
            time.sleep(0.05)
        print(f"  acquisition {n} timed out")

    def run_line(self, line: str) -> bool:
        parts = line.split()
        if not parts:
            return True
        cmd, args = parts[0].lower(), parts[1:]
        on = lambda a: a.lower() in ("on", "1", "true", "yes")
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd in ("help", "?"):
            print(__doc__.split("Commands")[1])
            return True
        try:
            if cmd == "field":
                s = self.send({"cmd": "status"}).get("status", {})
                print(f"  {fmt_mt(s.get('field_mT'))} {s.get('flag') or ''}")
            elif cmd == "acquire":
                self.acquire()
            elif cmd == "mode":
                print(self.send({"cmd": "set_mode", "mode": args[0].lower()}))
            elif cmd == "digits":
                print(self.send({"cmd": "set_dc_digits", "digits": int(args[0])}))
            elif cmd == "band":
                print(self.send({"cmd": "set_rms_band", "band": args[0].lower()}))
            elif cmd == "auto":
                print(self.send({"cmd": "set_auto_range", "on": on(args[0])}))
            elif cmd == "range":
                print(self.send({"cmd": "set_range", "range_mT": float(args[0])}))
            elif cmd == "unit":
                print(self.send({"cmd": "set_display_unit", "unit": args[0]}))
            elif cmd == "rel":
                msg = {"cmd": "set_relative", "on": on(args[0])}
                if len(args) > 1:
                    msg["setpoint_mT"] = float(args[1])
                print(self.send(msg))
            elif cmd == "relhere":
                print(self.send({"cmd": "relative_here"}))
            elif cmd == "readings":
                print(self.send({"cmd": "set_acquisition", "readings": int(args[0])}))
            elif cmd == "zero":
                if input("  probe in the zero-gauss chamber? [y/N] ").strip().lower() == "y":
                    print(self.send({"cmd": "zero"}))
            elif cmd == "clearzero":
                print(self.send({"cmd": "clear_zero"}))
            elif cmd == "probe":
                r = self.send({"cmd": "reread_probe"})
                if not r.get("ok"):
                    print(" ", r)
                else:
                    s = self.send({"cmd": "status"}).get("status", {})
                    print(f"  probe: {s.get('probe_desc')}  ranges (mT): {s.get('ranges_mT')}")
            elif cmd == "take":
                print(self.send({"cmd": "take_control", "force": False}))
            elif cmd == "take!":
                print(self.send({"cmd": "take_control", "force": True}))
            elif cmd == "release":
                print(self.send({"cmd": "release_control"}))
            elif cmd == "clients":
                print("  " + json.dumps(self.send({"cmd": "clients"}), indent=1).replace("\n", "\n  "))
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

    def close(self):
        self.req.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="External console for the gaussmeter service")
    ap.add_argument("--connect", default="localhost", help="service host (default: localhost)")
    ap.add_argument("--cmd-port", type=int, default=CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=PUB_PORT)
    ap.add_argument("words", nargs="*", help="a single command to run, then exit")
    args = ap.parse_args()

    con = Console(args.connect, args.cmd_port, args.pub_port)
    try:
        if args.words:
            con.run_line(" ".join(args.words))
            return 0
        con.start_heartbeat()
        print(f"connected to tcp://{args.connect}:{args.cmd_port}   (type 'help' or 'quit')")
        while True:
            try:
                line = input("ls455> ")
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
