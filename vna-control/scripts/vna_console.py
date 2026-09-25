"""A tiny external console for the VNA service (real PNA-X or simulator).

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `vna` imports --
so this one file can be copied to any machine with pyzmq.

    uv run scripts/vna_console.py                     # interactive, localhost
    uv run scripts/vna_console.py --connect 192.168.1.42
    uv run scripts/vna_console.py acquire              # one command, then exit

Commands
    acquire                 trigger a fresh sweep, wait for it, print the dip
    ref                     take a reference (an acquisition kept for u), wait for it
    clearref                forget the reference
    abort                   cancel a running acquisition
    sparam S11|S12|S21|S22  what is measured
    start <GHz>             sweep start
    stop <GHz>              sweep stop
    points <n>              points per sweep
    ifbw <kHz>              IF bandwidth
    power <dBm>             source power
    avg <n>                 sweeps averaged per acquisition
    cont on|off             continuous sweeping
    field mag2d|clMag|manual  where the field is read from
    field <mT> [deg]        the manual field (and angle)
    angle <deg>             the manual angle
    sample <name> <value>   e.g. sample alpha 1e-3   (ms_mT gamma_GHz_per_T alpha
                            dh0_mT h_anis_mT dip_dB)
    status                  print one status snapshot
    watch [seconds]         stream field / dip from the status broadcast (default 5 s)
    help                    show this list
    quit / exit             leave
"""

from __future__ import annotations

import argparse
import json
import time

import zmq

CMD_PORT = 5573
PUB_PORT = 5574
TIMEOUT_MS = 3000


def _f(v, scale=1.0, fmt=".4f"):
    return "--" if v is None else format(v / scale, fmt)


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host, self.cmd_port, self.pub_port = host, cmd_port, pub_port
        self.ctx = zmq.Context.instance()
        self.req = self._new_req()

    def _new_req(self):
        s = self.ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(f"tcp://{self.host}:{self.cmd_port}")
        return s

    def cmd(self, **msg) -> dict:
        self.req.send_json(msg)
        try:
            return self.req.recv_json()
        except zmq.Again:
            self.req.close(0)
            self.req = self._new_req()
            return {"ok": False, "error": "service did not respond (timeout)"}

    def run(self, words: list[str]) -> bool:
        """Execute one command line. Returns False to quit."""
        if not words:
            return True
        w, args = words[0].lower(), words[1:]
        num = lambda i=0: float(args[i])          # noqa: E731
        simple = {
            "start": lambda: self.cmd(cmd="set_start", start_Hz=num() * 1e9),
            "stop": lambda: self.cmd(cmd="set_stop", stop_Hz=num() * 1e9),
            "points": lambda: self.cmd(cmd="set_points", points=int(num())),
            "ifbw": lambda: self.cmd(cmd="set_ifbw", ifbw_Hz=num() * 1e3),
            "power": lambda: self.cmd(cmd="set_power", power_dBm=num()),
            "avg": lambda: self.cmd(cmd="set_averages", averages=int(num())),
            "abort": lambda: self.cmd(cmd="abort"),
            "cont": lambda: self.cmd(cmd="set_continuous", on=args[0].lower() == "on"),
            "sample": lambda: self.cmd(cmd="set_sample", name=args[0], value=num(1)),
            "sparam": lambda: self.cmd(cmd="set_sparam", sparam=args[0].upper()),
            "angle": lambda: self.cmd(cmd="set_manual_angle", angle_deg=num()),
            "clearref": lambda: self.cmd(cmd="clear_reference"),
        }
        try:
            if w in ("quit", "exit"):
                return False
            if w == "help":
                print(__doc__)
            elif w in simple:
                print(simple[w]())
            elif w == "field":
                if args[0] in ("mag2d", "clMag", "manual"):
                    print(self.cmd(cmd="set_field_source", source=args[0]))
                elif len(args) > 1:
                    print(self.cmd(cmd="set_manual_field", field_mT=num(), angle_deg=num(1)))
                else:
                    print(self.cmd(cmd="set_manual_field", field_mT=num()))
            elif w == "status":
                print(json.dumps(self.cmd(cmd="status").get("status"), indent=1))
            elif w == "acquire":
                self.acquire()
            elif w == "ref":
                self.acquire(verb="take_reference")
            elif w == "watch":
                self.watch(float(args[0]) if args else 5.0)
            else:
                print(f"unknown command {w!r}; try help")
        except (IndexError, ValueError):
            print(f"bad arguments for {w!r}; try help")
        return True

    def acquire(self, verb="acquire"):
        r = self.cmd(cmd=verb)
        if not r.get("ok"):
            print(r)
            return
        n = r["acq_id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            st = self.cmd(cmd="status").get("status") or {}
            if st.get("acq_id") == n and not st.get("acquiring"):
                t = self.cmd(cmd="get_trace", which="sample")
                if not t.get("ok"):
                    print(t)
                    return
                print(f"#{n} {t.get('sparam', '')}{' (reference)' if verb != 'acquire' else ''}: "
                      f"{t['points']} points {_f(t['start_Hz'], 1e9, '.3f')}-"
                      f"{_f(t['stop_Hz'], 1e9, '.3f')} GHz, field {_f(t['field_mT'], 1, '.3f')} mT"
                      f" at {_f(t.get('angle_deg'), 1, '.1f')} deg"
                      f"{'' if t['field_ok'] else ' (NOT live)'}, dip {_f(t['dip_Hz'], 1e9)} GHz "
                      f"({_f(t['dip_dB'], 1, '.2f')} dB), Kittel {_f(t['f_res_model_Hz'], 1e9)} GHz")
                return
            time.sleep(0.05)
        print(f"acquisition {n} did not finish")

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.connect(f"tcp://{self.host}:{self.pub_port}")
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                if sub.poll(300):
                    topic, payload = sub.recv_multipart()
                    d = json.loads(payload)
                    if topic == b"event":
                        print(f"  [{d.get('level')}] {d.get('msg')}")
                    else:
                        print(f"  field {_f(d.get('field_mT'), 1, '8.3f')} mT "
                              f"({d.get('field_source')})  dip {_f(d.get('dip_Hz'), 1e9)} GHz  "
                              f"sweeps {d.get('sweeps')}")
        finally:
            sub.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="console for the VNA service")
    ap.add_argument("--connect", default="localhost", metavar="HOST")
    ap.add_argument("--cmd-port", type=int, default=CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=PUB_PORT)
    ap.add_argument("words", nargs="*", help="one command to run, then exit")
    args = ap.parse_args()
    con = Console(args.connect, args.cmd_port, args.pub_port)
    if args.words:
        con.run(args.words)
        return 0
    print(f"vna console -> {args.connect}:{args.cmd_port}   (help for commands)")
    while True:
        try:
            line = input("vna> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not con.run(line.split()):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
