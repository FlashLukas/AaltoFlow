"""
run_meqi_sweep.py
=================

The payoff: a QCoDeS 1-D sweep driving your REAL meqi service (sim backend).

  1. start meqi's own sim service as a subprocess (its unchanged code);
  2. wrap it with the Meqi QCoDeS adapter;
  3. dond sweeps the field from -40 to +40 mT -- at each point it SETS the field,
     BLOCKS until the service reports it stable, then READS measured_field and
     current into the dataset;
  4. export to netCDF + a plot.

You wrote no sweep loop and no settle logic in the experiment itself: QCoDeS's
dond does the set/wait/read/store, driven entirely by the adapter's blocking set.
"""

import subprocess, sys, time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import zmq

from qcodes.dataset import (dond, LinSweep,
                            initialise_or_create_database_at,
                            load_or_create_experiment)
from meqi_qcodes import Meqi

HERE = Path(__file__).parent
CMD_PORT, PUB_PORT = 5555, 5556


def wait_for_service(port, timeout_s=15.0):
    ctx = zmq.Context.instance()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        s = ctx.socket(zmq.REQ)
        s.setsockopt(zmq.RCVTIMEO, 400); s.setsockopt(zmq.LINGER, 0)
        s.connect(f"tcp://127.0.0.1:{port}")
        try:
            s.send_json({"cmd": "info"})
            if s.recv_json().get("ok"):
                s.close(0); return
        except zmq.Again:
            pass
        finally:
            s.close(0)
    raise RuntimeError("meqi service never came up")


def main():
    proc = subprocess.Popen([sys.executable, str(HERE / "run_meqi_service.py"),
                             "--cmd-port", str(CMD_PORT), "--pub-port", str(PUB_PORT)])
    try:
        wait_for_service(CMD_PORT)
        print("[sweep] meqi sim service is up")

        initialise_or_create_database_at(str(HERE / "meqi_spike.db"))
        load_or_create_experiment("meqi_qcodes_spike", sample_name="sim")

        magnet = Meqi("meqi", host="localhost", cmd_port=CMD_PORT, pub_port=PUB_PORT)
        print("[sweep] connected; IDN =", magnet.get_idn())

        t0 = time.monotonic()
        ds, _, _ = dond(
            LinSweep(magnet.field, -40, 40, 17, delay=0),   # driven axis
            magnet.measured_field,                           # measured
            magnet.current,                                  # measured
            do_plot=False,
            measurement_name="field_hysteresis_1d",
        )
        print(f"[sweep] done in {time.monotonic()-t0:.1f}s")

        xr = ds.to_xarray_dataset()
        print("[sweep] dataset dims:", dict(xr.sizes))
        xr.to_netcdf(HERE / "meqi_field_sweep.nc")

        fig, ax = plt.subplots(figsize=(6, 4.2))
        ax.plot(xr["meqi_field"], xr["meqi_measured_field"], "o-", lw=1)
        ax.set_xlabel("Applied field setpoint [mT]")
        ax.set_ylabel("Measured field [mT]")
        ax.set_title("meqi sim: measured vs setpoint (QCoDeS dond)")
        ax.grid(alpha=0.3)
        fig.tight_layout(); fig.savefig(HERE / "meqi_sweep.png", dpi=120)
        print("[sweep] wrote meqi_field_sweep.nc, meqi_sweep.png, meqi_spike.db")

        magnet.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
