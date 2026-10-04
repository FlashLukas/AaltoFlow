"""A tiny external console for the 7230 lock-in service.

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `sr7230` import --
so this one file can be copied to any machine with pyzmq.

Start the service first:
    uv run scripts/run_service.py

Then:
    uv run scripts/sr7230_console.py                      # interactive, localhost
    uv run scripts/sr7230_console.py --connect 192.168.1.42
        sr7230> tc 30 ms
        sr7230> sens 10 mV
        sr7230> ref int
        sr7230> freq 1.2345 kHz
        sr7230> auto measure
        sr7230> acquire
        sr7230> quit

or one command and exit:
    uv run scripts/sr7230_console.py acquire

Commands
    ref int|ttl|analog             reference source
    freq <value> [Hz|kHz]          oscillator frequency
    amp <volts>                    OSC OUT amplitude, V rms (drives the load!)
    off                            OSC OUT to 0 V (the safety verb output_off:
                                   works also while a GUI has control)
    phase <deg>                    reference phase
    harm <n>                       detect at harmonic n
    input A|-B|A-B|ground|"I high-BW"|"I low-noise"
    coupling AC|DC
    sens <label>|<index>           full-scale sensitivity, e.g. "sens 10 mV" or "sens 21"
    tc <value> [us|ms|s]           time constant (snapped to the 1-2-5 table)
    slope 6|12|18|24               filter slope, dB/oct
    fast on|off                    fast mode
    auto phase|sens|measure        auto-phase / auto-sensitivity / auto-measure (waits)
    acquire                        settle, latch and print one sample (waits)
    status                         print one status snapshot
    info                           static info (ranges, tables)
    watch [seconds]                stream the live status broadcast (default 5 s)
    help                           show this list
    quit / exit                    leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                   take control if nobody has it
    take!                  take it over from whoever has it (they become a viewer)
    release                give it back
    clients                who holds control, who is connected
  While a GUI on another PC holds control, this console can read and switch the
  oscillator off ('off') but change nothing else until it takes control.
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

CMD_PORT = 5621
PUB_PORT = 5622
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "sr7230 console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control

_TC = {"us": 1e-6, "ms": 1e-3, "s": 1.0}
_HZ = {"hz": 1.0, "khz": 1e3}
_AUTO = {"phase": "auto_phase", "sens": "auto_sensitivity",
         "sensitivity": "auto_sensitivity", "measure": "auto_measure"}


def _value(tokens, units, default_unit):
    v = float(tokens[0])
    unit = tokens[1].lower() if len(tokens) > 1 else default_unit
    if unit not in units:
        raise ValueError(f"unknown unit {unit!r} (use {'/'.join(units)})")
    return v * units[unit]


def _si(x, unit="V"):
    """Human-scaled, ASCII only (this prints to pipes too)."""
    if x is None:
        return "--"
    a = abs(x)
    for s, p in ((1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if a >= s:
            return f"{x / s:9.4f} {p}{unit}"
    return f"{x * 1e15:9.4f} f{unit}"


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
        msg.setdefault("client", IDENTITY)       # say who we are (control)
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
        u = s.get("unit", "V")
        ovl = s.get("overload") or {}
        lock = {None: "", True: " LOCKED", False: " UNLOCKED"}.get(s.get("ref_locked"), "")
        tc = s.get("tc_s")
        print(f"  ref {s.get('ref_source')}{lock}  f={s.get('ref_freq_Hz')} Hz  "
              f"n={s.get('harmonic')}  phase={s.get('phase_deg')} deg  "
              f"osc {s.get('amplitude_V')} V")
        print(f"  {s.get('input')} {s.get('coupling')}  sens {s.get('sensitivity')}  "
              f"tc {'--' if tc is None else format(tc, '.4g')} s  {s.get('slope')}"
              f"{'  FAST' if s.get('fast_mode') else ''}")
        th = live.get("theta_deg")
        print(f"  R={_si(live.get('r'), u)}  theta={'--' if th is None else format(th, '+7.2f')} deg"
              f"  X={_si(live.get('x'), u)}  Y={_si(live.get('y'), u)}"
              f"{'  OVERLOAD' if ovl.get('input') or ovl.get('output') else ''}")
        adc = live.get("adc") or [None, None]
        print(f"  adc1={_si(adc[0])}  adc2={_si(adc[1])}  acq #{s.get('acq_id')}"
              f"{' (acquiring)' if s.get('acquiring') else ''}"
              f"{'  HW ERROR: ' + s['hw_error'] if s.get('hw_error') else ''}")

    def _wait(self, id_key, busy_key, n, timeout):
        """Wait for OUR id with busy false -- never the flag alone, which can
        still describe the previous operation."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.send({"cmd": "status"}).get("status", {})
            if st.get(id_key) == n and not st.get(busy_key):
                return st
            time.sleep(0.02)
        return None

    def acquire(self):
        r = self.send({"cmd": "acquire"})
        if not r.get("ok"):
            print(r)
            return
        n = r["acq_id"]
        cfg = self.send({"cmd": "get_config"}).get("config", {})
        timeout = (cfg.get("acquisition") or {}).get("timeout_s", 120)
        st = self._wait("acq_id", "acquiring", n, timeout)
        if st is None:
            print(f"  acquisition #{n} timed out after {timeout} s")
            return
        smp = st.get("sample") or {}
        u = smp.get("unit", "V")
        print(f"  sample #{n}: settle {smp.get('settle_s', 0) * 1e3:.2f} ms, "
              f"{smp.get('n_avg')} pts averaged"
              f"{'  OVERLOADED' if smp.get('overload') else ''}"
              f"{'  UNLOCKED' if smp.get('ref_locked') is False else ''}")
        print(f"    X={_si(smp.get('x'), u)} Y={_si(smp.get('y'), u)} "
              f"R={_si(smp.get('r'), u)} theta={smp.get('theta_deg', 0):+7.2f} deg")
        adc = smp.get("adc") or [None, None]
        print(f"    adc1={_si(adc[0])} adc2={_si(adc[1])}")

    def auto(self, which):
        verb = _AUTO.get(which)
        if verb is None:
            print(f"  auto what? ({', '.join(_AUTO)})")
            return
        r = self.send({"cmd": verb})
        if not r.get("ok"):
            print(r)
            return
        st = self._wait("auto_id", "auto_busy", r["auto_id"], 300)
        if st is None:
            print("  timed out")
        else:
            print(f"  {verb} #{r['auto_id']}: {st.get('auto_error') or 'done'} -- "
                  f"sens {st.get('sensitivity')}, phase {st.get('phase_deg')} deg")

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
        rest = " ".join(args).strip('"')
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd in ("help", "?"):
            print(__doc__.split("Commands")[1])
            return True
        try:
            if cmd in ("take", "take!"):
                print(self.send({"cmd": "take_control", "force": cmd == "take!"}))
            elif cmd == "release":
                print(self.send({"cmd": "release_control"}))
            elif cmd == "clients":
                print("  " + json.dumps(self.send({"cmd": "clients"}), indent=2).replace("\n", "\n  "))
            elif cmd == "ref":
                print(self.send({"cmd": "set_reference", "source": args[0]}))
            elif cmd == "freq":
                print(self.send({"cmd": "set_frequency",
                                 "frequency_Hz": _value(args, _HZ, "hz")}))
            elif cmd == "off":
                # the safety verb, allowed also while a GUI has control
                print(self.send({"cmd": "output_off"}))
            elif cmd == "amp":
                print(self.send({"cmd": "set_amplitude", "amplitude_V": float(args[0])}))
            elif cmd == "phase":
                print(self.send({"cmd": "set_phase", "phase_deg": float(args[0])}))
            elif cmd == "harm":
                print(self.send({"cmd": "set_harmonic", "harmonic": int(args[0])}))
            elif cmd == "input":
                print(self.send({"cmd": "set_input", "mode": rest}))
            elif cmd == "coupling":
                print(self.send({"cmd": "set_coupling", "coupling": args[0]}))
            elif cmd == "sens":
                value = int(rest) if rest.isdigit() else rest
                print(self.send({"cmd": "set_sensitivity", "sensitivity": value}))
            elif cmd == "tc":
                print(self.send({"cmd": "set_time_constant",
                                 "time_constant_s": _value(args, _TC, "s")}))
            elif cmd == "slope":
                print(self.send({"cmd": "set_slope", "slope": int(args[0])}))
            elif cmd == "fast":
                print(self.send({"cmd": "set_fast_mode",
                                 "enabled": args[0].lower() in ("on", "1", "true")}))
            elif cmd == "auto":
                self.auto(args[0].lower())
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

    def start_heartbeat(self):
        """"Still here" in the background, on its OWN socket (a ZeroMQ socket
        belongs to one thread): while you think, control stays yours."""
        self._hb_stop = threading.Event()

        def beat():
            def make():
                s = self.ctx.socket(zmq.REQ)
                s.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
                s.setsockopt(zmq.LINGER, 0)
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
    ap = argparse.ArgumentParser(description="External console for the 7230 lock-in service")
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
        con.start_heartbeat()            # interactive: keep control while you think
        print(f"connected to tcp://{args.connect}:{args.cmd_port}   (type 'help' or 'quit')")
        while True:
            try:
                line = input("sr7230> ")
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
