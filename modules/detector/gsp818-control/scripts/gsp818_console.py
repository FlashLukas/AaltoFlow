"""A tiny external console for the spectrum analyser service (GSP-818 or simulator).

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `gsp818` imports --
so this one file can be copied to any machine with pyzmq.

    uv run scripts/gsp818_console.py                     # interactive, localhost
    uv run scripts/gsp818_console.py --connect 192.168.1.42
    uv run scripts/gsp818_console.py acquire              # one command, then exit

Commands
    acquire                 trigger a fresh acquisition, wait for it, print the peak
    ref                     take a thru reference (tracking generator on!), wait for it
    clearref                forget the reference
    norm                    acquire and print the transmission (trace - reference) summary
    abort                   cancel a running acquisition
    start <MHz> / stop <MHz> / center <MHz> / span <MHz>
    points <n>              points per sweep
    rbw <kHz>|auto          resolution bandwidth (a value switches auto off)
    vbw <kHz>|auto          video bandwidth
    ref_level <dBm>         reference level (top of the screen)
    att <dB>|auto           input attenuation
    swt <s>|auto            sweep time
    det auto|normal|pos_peak|neg_peak|sample
    preamp on|off
    avg <n>                 sweeps power-averaged per acquisition
    cont on|off             continuous sweeping
    tg on|off               tracking generator output (off = the safety verb tg_off:
                            works also while a GUI has control)
    tglevel <dBm>           tracking generator level (-30 ... 0)
    dut thru|bandpass|lowpass|open     (simulator) the device under test
    status                  print one status snapshot
    watch [seconds]         stream peak / floor from the status broadcast (default 5 s)
    help                    show this list
    quit / exit             leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                    take control if nobody has it
    take!                   take it over from whoever has it (they become a viewer)
    release                 give it back
    clients                 who holds control, who is connected
  While a GUI on another PC holds control, this console can read and use the
  safety verbs (abort, tg off) but change nothing until it takes control.
"""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import sys
import threading
import time
import uuid

import zmq


def _load_secure():
    """The module's secure.py (encryption, README "Encryption and keys"),
    loaded straight from its file when this console sits in its module folder
    -- so the console still imports no package and runs anywhere. A copy
    taken elsewhere has no secure.py and talks plain; a secured gsp818 will
    not answer it."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "gsp818" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("gsp818_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures gsp818."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "gsp818")

CMD_PORT = 5585
PUB_PORT = 5586
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "gsp818 console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


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
        s.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)   # a refused handshake must not block send
        s.setsockopt(zmq.LINGER, 0)
        _secure(s, self.host)
        s.connect(f"tcp://{self.host}:{self.cmd_port}")
        return s

    def cmd(self, **msg) -> dict:
        msg.setdefault("client", IDENTITY)      # say who we are (control)
        for attempt in (1, 2):
            try:
                self.req.send_json(msg)
                return self.req.recv_json()
            except zmq.Again:
                self.req.close(0)
                # gsp818 may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches gsp818)
                flipped = (attempt == 1 and _SECURE is not None
                           and _SECURE.no_answer(self.host, "gsp818"))
                self.req = self._new_req()
                if not flipped:
                    return {"ok": False, "error": "service did not respond (timeout)"}

    def _value_or_auto(self, args, verb, arg, auto_verb, scale=1.0):
        """'rbw auto' -> set_rbw_auto on; 'rbw 30' -> set_rbw 30e3."""
        if args[0].lower() == "auto":
            return self.cmd(cmd=auto_verb, on=True)
        return self.cmd(**{"cmd": verb, arg: float(args[0]) * scale})

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

    def run(self, words: list[str]) -> bool:
        """Execute one command line. Returns False to quit."""
        if not words:
            return True
        w, args = words[0].lower(), words[1:]
        num = lambda i=0: float(args[i])          # noqa: E731
        onoff = lambda: args[0].lower() == "on"   # noqa: E731
        simple = {
            "start": lambda: self.cmd(cmd="set_start", start_Hz=num() * 1e6),
            "stop": lambda: self.cmd(cmd="set_stop", stop_Hz=num() * 1e6),
            "center": lambda: self.cmd(cmd="set_center", center_Hz=num() * 1e6),
            "span": lambda: self.cmd(cmd="set_span", span_Hz=num() * 1e6),
            "points": lambda: self.cmd(cmd="set_points", points=int(num())),
            "rbw": lambda: self._value_or_auto(args, "set_rbw", "rbw_Hz", "set_rbw_auto", 1e3),
            "vbw": lambda: self._value_or_auto(args, "set_vbw", "vbw_Hz", "set_vbw_auto", 1e3),
            "att": lambda: self._value_or_auto(args, "set_atten", "atten_dB", "set_atten_auto"),
            "swt": lambda: self._value_or_auto(args, "set_sweep_time", "sweep_time_s",
                                               "set_sweep_time_auto"),
            "ref_level": lambda: self.cmd(cmd="set_ref_level", ref_level_dBm=num()),
            "det": lambda: self.cmd(cmd="set_detector", detector=args[0].lower()),
            "preamp": lambda: self.cmd(cmd="set_preamp", on=onoff()),
            "avg": lambda: self.cmd(cmd="set_averages", averages=int(num())),
            "cont": lambda: self.cmd(cmd="set_continuous", on=onoff()),
            # "tg off" is the SAFETY verb tg_off: it works also while a GUI on
            # another PC has control ("tg on" does not)
            "tg": lambda: self.cmd(cmd="set_tg", on=True) if onoff() else self.cmd(cmd="tg_off"),
            "tglevel": lambda: self.cmd(cmd="set_tg_level", level_dBm=num()),
            "dut": lambda: self.cmd(cmd="set_dut", dut=args[0].lower()),
            "abort": lambda: self.cmd(cmd="abort"),
            "take": lambda: self.cmd(cmd="take_control", force=False),
            "take!": lambda: self.cmd(cmd="take_control", force=True),
            "release": lambda: self.cmd(cmd="release_control"),
            "clients": lambda: json.dumps(self.cmd(cmd="clients"), indent=1),
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
            elif w == "norm":
                self.acquire(quantity="norm")
            elif w == "watch":
                self.watch(float(args[0]) if args else 5.0)
            else:
                print(f"unknown command {w!r}; try help")
        except (IndexError, ValueError):
            print(f"bad arguments for {w!r}; try help")
        return True

    def acquire(self, verb="acquire", quantity="power"):
        r = self.cmd(cmd=verb)
        if not r.get("ok"):
            print(r)
            return
        n = r["acq_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            st = self.cmd(cmd="status").get("status") or {}
            # the id BEFORE the flag: right after the trigger the status can
            # still be the previous acquisition's "not acquiring" (gotcha #17)
            if st.get("acq_id") == n and not st.get("acquiring"):
                t = self.cmd(cmd="get_trace", which="sample", quantity=quantity)
                if not t.get("ok"):
                    print(t.get("error", t))
                    return
                what = " (reference)" if verb != "acquire" else ""
                head = (f"#{n}{what}: {t['points']} points {_f(t['start_Hz'], 1e6, '.3f')}-"
                        f"{_f(t['stop_Hz'], 1e6, '.3f')} MHz, RBW {_f(t['rbw_Hz'], 1e3, '.3g')} kHz")
                if quantity == "norm":
                    y = [v for v in t["norm_dB"] if v is not None]
                    print(f"{head}, transmission max {max(y):.2f} dB, min {min(y):.2f} dB")
                else:
                    print(f"{head}, peak {_f(t['peak_Hz'], 1e6)} MHz at "
                          f"{_f(t['peak_dBm'], 1, '.2f')} dBm, floor {_f(t['floor_dBm'], 1, '.1f')} dBm"
                          f"{'  OVERLOAD' if t.get('overload') else ''}")
                return
            time.sleep(0.05)
        print(f"acquisition {n} did not finish")

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        _secure(sub, self.host)                   # telemetry is encrypted too
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
                        print(f"  peak {_f(d.get('peak_Hz'), 1e6)} MHz "
                              f"{_f(d.get('peak_dBm'), 1, '.2f')} dBm  floor "
                              f"{_f(d.get('floor_dBm'), 1, '.1f')} dBm  TG "
                              f"{'on' if d.get('tg_on') else 'off'}  sweeps {d.get('sweeps')}")
        finally:
            sub.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="console for the spectrum analyser service")
    ap.add_argument("--connect", default="localhost", metavar="HOST")
    ap.add_argument("--cmd-port", type=int, default=CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=PUB_PORT)
    ap.add_argument("words", nargs="*", help="one command to run, then exit")
    args = ap.parse_args()
    con = Console(args.connect, args.cmd_port, args.pub_port)
    if args.words:
        con.run(args.words)
        return 0
    con.start_heartbeat()
    print(f"gsp818 console -> {args.connect}:{args.cmd_port}   (help for commands)")
    while True:
        try:
            line = input("gsp818> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not con.run(line.split()):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
