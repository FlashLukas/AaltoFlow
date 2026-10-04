"""A tiny external console for the SR830 lock-in service.

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `sr830` import --
so this one file can be copied to any machine with pyzmq.

Start the service first:
    uv run scripts/run_service.py

Then:
    uv run scripts/sr830_console.py                      # interactive, localhost
    uv run scripts/sr830_console.py --connect 192.168.1.42
        sr830> tc 30 ms
        sr830> sens 10 mV
        sr830> ref int
        sr830> freq 1.2345 kHz
        sr830> autogain
        sr830> acquire
        sr830> quit

or one command and exit:
    uv run scripts/sr830_console.py acquire

Commands
    ref int|ext                    reference source
    freq <value> [Hz|kHz]          internal frequency
    harm <n>                       detection harmonic
    phase <deg>                    reference phase
    sine <volts>                   SINE OUT amplitude (Vrms, 0.004 .. 5)
    tc <value> [us|ms|s|ks]        time constant (snapped to the SR830's steps)
    sens <label>                   sensitivity, e.g. 10 mV, 500 uV, 2 nA
    slope 6|12|18|24               filter slope in dB/oct
    reserve high|normal|low_noise  dynamic reserve
    input A|A-B|I1M|I100M          input source
    sync on|off                    synchronous filter
    aux <1..4> <volts>             AUX OUT voltage
    off                            SINE OUT to minimum, every AUX OUT to 0 V (the
                                   safety verb output_off: works also while a
                                   GUI has control)
    autogain | autophase | autoreserve   run an auto function and wait for it
    acquire                        settle, latch and print one sample (waits)
    status                         print one status snapshot
    info                           static info (limits, the discrete steps)
    watch [seconds]                stream the live status broadcast (default 5 s)
    help                           show this list
    quit / exit                    leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                   take control if nobody has it
    take!                  take it over from whoever has it (they become a viewer)
    release                give it back
    clients                who holds control, who is connected
  While a GUI on another PC holds control, this console can read and take the
  outputs off ('off') but change nothing else until it takes control.
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
    taken elsewhere has no secure.py and talks plain; a secured sr830 will not
    answer it."""
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "sr830" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("sr830_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures sr830."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "sr830")


CMD_PORT = 5599
PUB_PORT = 5600
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "sr830 console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control

_TC = {"us": 1e-6, "ms": 1e-3, "s": 1.0, "ks": 1e3}
_HZ = {"hz": 1.0, "khz": 1e3}


def _value(tokens, units, default_unit):
    v = float(tokens[0])
    unit = tokens[1].lower() if len(tokens) > 1 else default_unit
    if unit not in units:
        raise ValueError(f"unknown unit {unit!r} (use {'/'.join(units)})")
    return v * units[unit]


def _eng(x, unit):
    """Human-scaled, ASCII only (this prints to pipes too, gotcha #14)."""
    if x is None:
        return "--"
    a = abs(x)
    for s, p in ((1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if a >= s:
            return f"{x / s:9.4f} {p}{unit}"
    return f"{x / 1e-15:9.4f} f{unit}"


class Console:
    def __init__(self, host, cmd_port, pub_port):
        self.host, self.cmd_port, self.pub_port = host, cmd_port, pub_port
        self.ctx = zmq.Context.instance()
        self.req = self._req()

    def _req(self):
        s = self.ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, TIMEOUT_MS)
        s.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)    # a refused handshake must not block send
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
                # timed out -> the REQ socket is stuck; rebuild it
                self.req.close(0)
                # the service may run in the other mode than the policy now
                # says (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches it)
                flipped = (attempt == 1 and _SECURE is not None
                           and _SECURE.no_answer(self.host, "sr830"))
                self.req = self._req()
                if not flipped:
                    return {"ok": False, "error": "no reply (is the service running?)"}

    @staticmethod
    def show_status(s: dict):
        live = s.get("live") or {}
        u = s.get("unit", "V")
        ovl = s.get("overload") or {}
        flags = [k.upper() for k, v in ovl.items() if v]
        if s.get("unlocked"):
            flags.append("UNLOCK")
        th = live.get("theta_deg")
        print(f"  ref {s.get('reference_source')}  f={s.get('ref_freq_Hz')} Hz"
              f"  harm {s.get('harmonic')}  phase {s.get('phase_deg')} deg"
              f"  sine {s.get('sine_out_V')} V")
        print(f"  sens {s.get('sensitivity')}  tc {s.get('time_constant')}"
              f"  {s.get('slope')}  reserve {s.get('reserve')}"
              f"  input {s.get('input_source')}")
        print(f"  X={_eng(live.get('x'), u)}  Y={_eng(live.get('y'), u)}"
              f"  R={_eng(live.get('r'), u)}"
              f"  theta={'--' if th is None else format(th, '+7.2f')} deg"
              f"  {'OVERLOAD ' + ','.join(flags) if flags else ''}")
        aux = live.get("aux_in") or [None] * 4
        print("  aux in " + "  ".join(f"{k + 1}:{'--' if v is None else format(v, '+.4f')}"
                                      for k, v in enumerate(aux))
              + f"   acq #{s.get('acq_id')}{' (acquiring)' if s.get('acquiring') else ''}"
              + (f"  HW ERROR: {s['hw_error']}" if s.get("hw_error") else ""))

    def _wait_status(self, pred, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.send({"cmd": "status"}).get("status", {})
            if pred(st):
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
        # Wait for OUR id with acquiring false -- never the flag alone, which
        # can still describe the previous acquisition.
        st = self._wait_status(lambda s: s.get("acq_id") == n and not s.get("acquiring"),
                               timeout)
        if st is None:
            print(f"  acquisition #{n} timed out after {timeout} s")
            return
        smp = st.get("sample") or {}
        u = smp.get("unit", "V")
        print(f"  sample #{n}: settle {smp.get('settle_s', 0) * 1e3:.2f} ms, "
              f"{smp.get('n_avg')} pts, overload {smp.get('overload')}")
        print(f"    X={_eng(smp['x'], u)} Y={_eng(smp['y'], u)} R={_eng(smp['r'], u)} "
              f"theta={smp['theta_deg']:+7.2f} deg  f={smp.get('freq_Hz')} Hz")
        print("    aux " + "  ".join(f"{v:+.4f}" for v in smp.get("aux_in", [])))

    def auto(self, verb):
        r = self.send({"cmd": verb})
        if not r.get("ok"):
            print(r)
            return
        n = r["auto_id"]
        st = self._wait_status(lambda s: s.get("auto_id") == n and not s.get("auto_busy"), 120)
        print(f"  {verb} #{n}: " + ("timed out" if st is None else st.get("auto_note", "done")))

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        _secure(sub, self.host)
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
        simple = {  # console word -> (verb, argument key, converter)
            "ref": ("set_reference_source", "source",
                    lambda a: {"int": "internal", "ext": "external"}.get(a[0], a[0])),
            "harm": ("set_harmonic", "harmonic", lambda a: int(a[0])),
            "phase": ("set_phase", "phase_deg", lambda a: float(a[0])),
            "sine": ("set_sine_out", "sine_out_V", lambda a: float(a[0])),
            "sens": ("set_sensitivity", "sensitivity", lambda a: " ".join(a)),
            "slope": ("set_slope", "slope", lambda a: f"{int(a[0])} dB/oct"),
            "reserve": ("set_reserve", "reserve", lambda a: a[0]),
            "input": ("set_input_source", "source", lambda a: a[0]),
            "sync": ("set_sync_filter", "enabled", lambda a: a[0].lower() in ("on", "1", "true")),
            "freq": ("set_frequency", "frequency_Hz", lambda a: _value(a, _HZ, "hz")),
            "tc": ("set_time_constant", "time_constant", lambda a: _value(a, _TC, "s")),
        }
        try:
            if cmd in ("take", "take!"):
                print(self.send({"cmd": "take_control", "force": cmd == "take!"}))
            elif cmd == "release":
                print(self.send({"cmd": "release_control"}))
            elif cmd == "clients":
                print("  " + json.dumps(self.send({"cmd": "clients"}), indent=2).replace("\n", "\n  "))
            elif cmd in simple:
                verb, key, conv = simple[cmd]
                print(self.send({"cmd": verb, key: conv(args)}))
            elif cmd == "off":
                # the safety verb, allowed also while a GUI has control
                print(self.send({"cmd": "output_off"}))
            elif cmd == "aux":
                print(self.send({"cmd": "set_aux_out", "channel": int(args[0]),
                                 "volts": float(args[1])}))
            elif cmd in ("autogain", "autophase", "autoreserve"):
                self.auto("auto_" + cmd[4:])
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
                s.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)    # a refused handshake must not block send
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
    ap = argparse.ArgumentParser(description="External console for the SR830 lock-in service")
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
                line = input("sr830> ")
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
