"""
run_sweep.py
============

End-to-end demonstration. This is the file you actually run.

It:
  1. launches TWO mock instrument servers as separate PROCESSES (ports 5555 and
     5557 -- your blueprint's port scheme), so the ZeroMQ round-trip is real;
  2. wraps each as a QCoDeS instrument;
  3. runs a 1-D field sweep -> a 1-D dataset;
  4. runs a 2-D (field x axis2) sweep -> a genuine multidimensional dataset;
  5. exports both to xarray + netCDF and saves a plot.

Everything the sync layer must do -- set driven params, wait until settled,
grab the detector, accumulate into an N-dimensional labelled array, persist it --
is handled by QCoDeS given only the blocking `field.set()` we defined.
"""

import subprocess
import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless: render to file, never open a window
import matplotlib.pyplot as plt

import zmq

from qcodes.dataset import (
    dond,
    LinSweep,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes_adapter import Magnet

HERE = Path(__file__).parent
PORTS = [5555, 5557]


def wait_for_port(port, timeout_s=10.0):
    """Ping the server until it answers, so we don't sweep before it's up."""
    ctx = zmq.Context.instance()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        s = ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, 300)
        s.setsockopt(zmq.LINGER, 0)
        s.connect(f"tcp://127.0.0.1:{port}")
        try:
            s.send_json({"cmd": "ping"})
            if s.recv_json().get("ok"):
                s.close(0)
                return
        except zmq.Again:
            pass
        finally:
            s.close(0)
    raise RuntimeError(f"server on {port} never came up")


def main():
    # --- 1. start the fake instruments as child processes ------------------
    procs = [
        subprocess.Popen([sys.executable, str(HERE / "mock_instrument.py"),
                          "--port", str(p)])
        for p in PORTS
    ]
    for p in PORTS:
        wait_for_port(p)
    print("[run] both mock instruments are up")

    # --- 2. QCoDeS storage: one SQLite file holds every run ----------------
    initialise_or_create_database_at(str(HERE / "spike.db"))
    load_or_create_experiment("sync_spike", sample_name="mock")

    # --- 3. wrap the clients ----------------------------------------------
    mag = Magnet("magnet", port=5555)
    ax2 = Magnet("axis2", port=5557)  # a second driven axis (e.g. temperature)

    try:
        # --- 4a. 1-D sweep: the "grab a data point" case ------------------
        print("[run] 1-D field sweep ...")
        ds1, _, _ = dond(
            LinSweep(mag.field, -30, 30, 31, delay=0),  # driven axis
            mag.kerr,                                    # measured
            do_plot=False,
            measurement_name="field_sweep_1d",
        )
        xr1 = ds1.to_xarray_dataset()
        print("   1-D dataset dims:", dict(xr1.sizes))

        # --- 4b. 2-D sweep: the "array of points / multidimensional" case -
        print("[run] 2-D (field x axis2) sweep ...")
        ds2, _, _ = dond(
            LinSweep(mag.field, -30, 30, 21, delay=0),   # outer driven axis
            LinSweep(ax2.field, -10, 10, 11, delay=0),   # inner driven axis
            mag.kerr,                                     # measured
            do_plot=False,
            measurement_name="field_sweep_2d",
        )
        xr2 = ds2.to_xarray_dataset()
        print("   2-D dataset dims:", dict(xr2.sizes))

        # --- 5. persist to portable formats + a picture -------------------
        xr1.to_netcdf(HERE / "field_sweep_1d.nc")
        xr2.to_netcdf(HERE / "field_sweep_2d.nc")

        fig, (a, b) = plt.subplots(1, 2, figsize=(11, 4))
        xr1["magnet_kerr"].plot(ax=a, marker="o")
        a.set_title("1-D: Kerr vs field")
        xr2["magnet_kerr"].plot(ax=b)  # a 2-D array -> a heatmap
        b.set_title("2-D: Kerr over (field, axis2)")
        fig.tight_layout()
        fig.savefig(HERE / "sweeps.png", dpi=120)
        print("[run] wrote field_sweep_1d.nc, field_sweep_2d.nc, sweeps.png, spike.db")

    finally:
        mag.close()
        ax2.close()
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
