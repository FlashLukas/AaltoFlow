"""
mock_instrument.py
==================

A *stand-in* for one of your real instrument modules (think meqi / the magnet),
built to the exact same wire protocol as the module blueprint:

    - ZeroMQ REQ/REP, JSON commands, every reply {"ok": bool, ...}
    - FIRE-AND-FORGET: `set_field` returns {"ok": true, "queued": true}
      immediately; the field then *ramps* toward the setpoint at a finite
      slew rate in a background thread. You poll `status` to learn when it
      has actually settled.
    - A read-only "detector" (`read_signal`) whose value depends on the
      current field, so a sweep produces a real curve.

The whole point of this file is that it behaves like your hardware server WITHOUT
any hardware: the field cannot teleport to its setpoint, so "wait until all driven
parameters are set, THEN grab a data point" becomes a real, testable problem.

Run standalone (for poking with a console):  python mock_instrument.py --port 5555
Normally it is launched as a subprocess by run_sweep.py.
"""

import argparse
import json
import math
import threading
import time

import zmq

# --- physics of the fake instrument -----------------------------------------
SLEW_RATE_MT_PER_S = 80.0   # how fast the "magnet" can change field (mT/s)
SETTLE_TOL_MT = 0.05        # |field - setpoint| below this counts as "there"
SETTLE_DWELL_S = 0.05       # ...and it must stay there this long to be "settled"
SATURATION_MT = 30.0        # detector saturates around this field


class FakeField:
    """The mutable state of the instrument, guarded by a lock.

    A background thread nudges `current` toward `setpoint` every tick, exactly
    like a magnet power supply ramping. `settled` only becomes True once the
    field has been within tolerance of the setpoint continuously for a dwell.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.current = 0.0        # mT, where the field actually is right now
        self.setpoint = 0.0       # mT, where we've been told to go
        self._in_tol_since = None  # timestamp we first entered tolerance
        self.running = True

    def set_setpoint(self, value):
        with self.lock:
            self.setpoint = float(value)
            self._in_tol_since = None  # a new target => not settled anymore

    def tick(self, dt):
        """Advance the ramp by dt seconds."""
        with self.lock:
            err = self.setpoint - self.current
            max_step = SLEW_RATE_MT_PER_S * dt
            if abs(err) <= max_step:
                self.current = self.setpoint      # arrived this tick
            else:
                self.current += math.copysign(max_step, err)  # keep ramping

            # settle bookkeeping
            if abs(self.setpoint - self.current) <= SETTLE_TOL_MT:
                if self._in_tol_since is None:
                    self._in_tol_since = time.monotonic()
            else:
                self._in_tol_since = None

    def snapshot(self):
        """Return a status dict, like your real `status` command."""
        with self.lock:
            settled = (
                self._in_tol_since is not None
                and (time.monotonic() - self._in_tol_since) >= SETTLE_DWELL_S
            )
            return {
                "field_mT": round(self.current, 4),
                "setpoint_mT": round(self.setpoint, 4),
                "settled": settled,
            }

    def read_detector(self):
        """A fake 'Kerr' detector: saturating S-curve of the CURRENT field.

        Note it reads `current`, not `setpoint` -- so if you read before the
        field has settled, you get a wrong (mid-ramp) value. That is precisely
        why the sync layer must wait for `settled` first.
        """
        with self.lock:
            x = self.current / SATURATION_MT
        # a smooth deterministic signal; no RNG so results are reproducible
        return round(math.tanh(x) + 0.02 * math.sin(self.current), 6)


def ramp_thread(state: FakeField):
    last = time.monotonic()
    while state.running:
        now = time.monotonic()
        state.tick(now - last)
        last = now
        time.sleep(0.005)  # 200 Hz internal ramp update


def serve(port: int):
    state = FakeField()
    t = threading.Thread(target=ramp_thread, args=(state,), daemon=True)
    t.start()

    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REP)
    sock.bind(f"tcp://0.0.0.0:{port}")
    print(f"[mock_instrument] listening on tcp://0.0.0.0:{port}", flush=True)

    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)

    while True:
        if not dict(poller.poll(200)):
            continue  # 200 ms poll; loop stays alive even with no traffic
        msg = sock.recv_json()
        cmd = msg.get("cmd")

        if cmd == "set_field":
            state.set_setpoint(msg["value"])
            sock.send_json({"ok": True, "queued": True})       # fire-and-forget
        elif cmd == "status":
            sock.send_json({"ok": True, **state.snapshot()})
        elif cmd == "read_signal":
            sock.send_json({"ok": True, "signal": state.read_detector()})
        elif cmd == "ping":
            sock.send_json({"ok": True})
        else:
            sock.send_json({"ok": False, "error": f"unknown cmd {cmd!r}"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5555)
    args = ap.parse_args()
    serve(args.port)
