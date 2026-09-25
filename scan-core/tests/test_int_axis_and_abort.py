"""Two faults found on the rig, 2026-09-16, with a 21 x 21 scan of the camera's
scan-array indices: the scan sat at 0 % and Abort did nothing.

  1. An INT control was sent 0.95 (21 points across indices 0..19). The service
     rounds it to 1, so the setpoint it echoes never equals the target, and the
     settle policy waits out its whole 60 s timeout ON A POINT THAT ARRIVED.
  2. Abort is only checked between points, so during that wait the window looks
     frozen.
"""

import threading
import time

import pytest
xr = pytest.importorskip('xarray')

zmq = pytest.importorskip("zmq")

from scan_core.instrument import Instrument, ScanAborted
from scan_core.recipe import Recipe
from scan_core.manifest import register_manifest
from scan_core.registry import Registry


class IndexService:
    """A service with ONE int control, which rounds what it is given."""

    def __init__(self):
        self.index = 0
        self.received: list = []
        ctx = zmq.Context.instance()
        self._rep = ctx.socket(zmq.REP)
        self.cmd_port = self._rep.bind_to_random_port("tcp://127.0.0.1")
        self._pub = ctx.socket(zmq.PUB)
        self.pub_port = self._pub.bind_to_random_port("tcp://127.0.0.1")
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def manifest(self) -> dict:
        return {"schema": 1, "module": "cam", "revision": 1, "parameters": [
            {"id": "scan_ix", "label": "Scan point X", "kind": "control", "type": "int",
             "unit": "", "min": 0, "max": 19, "read_path": ["selected_index_x"],
             "set": {"verb": "set_selected_index", "arg": "ix"},
             "settle": {"policy": "adopt_then_flag", "setpoint_key": "selected_index_x",
                        "flag_key": "point_settled"}}]}

    def _status(self) -> dict:
        return {"selected_index_x": self.index, "point_settled": True, "describe_rev": 1}

    def _serve(self):
        poller = zmq.Poller()
        poller.register(self._rep, zmq.POLLIN)
        while not self._stop.is_set():
            if poller.poll(50):
                req = self._rep.recv_json()
                cmd = req.get("cmd")
                if cmd == "describe":
                    self._rep.send_json({"ok": True, "describe": self.manifest()})
                elif cmd == "status":
                    self._rep.send_json({"ok": True, "status": self._status()})
                elif cmd == "info":
                    self._rep.send_json({"ok": True, "info": {}})
                elif cmd == "set_selected_index":
                    self.received.append(req.get("ix"))
                    self.index = int(round(float(req["ix"])))   # the camera rounds
                    self._rep.send_json({"ok": True})
                else:
                    self._rep.send_json({"ok": False, "error": "unknown"})
        self._rep.close(0)
        self._pub.close(0)

    def close(self):
        self._stop.set()
        self._t.join(1)


@pytest.fixture
def service():
    svc = IndexService()
    yield svc
    svc.close()


def test_an_int_axis_is_sent_whole_numbers(service):
    """The fix: round for an int control, and settle on the rounded value."""
    inst = Instrument("cam", "127.0.0.1", service.cmd_port, service.pub_port, timeout_ms=1000)
    inst.start()
    reg = Registry()
    try:
        register_manifest(reg, inst, service.manifest(), prefix=False)
        p = reg.get("scan_ix")
        assert p.integer is True
        p.set(0.95)                      # what a 21-point sweep of 0..19 asks for
        assert service.received == [1]   # an int on the wire, and it settles
        assert service.index == 1
        p.set(1.9)
        assert service.received == [1, 2]
    finally:
        inst.close()


def test_abort_interrupts_a_settle_wait(service):
    """Abort must be seen DURING a wait, not only between points."""
    inst = Instrument("cam", "127.0.0.1", service.cmd_port, service.pub_port, timeout_ms=1000)
    inst.start()
    try:
        aborting = {"now": False}
        inst.should_abort = lambda: aborting["now"]
        threading.Timer(0.3, lambda: aborting.__setitem__("now", True)).start()
        t0 = time.monotonic()
        with pytest.raises(ScanAborted):
            # a condition that can never hold, with a long timeout
            inst.wait_until(lambda st: False, timeout_s=30.0, what="never")
        assert time.monotonic() - t0 < 5.0, "Abort waited for the timeout"
    finally:
        inst.close()


def _builder():
    """A Scan Builder on the simulated registry, offscreen."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    from scan_core import build_sim_registry

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return ScanBuilder(build_sim_registry())


def test_the_plot_fills_DURING_the_run_not_only_at_the_end():
    """A long scan is unwatchable if its data only appears at the end."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    win = _builder()
    try:
        win.add_axis("field")
        win.rows[0].num.setValue(5)
        seen = []
        win.worker = None
        from scan_core.engine import run as engine_run
        engine_run(win.build_recipe(), win.registry, created_iso="t",
                   on_point=lambda done, total, snap: seen.append((done, snap())))
        assert [d for d, _ in seen] == [1, 2, 3, 4, 5]
        first = seen[0][1]
        det = list(first.data_vars)[0]
        import numpy as np
        # the first snapshot has one real point and the rest NaN
        assert np.isfinite(first[det].values).sum() == 1
        assert np.isfinite(seen[-1][1][det].values).all()
        # and the GUI can draw such a half-filled dataset
        win._on_partial(first)
        assert win.det_combo.count() > 0
    finally:
        win.close()


def test_after_an_abort_the_next_run_can_start():
    """'I aborted, changed the range, and it would not start again': the failed
    signal only wrote a message -- Run stayed disabled and the worker stayed."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    win = _builder()
    try:
        win.add_axis("field")
        win.run_btn.setEnabled(False)          # as during a run
        win.abort_btn.setEnabled(True)
        win._on_failed("aborted (cam: aborted while waiting for field)")
        assert win.run_btn.isEnabled() and not win.abort_btn.isEnabled()
        assert win.worker is None and win.is_aborting() is False
        assert "aborted" in win.detail.text()
    finally:
        win.close()


def test_a_second_run_is_refused_while_one_is_still_running():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    win = _builder()
    try:
        win.add_axis("field")

        class Busy:
            def isRunning(self):
                return True

        win.worker = Busy()
        win.run_scan()
        assert "still running" in win.detail.text()
    finally:
        win.worker = None
        win.close()


def test_axis_rows_follow_limits_that_moved():
    """Limits MOVE (a resized scan array, an armed leash, a closed loop). The
    row must re-clamp to them, and a sweep past the new end must be refused --
    otherwise the service clamps and the file claims points never visited."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    win = _builder()
    try:
        p = win.registry.get("field")
        win.add_axis("field")
        row = win.rows[0]
        row.start.setValue(0.0)
        row.stop.setValue(150.0)
        assert "invalid" not in win.summary.text()

        # the instrument's range shrinks, and the refresher reports it
        p.limits = (-50.0, 50.0)
        win.limits_refresher = lambda: ["field"]
        assert win.refresh_axis_limits() == ["field"]
        assert row.stop.value() == 50.0                 # box re-clamped
        assert "-50" in row.limits_lbl.text()           # and it SAYS so
    finally:
        win.close()


def test_a_broken_refresher_does_not_block_the_builder():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    win = _builder()
    try:
        win.add_axis("field")

        def boom():
            raise RuntimeError("service went away")

        win.limits_refresher = boom
        assert win.refresh_axis_limits() == []
        assert "could not refresh limits" in win.detail.text()
    finally:
        win.close()


def test_a_measurement_file_is_a_scan_definition(tmp_path):
    """Save data, load the definition back out of it, and get the same stack."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    win = _builder()
    try:
        win.add_axis("field")
        win.rows[0].start.setValue(-10.0)
        win.rows[0].stop.setValue(10.0)
        win.rows[0].num.setValue(4)
        win.run_scan(block=True)                  # simulated registry: instant
        path = tmp_path / "measurement.nc"
        win.dataset.to_netcdf(path)

        win.load_recipe(Recipe(axes=[], detectors=[]))      # clear the stack
        assert win.rows == []
        missing = win.load_recipe(win.recipe_from_file(str(path)))
        assert missing == []
        assert [r.param.id for r in win.rows] == ["field"]
        assert (win.rows[0].start.value(), win.rows[0].stop.value()) == (-10.0, 10.0)
        assert win.rows[0].num.value() == 4
    finally:
        win.close()


def test_loading_a_definition_flags_what_is_not_available():
    """A definition written against other instruments must load what it can and
    NAME the rest -- not crash, and not silently drop half the scan."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    win = _builder()
    try:
        recipe = Recipe(
            axes=[{"type": "linear", "param": "field", "start": 0, "stop": 10, "num": 3},
                  {"type": "linear", "param": "hf2.tc1", "start": 0, "stop": 1, "num": 2}],
            detectors=["lockin_r", "pm16.power"])
        missing = win.load_recipe(recipe)
        assert missing == ["hf2.tc1", "pm16.power"]
        assert [r.param.id for r in win.rows] == ["field"]       # the rest still loads
        assert win.build_recipe().detectors == ["lockin_r"]
    finally:
        win.close()


def test_every_run_is_saved_by_itself(tmp_path):
    """A scan nobody remembered to save is a scan that did not happen."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from apps.scan_builder import ScanWorker

    win = _builder()
    try:
        win.autosave_dir = tmp_path
        win.add_axis("field")
        win.rows[0].num.setValue(3)
        recipe = win.build_recipe()
        path = win.autosave_path(recipe)
        assert path.parent.name == time.strftime("%Y-%m-%d")     # dated folder
        assert path.suffix == ".nc"

        worker = ScanWorker(recipe, win.registry, save_path=path)
        worker.run()                                  # in THIS thread, no event loop
        assert path.is_file()
        with xr.open_dataset(path) as ds:
            assert ds.sizes["field"] == 3
            assert ds.attrs["recipe_json"]            # the definition rode along
        assert not list(path.parent.glob("*.writing.nc")), "temp file left behind"
    finally:
        win.close()


def test_a_long_scan_is_checkpointed_every_tenth():
    """Over 100 points, the file is written as it goes, so an abort or a crash
    still leaves what was measured."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from apps.scan_builder import ScanWorker

    w = ScanWorker(None, None, save_path=None)
    written = []
    w._write = lambda ds, done, total: written.append(done)
    snap = lambda: None

    for done in range(1, 251):
        w._live(done, 250, snap)
    assert written == [25, 50, 75, 100, 125, 150, 175, 200, 225]   # not the last: the
    #                                                                final save covers it

    short = ScanWorker(None, None, save_path=None)
    short._write = lambda ds, done, total: written.append(("short", done))
    for done in range(1, 51):
        short._live(done, 50, snap)
    assert not [x for x in written if isinstance(x, tuple)], "a short scan needs no checkpoints"


def test_an_int_axis_defaults_to_one_point_per_value():
    """The Scan Builder must not offer 21 points across 0..19 for an index."""
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from apps.scan_builder import AxisRow
    from scan_core.registry import Settable

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    p = Settable("cam.scan_ix", "Scan point X", "", (0, 19),
                 set_fn=lambda v: None, get_fn=lambda: 0)
    p.integer = True
    row = AxisRow(p, lambda r: 0)
    try:
        assert (row.start.value(), row.stop.value()) == (0, 19)
        assert row.num.value() == 20            # one point per index, not 21
        assert row.start.decimals() == 0        # "19", not "19.000"

        # pts is CAPPED at the number of distinct values: there is no 0.95th point
        assert row.num.maximum() == 20
        row.num.setValue(999)
        assert row.num.value() == 20
        row.stop.setValue(4)                    # a shorter sweep caps tighter
        assert row.num.maximum() == 5 and row.num.value() == 5
    finally:
        row.deleteLater()
