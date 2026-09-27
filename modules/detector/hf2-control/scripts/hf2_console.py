"""A tiny external console for the lock-in service.

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `hf2` import --
so this one file can be copied to any machine with pyzmq.

Start the service first:
    uv run scripts/run_service.py

Then:
    uv run scripts/hf2_console.py                      # interactive, localhost
    uv run scripts/hf2_console.py --connect 192.168.1.42
        hf2> tc 1 30 ms
        hf2> order 2 4
        hf2> ref 1 int
        hf2> freq 1 1.2345 kHz
        hf2> acquire
        hf2> status
        hf2> quit

or one command and exit:
    uv run scripts/hf2_console.py acquire

Commands
    tc <ch> <value> [us|ms|s]      set a channel's time constant (default unit s)
    order <ch> <1..8>              set a channel's filter order
    ref <ch> int|ext               internal or external reference
    freq <ch> <value> [Hz|kHz|MHz] set the frequency (internal reference only)
    acquire                        settle, latch and print one sample (waits)
    status                         print one status snapshot
    info                           static info (limits, channel routing)
    watch [seconds]                stream the live status broadcast (default 5 s)
    help                           show this list
    quit / exit                    leave

Channels are 1 and 2.
"""

from __future__ import annotations

import argparse
import json
import time

import zmq

CMD_PORT = 5569
PUB_PORT = 5570
TIMEOUT_MS = 3000

_TC = {"us": 1e-6, "ms": 1e-3, "s": 1.0}
_HZ = {"hz": 1.0, "khz": 1e3, "mhz": 1e6}


def _value(tokens, units, default_unit):
    v = float(tokens[0])
    unit = tokens[1].lower() if len(tokens) > 1 else default_unit
    if unit not in units:
        raise ValueError(f"unknown unit {unit!r} (use {'/'.join(units)})")
    return v * units[unit]


def _v(x):
    """Volts, human-scaled, ASCII only (this prints to pipes too)."""
    if x is None:
        return "--"
    a = abs(x)
    for s, u in ((1.0, "V"), (1e-3, "mV"), (1e-6, "uV")):
        if a >= s:
            return f"{x / s:9.4f} {u}"
    return f"{x * 1e9:9.4f} nV"


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host, self.cmd_port, self.pub_port = host, cmd_port, pub_port
        self.ctx = zmq.Context.instance()
        self.req = self._req()

    def _req(self):
        s = self.ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(f"tcp://{self.host}:{self.cmd_port}")
        return s

    def send(self, msg: dict) -> dict:
        try:
            self.req.send_json(msg)
            return self.req.recv_json()
        except zmq.Again:
            self.req.close(0)
            self.req = self._req()          # a timed-out REQ socket is stuck; rebuild
            return {"ok": False, "error": "no reply (is the service running?)"}

    @staticmethod
    def show_status(s: dict):
        live = s.get("live") or {}

        def pick(d, key, i):
            """d[key][i], or None if the list (or the value) is missing."""
            values = d.get(key) or [None, None]
            return values[i]

        def num(x, fmt):
            return "--" if x is None else format(x, fmt)

        for i in range(2):
            ref = pick(s, "reference", i) or "?"
            lock = " LOCK" if pick(s, "pll_locked", i) else ""
            print(f"  ch{i + 1} [{ref[:3]}{lock}]"
                  f"  f={num(pick(s, 'ref_freq_Hz', i), '.4f')} Hz"
                  f"  tc={num(pick(s, 'tc_s', i), '.4g')} s"
                  f"  order={pick(s, 'order', i)}"
                  f"  R={_v(pick(live, 'r', i))}"
                  f"  theta={num(pick(live, 'theta_deg', i), '+7.2f')} deg")
        aux = live.get("aux_in") or [None, None]
        print(f"  aux1={_v(aux[0])}  aux2={_v(aux[1])}  acq #{s.get('acq_id')}"
              f"{' (acquiring)' if s.get('acquiring') else ''}"
              f"{'  HW ERROR: ' + s['hw_error'] if s.get('hw_error') else ''}")

    def acquire(self):
        r = self.send({"cmd": "acquire"})
        if not r.get("ok"):
            print(r)
            return
        n = r["acq_id"]
        info = self.send({"cmd": "get_config"}).get("config", {})
        timeout = (info.get("acquisition") or {}).get("timeout_s", 120)
        deadline = time.monotonic() + timeout
        # Wait for OUR id with acquiring false -- never the flag alone, which
        # can still describe the previous acquisition.
        while time.monotonic() < deadline:
            st = self.send({"cmd": "status"}).get("status", {})
            if st.get("acq_id") == n and not st.get("acquiring"):
                smp = st.get("sample") or {}
                print(f"  sample #{n}: settle {smp.get('settle_s', 0) * 1e3:.2f} ms, "
                      f"{smp.get('n_avg')} pts averaged")
                for i in range(2):
                    print(f"    ch{i + 1}: X={_v(smp['x'][i])} Y={_v(smp['y'][i])} "
                          f"R={_v(smp['r'][i])} theta={smp['theta_deg'][i]:+7.2f} deg")
                print(f"    aux1={_v(smp['aux_in'][0])} aux2={_v(smp['aux_in'][1])}")
                return
            time.sleep(0.02)
        print(f"  acquisition #{n} timed out after {timeout} s")

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
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
                    elif topic == b"event":
                        print(f"  event [{d.get('level')}] {d.get('msg')}")
        except KeyboardInterrupt:
            print("  (stopped)")
        finally:
            sub.close(0)

    def run_line(self, line: str) -> bool:
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
            if cmd == "tc":
                print(self.send({"cmd": "set_time_constant", "channel": int(args[0]),
                                 "time_constant_s": _value(args[1:], _TC, "s")}))
            elif cmd == "order":
                print(self.send({"cmd": "set_order", "channel": int(args[0]),
                                 "order": int(args[1])}))
            elif cmd == "ref":
                print(self.send({"cmd": "set_reference", "channel": int(args[0]),
                                 "mode": args[1]}))
            elif cmd == "freq":
                print(self.send({"cmd": "set_frequency", "channel": int(args[0]),
                                 "frequency_Hz": _value(args[1:], _HZ, "hz")}))
            elif cmd == "acquire":
                self.acquire()
            elif cmd == "status":
                r = self.send({"cmd": "status"})
                self.show_status(r["status"]) if r.get("ok") else print(r)
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

    def close(self):
        self.req.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="External console for the HF2LI lock-in service")
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
        print(f"connected to tcp://{args.connect}:{args.cmd_port}   (type 'help' or 'quit')")
        while True:
            try:
                line = input("hf2> ")
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
