"""The measurement suite WATCHING a scan server (apps/scan_server_view.py).

Offscreen, against a real ScanServer on scratch ports (the simulator inside).
The Measurement tab must show the server's scan with its usual widgets:
progress bar, the live map (DataView), the log, the PAUSED fault banner with
its Clear fault button, the operator banner with Continue / Abort -- and its
buttons must send the server's verbs. With "Run scans on this PC's scan
server" on, Run submits there.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtWidgets                                     # noqa: E402

from scan_core import Recipe, hooks                               # noqa: E402
from scan_core.scan_server import ScanServer                      # noqa: E402
from scan_core.scan_server_client import ScanServerClient         # noqa: E402

from test_scan_server import free_ports, pause_step, recipe, slow_registry  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(hooks, "PAUSE_POLL_S", 0.01)


def pump(app, until, timeout=15.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        app.processEvents()
        if until():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def rig(qapp, tmp_path, monkeypatch):
    """(server, suite watching it, a submitting client on this PC)."""
    import apps.control_panel as cp
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    from apps.suite import Suite
    made = {}

    def make(registry=None, settings=None):
        if settings:
            (tmp_path / "suite_local.json").write_text(
                json.dumps({"modules": {}, "remote": [], "settings": settings}),
                encoding="utf-8")
        cmd, pub = free_ports()
        srv = ScanServer(host="127.0.0.1", cmd_port=cmd, pub_port=pub,
                         registry=registry or slow_registry(), data_dir=tmp_path / "data",
                         echo=False, live_every_s=0.2, status_hz=10.0)
        srv.start()
        win = Suite(root=tmp_path, follow=False, scan_server=f"127.0.0.1:{cmd}:{pub}")
        c = ScanServerClient("127.0.0.1", cmd, pub)
        c.start()
        made.update(srv=srv, win=win, c=c)
        pump(qapp, lambda: win.watch is not None and win.watch.answering)
        return srv, win, c
    yield make
    if made:
        made["c"].close()
        made["win"].close()
        made["srv"].stop()


def test_watch_mode_shows_progress_live_map_log_and_header(qapp, rig):
    srv, win, c = rig()
    b = win.builder
    assert win.tabs.tabText(win.tabs.currentIndex()) == "Measurement"
    assert win.server_strip.isVisible() or not win.isVisible()   # offscreen: not shown
    assert "watching scan server on this PC" in win.server_lbl.text()
    assert not b.run_btn.isEnabled()          # watch only: submitting is phase 2 here
    c.submit(recipe(num=80, dets=("slow",)), attrs={"sample": "S7"})
    pump(qapp, lambda: 0 < b.progress.value() < 80 and b.progress.maximum() == 80)
    assert b.abort_btn.isEnabled()
    pump(qapp, lambda: b.dataset is not None and "slow" in b.dataset)
    assert b.view.ds is not None
    win._tick()
    assert win.run_state.text() == "RUNNING"
    assert "point" in win.where_lbl.text()
    assert "the server saves to" in b.save_lbl.text()
    # Abort -> the server's verb
    b.abort_btn.click()
    pump(qapp, lambda: not srv.running)
    assert srv._entries[0].result == "aborted"
    pump(qapp, lambda: "[server" in win.logbox.toPlainText()
         and "ABORT pressed" in win.logbox.toPlainText())
    pump(qapp, lambda: not b.abort_btn.isEnabled())


def test_operator_banner_continue_and_abort_buttons(qapp, rig):
    srv, win, c = rig()
    b = win.builder
    c.submit(recipe(num=5, hooks_=[pause_step("Insert the polariser")]))
    pump(qapp, lambda: b.ask_box.isVisibleTo(b.right_pane) and
         "polariser" in b.ask_lbl.text())
    b.ask_continue_btn.click()
    pump(qapp, lambda: not srv.running)
    assert srv._entries[0].result == "done"
    pump(qapp, lambda: not b.ask_box.isVisibleTo(b.right_pane))
    # Abort scan at the pause
    c.submit(recipe(num=5, hooks_=[pause_step("again")]))
    pump(qapp, lambda: b.ask_box.isVisibleTo(b.right_pane) and "again" in b.ask_lbl.text())
    b.ask_abort_btn.click()
    pump(qapp, lambda: not srv.running)
    assert srv._entries[0].result == "aborted"


def test_fault_banner_and_clear_fault_button(qapp, rig):
    reg = slow_registry()
    faults = [("camera", "pattern lost")]
    reg.fault_check = lambda ids=None: list(faults)
    cleared = []

    class FakeLab:
        def set_abort(self, fn):
            pass

        def can_clear_fault(self, name):
            return True

        def clear_fault(self, name):
            cleared.append(name)
            faults.clear()
            return {"ok": True}

        def close(self):
            pass

    srv, win, c = rig(registry=reg)
    srv.lab = FakeLab()
    b = win.builder
    c.submit(recipe(num=5))
    pump(qapp, lambda: b.pause_box.isVisibleTo(b.right_pane) and "camera" in b.clear_fault_btns)
    assert "pattern lost" in b.pause_lbl.text()
    win._tick()
    assert win.run_state.text() == "PAUSED"
    b.clear_fault_btns["camera"].click()
    assert cleared == ["camera"]
    pump(qapp, lambda: not srv.running, timeout=20)
    assert srv._entries[0].result == "done"
    pump(qapp, lambda: not b.pause_box.isVisibleTo(b.right_pane))


def test_stop_queue_button_and_queue_label(qapp, rig):
    srv, win, c = rig()
    b = win.builder
    c.submit_queue([("first", recipe(num=200, dets=("slow",))), ("second", recipe(num=5))])
    pump(qapp, lambda: "Scan 1 of 2" in b.queue_lbl.text())
    assert b.stop_queue_btn.isVisibleTo(b.right_pane)
    b.stop_queue_btn.click()
    pump(qapp, lambda: not srv.running)
    assert [e.result for e in srv._entries] == ["aborted", None]
    pump(qapp, lambda: "1 not run" in b.queue_lbl.text())


def test_run_submits_to_this_pcs_server_when_the_setting_is_on(qapp, rig):
    srv, win, c = rig(settings={"run_on_scan_server": True})
    b = win.builder
    assert win.run_on_server_box.isChecked()
    pump(qapp, lambda: b.server_submit and b.run_btn.isEnabled())
    b.load_recipe(Recipe(name="from_run", axes=[{"type": "linear", "param": "field",
                                                 "start": 0.0, "stop": 10.0, "num": 6}],
                         detectors=["lockin_r"]))
    b.run_scan()
    pump(qapp, lambda: srv._entries and srv._entries[0].name == "from_run"
         and not srv.running)
    assert srv._entries[0].result == "done"
    assert b.worker is None                 # nothing ran in this window
    # untick: watching goes on, Run no longer submits
    win.run_on_server_box.setChecked(False)
    assert not b.server_submit and not b.run_btn.isEnabled()


def test_closing_the_suite_does_not_stop_the_servers_scan(qapp, rig):
    srv, win, c = rig()
    c.submit(recipe(num=60, dets=("slow",)))
    pump(qapp, lambda: srv.status_payload()["done"] > 2)
    win.close()
    assert srv.running
    pump(qapp, lambda: not srv.running, timeout=20)
    assert srv._entries[0].result == "done"


def test_following_the_launcher_skips_a_scan_server_and_lists_it_to_watch(
        qapp, tmp_path, monkeypatch, fake_service):
    """A running scan server is NOT connected as an instrument (no
    "scanserver.*" parameters); it shows up in Settings > Watch scan server."""
    import apps.control_panel as cp
    import apps.suite as suite_mod
    from conftest import DEMO_MANIFEST
    from test_scan_server import _module
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    monkeypatch.setattr(suite_mod, "AVAILABILITY_PERIOD_S", 0.2)
    cmd, _ = free_ports()
    svc = fake_service(cmd, manifest=DEMO_MANIFEST)
    _module(tmp_path, "modules/field/magnet-control", "magnet", svc.cmd_port)
    s_cmd, s_pub = free_ports()
    _module(tmp_path, "scan-core", "scanserver", s_cmd)
    srv = ScanServer(host="127.0.0.1", cmd_port=s_cmd, pub_port=s_pub,
                     registry=slow_registry(), echo=False)
    srv.start()
    win = suite_mod.Suite(root=tmp_path, follow=True)
    try:
        pump(qapp, lambda: win.connected_ids == ["magnet"])
        assert not any(p.id.startswith("scanserver") for p in win.registry.settables())
        rows = [win.mod_tree.topLevelItem(i).text(0)
                for i in range(win.mod_tree.topLevelItemCount())]
        assert rows == ["magnet module"]
        pump(qapp, lambda: win.watch_combo.count() == 1
             and "(running)" in win.watch_combo.itemText(0))
        assert win.watch_combo.itemData(0) == f"localhost:{s_cmd}:{s_pub}"
    finally:
        win.close()
        srv.stop()


def test_stop_watching_gives_the_pane_back(qapp, rig):
    srv, win, c = rig()
    b = win.builder
    win.stop_watching()
    assert win.watch is None and b.server is None
    assert b.run_btn.isEnabled() and not win.server_strip.isVisibleTo(win)
