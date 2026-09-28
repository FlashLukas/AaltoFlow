"""A tiny external console for the scalar network analyser service.

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `shsna` imports --
so this one file can be copied to any machine with pyzmq.

    uv run scripts/shsna_console.py                     # interactive, localhost
    uv run scripts/shsna_console.py --connect 192.168.1.42
    uv run scripts/shsna_console.py acquire              # one command, then exit

Commands
    acquire                 run one TG acquisition, wait for it, print the result
    ref                     take a thru reference (DUT replaced by a thru), wait for it
    clearref                forget the reference
    abort                   cancel a running acquisition
    start <MHz>             sweep start
    stop <MHz>              sweep stop
    points <n>              points asked for (the analyser allows at most 1001)
    rbw <kHz>               resolution bandwidth (0 = the analyser's default)
    avg <n>                 sweeps averaged (in power) per acquisition
    cont on|off             continuous sweeping
    sim <name> <value>      simulator only, e.g. sim dut_inserted off / sim pad_dB 20
    status                  print one status snapshot
    watch [seconds]         stream peak / errors from the status broadcast (default 5 s)
    help                    show this list
    quit / exit             leave
"""

from __future__ import annotations

import argparse
import json
import time

import zmq

CMD_PORT = 5627
PUB_PORT = 5628
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
            "start": lambda: self.cmd(cmd="set_start", start_Hz=num() * 1e6),
            "stop": lambda: self.cmd(cmd="set_stop", stop_Hz=num() * 1e6),
            "points": lambda: self.cmd(cmd="set_points", points=int(num())),
            "rbw": lambda: self.cmd(cmd="set_rbw", rbw_Hz=num() * 1e3),
            "avg": lambda: self.cmd(cmd="set_averages", averages=int(num())),
            "abort": lambda: self.cmd(cmd="abort"),
            "cont": lambda: self.cmd(cmd="set_continuous", on=args[0].lower() == "on"),
            "sim": lambda: self.cmd(cmd="set_sim", name=args[0], value=args[1]),
            "clearref": lambda: self.cmd(cmd="clear_reference"),
        }
        try:
            if w in ("quit", "exit"):
                return False
            if w == "help":
                print(__doc__)
            elif w in simple:
                print(simple[w]())
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
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            st = self.cmd(cmd="status").get("status") or {}
            if st.get("acq_id") == n and not st.get("acquiring"):
                if st.get("acq_error"):
                    print(f"#{n} failed: {st['acq_error']}")
                    return
                raw = self.cmd(cmd="get_result", quantity="raw")
                tx = self.cmd(cmd="get_result", quantity="transmission")
                smp = st.get("sample") or {}
                print(f"#{n}{' (reference)' if verb != 'acquire' else ''}: "
                      f"{smp.get('points')} points {_f(smp.get('start_Hz'), 1e6, '.3f')}-"
                      f"{_f(smp.get('stop_Hz'), 1e6, '.3f')} MHz; "
                      f"raw peak {_f(raw.get('peak_db'), 1, '.2f')} dB (rel. TG output)")
                if tx.get("ok"):
                    print(f"   transmission: peak {_f(tx.get('peak_transmission_db'), 1, '.2f')} dB at "
                          f"{_f(tx.get('peak_freq_hz'), 1e6, '.3f')} MHz, mean "
                          f"{_f(tx.get('mean_transmission_db'), 1, '.2f')} dB, -3 dB width "
                          f"{_f(tx.get('bw3_hz'), 1e6, '.3f')} MHz")
                else:
                    print(f"   transmission: {tx.get('error')}")
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
                        print(f"  peak {_f(d.get('last_peak_db'), 1, '8.2f')} dB  "
                              f"T {_f(d.get('last_peak_transmission_db'), 1, '7.2f')} dB  "
                              f"sweeps {d.get('sweeps')}  {d.get('hw_error') or ''}")
        finally:
            sub.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="console for the shsna service")
    ap.add_argument("--connect", default="localhost", metavar="HOST")
    ap.add_argument("--cmd-port", type=int, default=CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=PUB_PORT)
    ap.add_argument("words", nargs="*", help="one command to run, then exit")
    args = ap.parse_args()
    con = Console(args.connect, args.cmd_port, args.pub_port)
    if args.words:
        con.run(args.words)
        return 0
    print(f"shsna console -> {args.connect}:{args.cmd_port}   (help for commands)")
    while True:
        try:
            line = input("shsna> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not con.run(line.split()):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
