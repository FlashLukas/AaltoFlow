"""A tiny external console for the spectrometer service (real CCS200 or simulator).

It speaks the raw wire protocol -- only `pyzmq` and `json`, NO `ccs200` imports --
so this one file can be copied to any machine with pyzmq.

    uv run scripts/ccs200_console.py                     # interactive, localhost
    uv run scripts/ccs200_console.py --connect 192.168.1.42
    uv run scripts/ccs200_console.py acquire              # one command, then exit

Commands
    acquire                 trigger fresh scans, wait for them, print the peak
    dark                    take a dark (block the light first!), wait for it
    cleardark               forget the dark
    abort                   cancel a running acquisition
    int <ms>                integration time
    avg <n>                 scans averaged per acquisition
    sub on|off              dark subtraction
    cont on|off             continuous scanning
    window <nm> <nm>        analysis window for peak / integrated intensity
    light on|off            SIMULATOR only: light on the input fibre
    sim <name> <value>      SIMULATOR only, e.g. sim line_level_per_s 120
                            (lamp_level_per_s lamp_temperature_K line_level_per_s
                             line_fwhm_nm dark_rate_per_s offset read_noise)
    status                  print one status snapshot
    watch [seconds]         stream peak / exposure from the status broadcast (default 5 s)
    help                    show this list
    quit / exit             leave

Control (who may change things -- docs/DEVELOPER_NOTES.md, "Control"):
    take                    take control if nobody has it
    take!                   take it over from whoever has it (they become a viewer)
    release                 give it back
    clients                 who holds control, who is connected
  While a GUI on another PC holds control, this console can read and use the
  safety verb (abort) but change nothing until it takes control.
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

CMD_PORT = 5603
PUB_PORT = 5604
TIMEOUT_MS = 3000

# Who this console is to the service (the same fields as control.py's
# make_identity, written out here because this file imports no package).
IDENTITY = {"id": uuid.uuid4().hex, "kind": "script", "name": "ccs200 console",
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
        s.setsockopt(zmq.LINGER, 0)
        s.connect(f"tcp://{self.host}:{self.cmd_port}")
        return s

    def cmd(self, **msg) -> dict:
        msg.setdefault("client", IDENTITY)      # say who we are (control)
        self.req.send_json(msg)
        try:
            return self.req.recv_json()
        except zmq.Again:
            self.req.close(0)
            self.req = self._new_req()
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
            "int": lambda: self.cmd(cmd="set_integration_time", integration_time_s=num() / 1e3),
            "avg": lambda: self.cmd(cmd="set_averages", averages=int(num())),
            "sub": lambda: self.cmd(cmd="set_dark_subtract", on=_on(args[0])),
            "cont": lambda: self.cmd(cmd="set_continuous", on=_on(args[0])),
            "window": lambda: self.cmd(cmd="set_window", min_nm=num(0), max_nm=num(1)),
            "light": lambda: self.cmd(cmd="set_light", on=_on(args[0])),
            "sim": lambda: self.cmd(cmd="set_sim", name=args[0], value=num(1)),
            "abort": lambda: self.cmd(cmd="abort"),
            "take": lambda: self.cmd(cmd="take_control", force=False),
            "take!": lambda: self.cmd(cmd="take_control", force=True),
            "release": lambda: self.cmd(cmd="release_control"),
            "clients": lambda: json.dumps(self.cmd(cmd="clients"), indent=1),
            "cleardark": lambda: self.cmd(cmd="clear_dark"),
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
            elif w == "dark":
                self.acquire(verb="take_dark")
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
        # Wait for THIS id to be finished: the status right after the trigger
        # may still be the frame from before it (gotcha #17).
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            st = self.cmd(cmd="status").get("status") or {}
            if st.get("acq_id") == n and not st.get("acquiring"):
                smp = st.get("sample") or {}
                if smp.get("aborted") or smp.get("error"):
                    print(f"#{n}: {'aborted' if smp.get('aborted') else smp.get('error')}")
                    return
                what = "dark" if verb != "acquire" else "spectrum"
                print(f"#{n} {what}: {smp.get('averages')} x {_f(smp.get('integration_time_s', 0) * 1e3, '.3f')} ms, "
                      f"peak {_f(smp.get('peak_nm'), '.3f')} nm = {_f(smp.get('peak_intensity'))} FS, "
                      f"integrated {_f(smp.get('integrated'))} FS nm"
                      f"{', dark subtracted' if smp.get('dark_applied') else ''}"
                      f"{'  SATURATED' if smp.get('saturated') else ''}")
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
                        print(f"  peak {_f(d.get('peak_nm'), '8.3f')} nm  "
                              f"{_f(d.get('peak_intensity'))} FS  exposure "
                              f"{_f(d.get('exposure'), '.3f')}  scans {d.get('scans')}"
                              f"{'  SATURATED' if d.get('saturated') else ''}")
        finally:
            sub.close(0)


def main() -> int:
    ap = argparse.ArgumentParser(description="console for the CCS200 spectrometer service")
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
    print(f"ccs200 console -> {args.connect}:{args.cmd_port}   (help for commands)")
    while True:
        try:
            line = input("ccs200> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not con.run(line.split()):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
