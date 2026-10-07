"""A tiny external console to talk to the function generator service (AFG1062).

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO
`afg` imports -- so you can copy this one file to any machine (that has
pyzmq) and drive the generator with it. Meant for poking at it by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/afg_console.py                  # connects to localhost
    uv run scripts/afg_console.py --connect 192.168.1.42
        afg> wave 1 sine
        afg> freq 1 30
        afg> amp 1 2
        afg> out 1 on
        afg> follow on 0
        afg> status
        afg> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/afg_console.py freq 1 1 kHz
    uv run scripts/afg_console.py off

Commands  (channel = 1 | 2)
    out <ch> on|off            switch one output on or off
    off                        switch EVERY output off (the safety verb
                               outputs_off: works also while a GUI has control)
    wave <ch> sine|square|pulse|ramp|noise|dc
    freq <ch> <value> [unit]   frequency; unit = Hz|kHz|MHz (default Hz)
    amp <ch> <Vpp>             amplitude, peak-to-peak, into the load setting
    offset <ch> <V>            offset (the level, for dc)
    phase <ch> <deg>           start phase, -180..180
    duty <ch> <pct>            pulse duty cycle
    sym <ch> <pct>             ramp symmetry (50 = triangle)
    load <ch> 50|highz|<ohm>   the load setting (changes what the volts mean)
    follow on|off [offset]     CH2's frequency follows CH1 (+ the phase offset)
    phasefollow on|off         CH2's phase follows CH1 too (off: its own phase)
    align                      re-align the channels' phases
    status                     print one status snapshot
    info                       print static info (limits, envelope, id)
    describe                   list the parameters the service declares
    watch [seconds]            stream the live status broadcast (default 5 s)
    help                       show this list
    quit / exit                leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                     take control if nobody has it
    take!                    take it over from whoever has it (they become a viewer)
    release                  give it back
    clients                  who holds control, who is connected
  While a GUI on another PC holds control, this console can read and switch
  every output off ('off') but change nothing else until it takes control.
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
    taken elsewhere has no secure.py and talks plain; a secured afg will not
    answer it."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "afg" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("afg_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures afg."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "afg")


CMD_PORT = 5631
PUB_PORT = 5632
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "afg console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control

_UNITS = {"hz": 1.0, "khz": 1e3, "mhz": 1e6}


def parse_freq(tokens) -> float:
    """'1.5 kHz' / '1e3' / '10 MHz' -> Hz as a float."""
    if not tokens:
        raise ValueError("frequency needs a value")
    value = float(tokens[0])
    if len(tokens) > 1:
        unit = tokens[1].lower()
        if unit not in _UNITS:
            raise ValueError(f"unknown unit {tokens[1]!r} (use Hz/kHz/MHz)")
        value *= _UNITS[unit]
    return value


def parse_ch(token: str) -> str:
    ch = token.lower().removeprefix("ch")
    if ch not in ("1", "2"):
        raise ValueError(f"channel must be 1 or 2, not {token!r}")
    return "ch" + ch


def _on(token: str) -> bool:
    return token.lower() in ("on", "1", "true", "yes")


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
        req.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)  # a refused handshake must not block send
        req.setsockopt(zmq.LINGER, 0)
        _secure(req, self.host)                   # encrypted when the policy says so
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
                # timed out -> REQ socket is stuck; rebuild it so the next call works
                self.req.close(0)
                # afg may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches afg)
                flipped = _SECURE is not None and _SECURE.no_answer(self.host, "afg")
                self.req = self._new_req()
                if not (flipped and attempt == 1):
                    return {"ok": False, "error": "no reply (is the service running?)"}

    # ---- pretty printers -------------------------------------------------

    @staticmethod
    def show_status(s: dict):
        for ch in s.get("channels", ["ch1", "ch2"]):
            wf = s.get(f"{ch}_waveform")
            line = (f"  {ch.upper()}: out={'ON ' if s.get(f'{ch}_output') else 'off'}"
                    f"  {wf:<6}")
            if wf not in ("dc", "noise"):
                line += f"  f={s.get(f'{ch}_frequency_Hz') or 0:.6g} Hz"
            if wf != "dc":
                line += f"  {s.get(f'{ch}_amplitude_Vpp') or 0:.4g} Vpp"
            line += (f"  offset={s.get(f'{ch}_offset_V') or 0:.4g} V"
                     f"  phase={s.get(f'{ch}_phase_deg') or 0:.2f} deg"
                     f"  load={s.get(f'{ch}_load')}"
                     f"  settled={'yes' if s.get(f'{ch}_settled') else 'NO '}")
            if s.get(f"{ch}_mode") not in (None, "continuous"):
                line += f"  MODE={s[f'{ch}_mode']}"
            print(line)
            if s.get(f"{ch}_mismatch"):
                print(f"       not as asked: {s[f'{ch}_mismatch']}")
        print(f"  follow={s.get('follow')} (offset {s.get('phase_offset_deg')} deg)"
              f"  connected={s.get('connected')}"
              + (f"  ERROR: {s['hw_error']}" if s.get("hw_error") else ""))

    # ---- watch the live PUB stream --------------------------------------

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        _secure(sub, self.host)                  # telemetry too
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
            simple = {"amp": ("set_amplitude", "amplitude_Vpp"),
                      "offset": ("set_offset", "offset_V"),
                      "phase": ("set_phase", "phase_deg"),
                      "duty": ("set_duty", "duty_pct"),
                      "sym": ("set_symmetry", "symmetry_pct")}
            if cmd == "out":
                print(self.send({"cmd": "set_output", "channel": parse_ch(args[0]),
                                 "on": _on(args[1])}))
            elif cmd == "off":
                # the safety verb, allowed also while a GUI has control
                print(self.send({"cmd": "outputs_off"}))
            elif cmd in simple:
                verb, key = simple[cmd]
                print(self.send({"cmd": verb, "channel": parse_ch(args[0]),
                                 key: float(args[1])}))
            elif cmd == "wave":
                print(self.send({"cmd": "set_waveform", "channel": parse_ch(args[0]),
                                 "waveform": args[1].lower()}))
            elif cmd == "load":
                print(self.send({"cmd": "set_load", "channel": parse_ch(args[0]),
                                 "load": args[1]}))
            elif cmd == "phasefollow":
                print(self.send({"cmd": "set_phase_follow", "on": _on(args[0])}))
            elif cmd == "follow":
                msg = {"cmd": "set_follow", "on": _on(args[0])}
                if len(args) > 1:
                    msg["phase_offset_deg"] = float(args[1])
                print(self.send(msg))
            elif cmd == "align":
                print(self.send({"cmd": "align_phase"}))
            elif cmd in ("take", "take!"):
                print(self.send({"cmd": "take_control", "force": cmd == "take!"}))
            elif cmd == "release":
                print(self.send({"cmd": "release_control"}))
            elif cmd == "clients":
                print("  " + json.dumps(self.send({"cmd": "clients"}), indent=2).replace("\n", "\n  "))
            elif cmd == "freq":
                print(self.send({"cmd": "set_frequency", "channel": parse_ch(args[0]),
                                 "frequency_Hz": parse_freq(args[1:])}))
            elif cmd == "status":
                r = self.send({"cmd": "status"})
                self.show_status(r.get("status", {})) if r.get("ok") else print(r)
            elif cmd == "info":
                r = self.send({"cmd": "info"})
                print("  " + json.dumps(r.get("info", r), indent=2).replace("\n", "\n  "))
            elif cmd == "describe":
                r = self.send({"cmd": "describe"})
                for p in r.get("describe", {}).get("parameters", []):
                    rng = f"  [{p['min']:g} .. {p['max']:g}]" if "min" in p else ""
                    print(f"  {p['id']:<22} {p['kind']:<9} {p.get('unit', ''):<5}{rng}")
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
                return self._new_req()     # same options and mode as send()
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
    ap = argparse.ArgumentParser(description="External console for the function generator service")
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
                line = input("afg> ")
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
