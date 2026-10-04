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

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                    take control if nobody has it
    take!                   take it over from whoever has it (they become a viewer)
    release                 give it back
    clients                 who holds control, who is connected
  While a GUI on another PC holds control, this console can read and use the
  safety verbs (abort) but change nothing until it takes control.
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
    taken elsewhere has no secure.py and talks plain; a secured shsna will not
    answer it."""
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src" / "shsna" / "secure.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("shsna_console_secure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


_SECURE = _load_secure()


def _secure(sock, host: str) -> None:
    """Make `sock` a CurveZMQ client when the lab's policy secures shsna."""
    if _SECURE is not None:
        _SECURE.secure_client(sock, host, "shsna")

CMD_PORT = 5627
PUB_PORT = 5628
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "shsna console",
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
                # shsna may run in the other mode than the policy now says
                # (started before it changed): try that mode once
                # (secure.no_answer; a wrong-mode request never reaches shsna)
                flipped = (attempt == 1 and _SECURE is not None
                           and _SECURE.no_answer(self.host, "shsna"))
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
            "start": lambda: self.cmd(cmd="set_start", start_Hz=num() * 1e6),
            "stop": lambda: self.cmd(cmd="set_stop", stop_Hz=num() * 1e6),
            "points": lambda: self.cmd(cmd="set_points", points=int(num())),
            "rbw": lambda: self.cmd(cmd="set_rbw", rbw_Hz=num() * 1e3),
            "avg": lambda: self.cmd(cmd="set_averages", averages=int(num())),
            "abort": lambda: self.cmd(cmd="abort"),
            "take": lambda: self.cmd(cmd="take_control", force=False),
            "take!": lambda: self.cmd(cmd="take_control", force=True),
            "release": lambda: self.cmd(cmd="release_control"),
            "clients": lambda: json.dumps(self.cmd(cmd="clients"), indent=1),
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
    con.start_heartbeat()
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
