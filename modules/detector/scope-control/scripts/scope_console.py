"""A tiny external console for the oscilloscope service (real scope or simulator).

Raw protocol only (pyzmq + json, NO `scope` import): copy it anywhere.

    uv run scripts/scope_console.py                     # interactive, localhost
    uv run scripts/scope_console.py --connect 192.168.1.42
    uv run scripts/scope_console.py acquire              # one command, then exit

Commands (channel = 1 | 2)
    vdiv <ch> <V>          volts/division          offset <ch> <V>
    coupling <ch> dc|ac|gnd                         probe <ch> <x>
    on <ch> / off <ch>     trace on / off
    tdiv <s>               time/division           delay <s>
    trig ch1|ch2|ext|ext5|line   level <V>   slope rising|falling
    mode auto|normal|single|stop
    avg <n>                traces per average      points <n>
    lp <Hz> / hp <Hz>      zero-phase filter (0 = off)   order <n>
    unit <ch> <scale> <offset> <unit> [label]   e.g. unit 1 10 0 A Current
    restart                raw on|off
    sim <name> <value>     (simulator) e.g. sim ch2_phase_deg 90
    acquire                trigger, wait, print the numbers
    abort                  status     watch [s]     help     quit
Control: take / take! / release / clients
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
    taken elsewhere has no secure.py and talks plain; a secured scope will not
    answer it."""
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "scope" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("scope_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures scope."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "scope")

CMD_PORT = 5633
PUB_PORT = 5634
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "scope console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


def _f(v, fmt=".4f"):
    return "--" if v is None else format(v, fmt)


def _on(word: str) -> bool:
    return word.lower() in ("on", "1", "true", "yes")


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
                # scope may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches scope)
                flipped = (attempt == 1 and _SECURE is not None
                           and _SECURE.no_answer(self.host, "scope"))
                self.req = self._new_req()
                if not flipped:
                    return {"ok": False, "error": "service did not respond (timeout)"}

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
        ch = lambda i=0: "ch" + args[i].lower().removeprefix("ch")   # noqa: E731
        simple = {
            "vdiv": lambda: self.cmd(cmd="set_vdiv", channel=ch(), vdiv_V=num(1)),
            "offset": lambda: self.cmd(cmd="set_offset", channel=ch(), offset_V=num(1)),
            "coupling": lambda: self.cmd(cmd="set_coupling", channel=ch(), coupling=args[1]),
            "probe": lambda: self.cmd(cmd="set_probe", channel=ch(), probe=num(1)),
            "on": lambda: self.cmd(cmd="set_channel_enabled", channel=ch(), on=True),
            "off": lambda: self.cmd(cmd="set_channel_enabled", channel=ch(), on=False),
            "tdiv": lambda: self.cmd(cmd="set_tdiv", tdiv_s=num()),
            "delay": lambda: self.cmd(cmd="set_delay", delay_s=num()),
            "trig": lambda: self.cmd(cmd="set_trigger_source", source=args[0].lower()),
            "level": lambda: self.cmd(cmd="set_trigger_level", level_V=num()),
            "slope": lambda: self.cmd(cmd="set_trigger_slope", slope=args[0].lower()),
            "mode": lambda: self.cmd(cmd="set_trigger_mode", mode=args[0].lower()),
            "avg": lambda: self.cmd(cmd="set_averages", averages=int(num())),
            "points": lambda: self.cmd(cmd="set_points", points=int(num())),
            "lp": lambda: self.cmd(cmd="set_filter", lowpass_Hz=num()),
            "hp": lambda: self.cmd(cmd="set_filter", highpass_Hz=num()),
            "order": lambda: self.cmd(cmd="set_filter", order=int(num())),
            "unit": lambda: self.cmd(cmd="set_physical", channel=ch(), scale=num(1),
                                     offset=num(2), unit=args[3],
                                     **({"label": " ".join(args[4:])} if len(args) > 4 else {})),
            "restart": lambda: self.cmd(cmd="restart_average"),
            "raw": lambda: self.cmd(cmd="set_keep_raw", on=_on(args[0])),
            "sim": lambda: self.cmd(cmd="set_sim", name=args[0], value=num(1)),
            "abort": lambda: self.cmd(cmd="abort"),
            "take": lambda: self.cmd(cmd="take_control", force=False),
            "take!": lambda: self.cmd(cmd="take_control", force=True),
            "release": lambda: self.cmd(cmd="release_control"),
            "clients": lambda: json.dumps(self.cmd(cmd="clients"), indent=1),
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
            elif w == "watch":
                self.watch(float(args[0]) if args else 5.0)
            else:
                print(f"unknown command {w!r}; try help")
        except (IndexError, ValueError):
            print(f"bad arguments for {w!r}; try help")
        return True

    def acquire(self):
        r = self.cmd(cmd="acquire")
        if not r.get("ok"):
            print(r)
            return
        n = r["acq_id"]
        # Wait for THIS id to be finished: the status right after the trigger
        # may still be the frame from before it (gotcha #17).
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            st = self.cmd(cmd="status").get("status") or {}
            if st.get("acq_id") == n and not st.get("acquiring"):
                smp = st.get("sample") or {}
                if smp.get("aborted") or smp.get("error"):
                    print(f"#{n}: {'aborted' if smp.get('aborted') else smp.get('error')}")
                    return
                print(f"#{n}: {smp.get('averages')} traces"
                      + (f"  CLIPPED {smp['clipped']}" if smp.get("clipped") else ""))
                for c in ("ch1", "ch2"):
                    v = smp.get(c) or {}
                    if v:
                        print(f"  {c}: mean {_f(v.get('mean'))}  pp {_f(v.get('pk2pk'))}  "
                              f"f {_f(v.get('frequency'), '.4f')} Hz  ({st.get(c + '_unit')})")
                print(f"  phase CH2 - CH1: {_f(smp.get('phase_21_deg'), '.2f')} deg")
                return
            time.sleep(0.05)
        print(f"acquisition {n} did not finish")

    def watch(self, seconds: float):
        sub = self.ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        _secure(sub, self.host)                  # telemetry is encrypted too
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
                        lv = d.get("live") or {}
                        print(f"  {d.get('running_n')}/{d.get('averages')} averaged  "
                              f"trigger {_f(d.get('trigger_rate_Hz'), '.2f')} Hz  "
                              f"ch1 pp {_f((lv.get('ch1') or {}).get('pk2pk'))}  "
                              f"ch2 pp {_f((lv.get('ch2') or {}).get('pk2pk'))}  "
                              f"phase {_f(lv.get('phase_21_deg'), '.2f')} deg")
        finally:
            sub.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="console for the oscilloscope service")
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
    print(f"scope console -> {args.connect}:{args.cmd_port}   (help for commands)")
    while True:
        try:
            line = input("scope> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not con.run(line.split()):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
