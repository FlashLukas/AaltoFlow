"""A tiny external console for the spectrum-analyser service (real Signal Hound or simulator).

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `signalhound`
imports -- so this one file can be copied to any machine with pyzmq.

    uv run scripts/signalhound_console.py                     # interactive, localhost
    uv run scripts/signalhound_console.py --connect 192.168.1.42
    uv run scripts/signalhound_console.py acquire              # one command, then exit

Commands
    acquire                 trigger fresh sweeps, wait for them, print the peak
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
  the tracking generator -- normally driven by the shsg / shsna modules; here
  to test the owner side of their contract:
    tgcw on [GHz] [dBm]     TG CW on (missing values are kept)
    tgcw off                "off" = PARK (the TG44A has no off)
    tgsweep <GHz> <GHz> [n] one TG sweep start..stop, n averages; prints dB
    tgabort                 abort a TG sweep
    scene <name> <value>    simulator only, e.g. scene dut_inserted off,
                            scene tone_dBm -20, scene dut_bandwidth_Hz 20e6
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
  safety verbs (abort, tgabort) but change nothing until it takes control.
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
    taken elsewhere has no secure.py and talks plain; a secured signalhound will not
    answer it."""
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "signalhound" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("signalhound_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures signalhound."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "signalhound")

CMD_PORT = 5587
PUB_PORT = 5588
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "signalhound console",
            "host": f"{getpass.getuser()}@{socket.gethostname()}"}
HEARTBEAT_S = 2.0          # control.py: a holder silent for 10 s loses control


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
        s.setsockopt(zmq.SNDTIMEO, TIMEOUT_MS)  # a refused handshake must not block send
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
                # signalhound may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches signalhound)
                flipped = (attempt == 1 and _SECURE is not None
                           and _SECURE.no_answer(self.host, "signalhound"))
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
            "abort": lambda: self.cmd(cmd="abort"),
            "take": lambda: self.cmd(cmd="take_control", force=False),
            "take!": lambda: self.cmd(cmd="take_control", force=True),
            "release": lambda: self.cmd(cmd="release_control"),
            "clients": lambda: json.dumps(self.cmd(cmd="clients"), indent=1),
            "tgabort": lambda: self.cmd(cmd="tg_abort"),
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
            elif w == "tgcw":
                msg = {"cmd": "tg_cw", "on": _onoff(args[0])}
                if len(args) > 1:
                    msg["freq_hz"] = num(1) * 1e9
                if len(args) > 2:
                    msg["level_dbm"] = num(2)
                print(self.cmd(**msg))
            elif w == "tgsweep":
                self.tg_sweep(num(0) * 1e9, num(1) * 1e9, int(num(2)) if len(args) > 2 else 1)
            elif w == "watch":
                self.watch(float(args[0]) if args else 5.0)
            else:
                print(f"unknown command {w!r}; try help")
        except (IndexError, ValueError):
            print(f"bad arguments for {w!r}; try help")
        return True

    def tg_sweep(self, start, stop, averages):
        r = self.cmd(cmd="tg_sweep_acquire", start_hz=start, stop_hz=stop, averages=averages)
        if not r.get("ok"):
            print(r)
            return
        n = r["tg_acq_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            st = self.cmd(cmd="status").get("status") or {}
            # the id first, then the flag (suite gotcha #17)
            if st.get("tg_acq_id") == n and not st.get("tg_acquiring"):
                t = self.cmd(cmd="get_tg_trace", id=n)
                if not t.get("ok"):
                    print(t)
                    return
                y = [v for v in t["db"] if v is not None]
                print(f"TG sweep #{n}: {t['points']} points {_f(t['start_hz'], 1e9, '.6f')}-"
                      f"{_f(t['stop_hz'], 1e9, '.6f')} GHz, {min(y):.2f} ... {max(y):.2f} dB "
                      f"(relative to the TG output)" + ("  OVERLOAD" if t.get("overload") else ""))
                return
            time.sleep(0.05)
        print(f"TG sweep {n} did not finish")

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
                head = (f"#{n}: "
                        f"{t['points']} bins {_f(t['start_Hz'], 1e9, '.6f')}-"
                        f"{_f(t['stop_Hz'], 1e9, '.6f')} GHz, RBW {_f(t['rbw_Hz'], 1e3, 'g')} kHz, "
                        f"{t['averages']} avg")
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
        _secure(sub, self.host)                 # the status stream is encrypted too
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
                              f"TG {d.get('tg_mode')}  sweeps {d.get('sweeps')}")
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
    con.start_heartbeat()
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
