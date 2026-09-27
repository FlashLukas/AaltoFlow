"""A tiny external console for the spectrum-analyser service (real Signal Hound or simulator).

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `signalhound`
imports -- so this one file can be copied to any machine with pyzmq.

    uv run scripts/signalhound_console.py                     # interactive, localhost
    uv run scripts/signalhound_console.py --connect 192.168.1.42
    uv run scripts/signalhound_console.py acquire              # one command, then exit

Commands
    acquire                 trigger fresh sweeps, wait for them, print the peak
    ref                     take the thru reference (tracking generator on), wait for it
    clearref                forget the reference
    tx                      acquire and print the transmission against the thru
    abort                   cancel a running acquisition
    center <GHz>            centre frequency
    span <MHz>              span
    startstop <GHz> <GHz>   start and stop instead of centre and span
    ref_level <dBm>         reference level (top of the screen)
    rbw <kHz>               resolution bandwidth
    vbw <kHz>               video bandwidth (<= RBW)
    detector average|peak   the bin detector
    reject on|off           software image rejection
    avg <n>                 sweeps averaged per acquisition (in power)
    cont on|off             continuous sweeping
    tg on|off               the tracking generator (transmission sweep)
    tglevel <dBm>           TG output level (-30 ... -10)
    tgpoints <n>            requested points per TG sweep
    scene <name> <value>    simulator only, e.g. scene dut_inserted off,
                            scene tone_dBm -20, scene dut_bandwidth_Hz 20e6
    status                  print one status snapshot
    watch [seconds]         stream peak / floor from the status broadcast (default 5 s)
    help                    show this list
    quit / exit             leave
"""

from __future__ import annotations

import argparse
import json
import time

import zmq

CMD_PORT = 5587
PUB_PORT = 5588
TIMEOUT_MS = 3000


def _f(v, scale=1.0, fmt=".4f"):
    return "--" if v is None else format(v / scale, fmt)


def _onoff(word: str) -> bool:
    return word.lower() in ("on", "1", "true", "yes")


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
            "center": lambda: self.cmd(cmd="set_center", center_Hz=num() * 1e9),
            "span": lambda: self.cmd(cmd="set_span", span_Hz=num() * 1e6),
            "startstop": lambda: self.cmd(cmd="set_start_stop", start_Hz=num() * 1e9,
                                          stop_Hz=num(1) * 1e9),
            "ref_level": lambda: self.cmd(cmd="set_ref_level", ref_level_dBm=num()),
            "rbw": lambda: self.cmd(cmd="set_rbw", rbw_Hz=num() * 1e3),
            "vbw": lambda: self.cmd(cmd="set_vbw", vbw_Hz=num() * 1e3),
            "detector": lambda: self.cmd(cmd="set_detector", detector=args[0].lower()),
            "reject": lambda: self.cmd(cmd="set_reject", on=_onoff(args[0])),
            "avg": lambda: self.cmd(cmd="set_averages", averages=int(num())),
            "cont": lambda: self.cmd(cmd="set_continuous", on=_onoff(args[0])),
            "tg": lambda: self.cmd(cmd="set_tg", on=_onoff(args[0])),
            "tglevel": lambda: self.cmd(cmd="set_tg_level", tg_level_dBm=num()),
            "tgpoints": lambda: self.cmd(cmd="set_tg_points", points=int(num())),
            "abort": lambda: self.cmd(cmd="abort"),
            "clearref": lambda: self.cmd(cmd="clear_reference"),
        }
        try:
            if w in ("quit", "exit"):
                return False
            if w == "help":
                print(__doc__)
            elif w in simple:
                print(simple[w]())
            elif w == "scene":
                val = args[1]
                if val.lower() in ("on", "off", "true", "false"):
                    value = _onoff(val)
                else:
                    value = float(val)
                print(self.cmd(cmd="set_scene", name=args[0], value=value))
            elif w == "status":
                print(json.dumps(self.cmd(cmd="status").get("status"), indent=1))
            elif w == "acquire":
                self.acquire()
            elif w == "ref":
                self.acquire(verb="take_reference")
            elif w == "tx":
                self.acquire(quantity="transmission")
            elif w == "watch":
                self.watch(float(args[0]) if args else 5.0)
            else:
                print(f"unknown command {w!r}; try help")
        except (IndexError, ValueError):
            print(f"bad arguments for {w!r}; try help")
        return True

    def acquire(self, verb="acquire", quantity="trace"):
        r = self.cmd(cmd=verb)
        if not r.get("ok"):
            print(r)
            return
        n = r["acq_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            st = self.cmd(cmd="status").get("status") or {}
            # the id first, then the flag: right after the trigger an old
            # frame can still say "not acquiring" (suite gotcha #17)
            if st.get("acq_id") == n and not st.get("acquiring"):
                t = self.cmd(cmd="get_trace", which="sample", quantity=quantity)
                if not t.get("ok"):
                    print(t)
                    return
                head = (f"#{n}{' (thru reference)' if verb != 'acquire' else ''}: "
                        f"{t['points']} bins {_f(t['start_Hz'], 1e9, '.6f')}-"
                        f"{_f(t['stop_Hz'], 1e9, '.6f')} GHz, RBW {_f(t['rbw_Hz'], 1e3, 'g')} kHz, "
                        f"{t['averages']} avg")
                if quantity == "transmission":
                    y = [v for v in t["transmission"] if v is not None]
                    print(head + f", transmission {min(y):.2f} ... {max(y):.2f} dB, "
                                 f"at centre {_f(t.get('tx_center_dB'), 1, '.2f')} dB")
                else:
                    print(head + f", peak {_f(t['peak_Hz'], 1e9, '.6f')} GHz at "
                                 f"{_f(t['peak_dBm'], 1, '.2f')} dBm, floor "
                                 f"{_f(t['floor_dBm'], 1, '.1f')} dBm"
                          + ("  OVERLOAD" if t.get("overload") else ""))
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
                        print(f"  peak {_f(d.get('peak_Hz'), 1e9, '.6f')} GHz "
                              f"{_f(d.get('peak_dBm'), 1, '7.2f')} dBm  floor "
                              f"{_f(d.get('floor_dBm'), 1, '7.2f')} dBm  "
                              f"{'TG ' if d.get('tg_on') else ''}sweeps {d.get('sweeps')}")
        finally:
            sub.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="console for the spectrum-analyser service")
    ap.add_argument("--connect", default="localhost", metavar="HOST")
    ap.add_argument("--cmd-port", type=int, default=CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=PUB_PORT)
    ap.add_argument("words", nargs="*", help="one command to run, then exit")
    args = ap.parse_args()
    con = Console(args.connect, args.cmd_port, args.pub_port)
    if args.words:
        con.run(args.words)
        return 0
    print(f"signalhound console -> {args.connect}:{args.cmd_port}   (help for commands)")
    while True:
        try:
            line = input("signalhound> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not con.run(line.split()):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
