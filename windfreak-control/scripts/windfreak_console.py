"""A tiny external console to talk to the Windfreak synthesizer service.

It speaks the raw wire protocol directly -- only `pyzmq` and `json`, NO
`windfreak` imports -- so you can copy this one file to any machine (that has
pyzmq) and drive the synthesizer with it. Meant for poking at it by hand.

Start the service first:
    uv run scripts/run_service.py

Then either use it interactively:
    uv run scripts/windfreak_console.py                  # connects to localhost
    uv run scripts/windfreak_console.py --connect 192.168.1.42
        wf> freq a 2.5 GHz
        wf> power a -10
        wf> rf a on
        wf> status
        wf> watch 5
        wf> quit

or fire a single command and exit (handy for scripts):
    uv run scripts/windfreak_console.py freq b 1e9
    uv run scripts/windfreak_console.py alloff

Commands  (channel = a | b)
    rf <ch> on|off           turn one RF output on or off
    alloff                   turn BOTH outputs off
    freq <ch> <value> [unit] set frequency; unit = Hz|kHz|MHz|GHz (default Hz)
    power <ch> <dBm>         set the output level in dBm
    phase <ch> <deg>         set the phase in degrees (0..360)
    ref int10|int27|ext [MHz]  select the reference (ext needs its frequency)
    status                   print one status snapshot
    info                     print static info (limits, id)
    describe                 list the parameters the service declares
    watch [seconds]          stream the live status broadcast (default 5 s)
    help                     show this list
    quit / exit              leave
"""

from __future__ import annotations

import argparse
import json

import zmq

CMD_PORT = 5583
PUB_PORT = 5584
TIMEOUT_MS = 3000

_UNITS = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}
_REFS = {"int10": "internal_10MHz", "int27": "internal_27MHz", "ext": "external"}


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


def parse_ch(token: str) -> str:
    ch = token.lower()
    if ch not in ("a", "b"):
        raise ValueError(f"channel must be a or b, not {token!r}")
    return ch


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
        for ch in ("a", "b"):
            f_hz = s.get(f"{ch}_frequency_Hz") or 0.0
            print(f"  {ch.upper()}: RF={'ON ' if s.get(f'{ch}_rf_on') else 'off'}"
                  f"  freq={f_hz/1e6:14.7f} MHz"
                  f"  power={s.get(f'{ch}_power_dBm', 0):7.2f} dBm"
                  f"  phase={s.get(f'{ch}_phase_deg', 0):7.2f} deg"
                  f"  lock={'yes' if s.get(f'{ch}_locked') else 'NO '}"
                  f"  leveled={'yes' if s.get(f'{ch}_leveled') else 'no'}")
        t = s.get("temperature_C")
        print(f"  ref={s.get('reference')} ({s.get('ext_ref_MHz')} MHz if external)"
              f"  temp={'-' if t is None else f'{t:.1f} C'}"
              f"  connected={s.get('connected')}"
              + (f"  ERROR: {s['hw_error']}" if s.get("hw_error") else ""))

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
            if cmd == "rf":
                on = args[1].lower() in ("on", "1", "true")
                print(self.send({"cmd": "set_rf", "channel": parse_ch(args[0]), "on": on}))
            elif cmd == "alloff":
                print(self.send({"cmd": "all_rf_off"}))
            elif cmd == "freq":
                print(self.send({"cmd": "set_frequency", "channel": parse_ch(args[0]),
                                 "frequency_Hz": parse_freq(args[1:])}))
            elif cmd == "power":
                print(self.send({"cmd": "set_power", "channel": parse_ch(args[0]),
                                 "power_dBm": float(args[1])}))
            elif cmd == "phase":
                print(self.send({"cmd": "set_phase", "channel": parse_ch(args[0]),
                                 "phase_deg": float(args[1])}))
            elif cmd == "ref":
                source = _REFS[args[0].lower()]
                msg = {"cmd": "set_reference", "source": source}
                if len(args) > 1:
                    msg["ext_MHz"] = float(args[1])
                print(self.send(msg))
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

    def close(self):
        self.req.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="External console for the Windfreak service")
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
                line = input("wf> ")
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
