"""Self-test of an Analog Discovery through the running scope service: its own
generator, looped back to its own scope (lab bench, Lukas 2026-10-08):

    W1 -> scope input 1 (CH1)        W2 -> scope input 2 (CH2)

    uv run scripts/ad_selftest.py                       # the service on this PC
    uv run scripts/ad_selftest.py --connect HOST --cmd-port 5633 --pub-port 5634

What it does, in order (nothing else is touched -- the supplies stay as they are):
  1. ZERO: both outputs off (they sit at 0 V, a low-impedance source, so each
     DC-coupled input sees a defined 0 V); the mean of each channel is its
     zero reference. Reported, and subtracted from every offset below.
  2. W1 alone, sine: at a few frequencies and amplitudes, CH1's amplitude,
     frequency and offset against what W1 was asked for.
  3. W1 and W2 at the same frequency, W2 phase-shifted (W2 follows W1 with a
     phase offset): CH2's amplitude and the phase CH2 - CH1.
  4. Both outputs OFF again (also when something fails or Ctrl+C).
Prints one line per check and PASS / FAIL with the tolerance; exit code 0 =
all passed. ASCII only (gotcha #14).

The script takes control of the service (a GUI elsewhere becomes a viewer
while it runs).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))

from scope.net.client import ScopeClient                      # noqa: E402
from scope.net.protocol import DEFAULT_CMD_PORT, DEFAULT_PUB_PORT  # noqa: E402

# what is checked, and how close it must be
TOL_AMPLITUDE = 0.03          # relative
TOL_FREQUENCY = 0.005         # relative
TOL_OFFSET_V = 0.02           # absolute, after the zero reference
TOL_PHASE_DEG = 2.0
CASES = [(100.0, 0.5), (1000.0, 1.0), (10000.0, 2.0)]   # (Hz, Vpp) for W1 alone
PHASE_CASE = (1000.0, 1.0, 0.5, 90.0)                    # Hz, W1 Vpp, W2 Vpp, W2 - W1 deg


class Checker:
    def __init__(self):
        self.failed = 0

    def check(self, what: str, got: float, want: float, tol: float, rel: bool) -> None:
        err = abs(got - want) / abs(want) if rel and want else abs(got - want)
        ok = err <= tol
        self.failed += 0 if ok else 1
        lim = f"{tol * 100:g} %" if rel else f"{tol:g}"
        print(f"  {'PASS' if ok else 'FAIL'}  {what:<28} got {got:10.5g}   want {want:10.5g}"
              f"   (tolerance {lim})")


def wait(cli, pred, what: str, timeout: float = 15.0) -> dict:
    t_end = time.monotonic() + timeout
    st = {}
    while time.monotonic() < t_end:
        st = cli._cmd({"cmd": "status"}).get("status", {})
        if st and pred(st):
            return st
        time.sleep(0.05)
    raise TimeoutError(f"{what}: not reached within {timeout:g} s")


def ok(r: dict, what: str) -> None:
    if not r.get("ok", False):
        raise RuntimeError(f"{what}: {r.get('error', 'refused')}")


def measure(cli, averages: int = 16) -> dict:
    """One averaged acquisition; returns {"ch1": values, "ch2": values, "phase_21_deg"}."""
    ok(cli.set_averages(averages), "averages")
    wait(cli, lambda s: s.get("averages") == averages, "averages")
    tr = cli.acquire_blocking(timeout_s=60)
    return {"ch1": tr.get("ch1_values") or {}, "ch2": tr.get("ch2_values") or {},
            "phase_21_deg": tr.get("phase_21_deg")}


def setup_timebase(cli, hz: float) -> None:
    """About 10 periods in the record, triggered by W1 starting a period."""
    ok(cli.set_tdiv(1.0 / hz), "time/div")
    ok(cli.set_trigger_source("w1"), "trigger source")
    ok(cli.set_trigger_mode("normal"), "trigger mode")
    wait(cli, lambda s: s.get("settings_settled") and s.get("trigger_source") == "w1",
         "scope settings")


def main() -> int:
    ap = argparse.ArgumentParser(description="Analog Discovery loopback self-test")
    ap.add_argument("--connect", default="localhost")
    ap.add_argument("--cmd-port", type=int, default=DEFAULT_CMD_PORT)
    ap.add_argument("--pub-port", type=int, default=DEFAULT_PUB_PORT)
    args = ap.parse_args()

    cli = ScopeClient(host=args.connect, cmd_port=args.cmd_port, pub_port=args.pub_port,
                      timeout_ms=5000, kind="script", name="AD self-test")
    info = cli.start()
    print(f"connected: {info.get('idn') or info.get('model')}")
    if cli.gen is None:
        print("this instrument has no generator: nothing to test")
        return 2
    cli.take_control()
    g = cli.gen
    c = Checker()
    try:
        # ---- 1. the zero reference ---------------------------------------------------
        print("\n1. zero reference (both outputs off = 0 V)")
        ok(g.outputs_off(), "outputs off")
        wait(cli, lambda s: s.get("gen_all_off"), "outputs off")
        ok(g.set_follow(False), "follow off")
        setup_timebase(cli, 1000.0)
        ok(cli.set_trigger_mode("auto"), "trigger mode")     # nothing to trigger on
        wait(cli, lambda s: s.get("settings_settled"), "auto")
        m = measure(cli)
        zero = {ch: float(m[ch].get("mean", 0.0)) for ch in ("ch1", "ch2")}
        for ch in ("ch1", "ch2"):
            print(f"  zero {ch.upper()} = {zero[ch] * 1e3:+.2f} mV "
                  f"(rms noise {float(m[ch].get('rms', 0.0)) * 1e3:.2f} mV)")

        # ---- 2. W1 alone ---------------------------------------------------------------
        print("\n2. W1 sine -> CH1")
        for hz, vpp in CASES:
            print(f" W1 {hz:g} Hz, {vpp:g} Vpp, offset 0 V")
            for cmd in (g.set_waveform("w1", "sine"), g.set_frequency("w1", hz),
                        g.set_amplitude("w1", vpp), g.set_offset("w1", 0.0),
                        g.set_phase("w1", 0.0), g.set_output("w1", True)):
                ok(cmd, "W1")
            wait(cli, lambda s: s.get("w1_output") and s.get("w1_settled"), "W1 settled")
            setup_timebase(cli, hz)
            v = measure(cli)["ch1"]
            # amplitude from the rms (noise peaks inflate a pk-pk): Vpp = 2 sqrt(2) rms
            mean = float(v.get("mean", 0.0))
            rms_ac = math.sqrt(max(float(v.get("rms", 0.0)) ** 2 - mean ** 2, 0.0))
            c.check("CH1 amplitude (Vpp)", 2 * math.sqrt(2) * rms_ac, vpp, TOL_AMPLITUDE, True)
            c.check("CH1 frequency (Hz)", float(v.get("frequency", float("nan"))), hz,
                    TOL_FREQUENCY, True)
            c.check("CH1 offset (V, - zero)", mean - zero["ch1"], 0.0, TOL_OFFSET_V, False)

        # ---- 3. W2 with a phase offset ------------------------------------------------
        hz, v1, v2, dphi = PHASE_CASE
        print(f"\n3. W1 and W2 at {hz:g} Hz, W2 {dphi:+g} deg -> CH2, phase CH2 - CH1")
        for cmd in (g.set_frequency("w1", hz), g.set_amplitude("w1", v1),
                    g.set_waveform("w2", "sine"), g.set_amplitude("w2", v2),
                    g.set_offset("w2", 0.0), g.set_follow(True, dphi, True),
                    g.set_output("w1", True), g.set_output("w2", True)):
            ok(cmd, "W2")
        wait(cli, lambda s: (s.get("w1_settled") and s.get("w2_settled")
                             and s.get("w2_output") and s.get("gen_follow")), "W1/W2 settled")
        setup_timebase(cli, hz)
        m = measure(cli)
        w = m["ch2"]
        mean = float(w.get("mean", 0.0))
        rms_ac = math.sqrt(max(float(w.get("rms", 0.0)) ** 2 - mean ** 2, 0.0))
        c.check("CH2 amplitude (Vpp)", 2 * math.sqrt(2) * rms_ac, v2, TOL_AMPLITUDE, True)
        c.check("CH2 frequency (Hz)", float(w.get("frequency", float("nan"))), hz,
                TOL_FREQUENCY, True)
        c.check("CH2 offset (V, - zero)", mean - zero["ch2"], 0.0, TOL_OFFSET_V, False)
        ph = m["phase_21_deg"]
        c.check("phase CH2 - CH1 (deg)", float("nan") if ph is None else float(ph), dphi,
                TOL_PHASE_DEG, False)
    finally:
        # ---- 4. outputs off, whatever happened ----------------------------------------
        try:
            cli.gen.outputs_off()
            cli.gen.set_follow(False)
            print("\noutputs OFF")
        finally:
            cli.release_control()
            cli.shutdown()
    print(f"\n{'ALL PASSED' if c.failed == 0 else f'{c.failed} CHECK(S) FAILED'}")
    return 0 if c.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
