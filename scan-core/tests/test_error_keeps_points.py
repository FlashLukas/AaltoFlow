"""An ERROR mid-scan keeps the points measured before it.

Lab PC 2026-10-06, the first scan on the scan server: the DS generator
ignored a -13.75 dBm setpoint, the settle wait for it timed out at point 6
of 25, and the five good points were never saved (no file, no folder). An
Abort and a fault already handed their points over (deep cleaning
2026-09-28); any other error did not.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from scan_core import Recipe, run
from scan_core.registry import Gettable, Registry, Settable


def _registry(fail_at: float):
    reg = Registry()
    state = {"p": 0.0}

    def set_p(v):
        if abs(v - fail_at) < 1e-9:
            raise TimeoutError(f"waited 60 s for p = {v}")
        state["p"] = v
    reg.add(Settable("p", "Power", "dBm", (-30, 10), set_p, lambda: state["p"]))
    reg.add(Gettable("det", "Detector", "mW", lambda: state["p"] + 100.0))
    return reg


def _recipe():
    return Recipe(name="err", axes=[{"type": "linear", "param": "p",
                                     "start": -20, "stop": 0, "num": 5}],
                  detectors=["det"])


def test_the_engine_hands_over_the_points_before_an_error():
    with pytest.raises(TimeoutError) as info:
        run(_recipe(), _registry(fail_at=-10.0))
    ds = info.value.dataset
    vals = ds["det"].values
    assert np.allclose(vals[:2], [80.0, 85.0])          # -20 and -15 dBm measured
    assert all(math.isnan(v) for v in vals[2:])
    assert ds.attrs["stopped_by"].startswith("error: waited 60 s")


def test_the_scan_server_saves_them(tmp_path):
    from test_scan_server import free_ports, wait_for
    from scan_core.scan_server import ScanServer
    from scan_core.scan_server_client import ScanServerClient
    cmd, pub = free_ports()
    srv = ScanServer(host="127.0.0.1", cmd_port=cmd, pub_port=pub,
                     registry=_registry(fail_at=-10.0), data_dir=tmp_path / "data",
                     echo=False)
    srv.start()
    c = ScanServerClient("127.0.0.1", cmd, pub)
    c.start()
    try:
        c.submit(_recipe())
        wait_for(lambda: srv._entries and srv._entries[0].result == "error")
        files = list((tmp_path / "data").rglob("*.nc"))
        assert len(files) == 1, "the measured points were not saved"
        from scan_core.data import load
        ds = load(files[0])
        assert np.allclose(ds["det"].values[:2], [80.0, 85.0])
        ds.close()
    finally:
        c.close()
        srv.stop()


def test_the_after_scan_routine_runs_after_an_error():
    """Lukas 2026-10-06: the settle timeout left RF ON -- after_scan (RF off)
    did not run on an error. It does now, and the error still surfaces."""
    reg = _registry(fail_at=-10.0)
    state = {"rf": 0}
    reg.add(Settable("rf", "RF output", "", (0, 1),
                     lambda v: state.__setitem__("rf", v), lambda: state["rf"]))
    r = _recipe()
    r.hooks = [{"when": "before_scan", "action": "call", "args": {"set": {"rf": 1}}},
               {"when": "after_scan", "action": "call", "args": {"set": {"rf": 0}}}]
    log = []
    with pytest.raises(TimeoutError):
        run(r, reg, on_log=log.append)
    assert state["rf"] == 0
    assert any("running the after-scan routine" in m for m in log)


def test_after_an_error_a_failing_after_step_does_not_stop_the_next():
    reg = _registry(fail_at=-10.0)
    state = {"rf": 1, "field": 5}

    def broken(v):
        raise RuntimeError("magnet does not answer")
    reg.add(Settable("field", "Field", "mT", (-10, 10), broken, lambda: state["field"]))
    reg.add(Settable("rf", "RF output", "", (0, 1),
                     lambda v: state.__setitem__("rf", v), lambda: state["rf"]))
    r = _recipe()
    r.hooks = [{"when": "after_scan", "action": "call",
                "args": {"steps": [{"set": {"field": 0}}, {"set": {"rf": 0}}]}}]
    with pytest.raises(TimeoutError):                 # the ORIGINAL error
        run(r, reg)
    assert state["rf"] == 0                           # the step after the broken one ran
