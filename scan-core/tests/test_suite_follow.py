"""The suite follows the launcher: module discovery decides what it can use.

A throwaway suite root holds fake module folders whose module.toml points at
`FakeService` ports, so the whole path -- discovery, availability check,
endpoints, prefixes -- runs over real sockets with nothing installed.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtWidgets

import apps.suite as suite_mod
from apps.suite import Suite
from conftest import DEMO_MANIFEST
from suite_common import add_remote


def _module(root: Path, key: str, cmd: int, order=10):
    d = root / f"{key}-control"
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "run_service.py").write_text("")
    (d / "module.toml").write_text(
        f'[module]\nkey = "{key}"\nname = "{key} module"\norder = {order}\n'
        f'[ports]\ncmd = {cmd}\npub = {cmd + 1}\n'
        f'[run]\nservice = "scripts/run_service.py"\ngui = ""\n')


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(suite_mod, "AVAILABILITY_PERIOD_S", 0.2)


def _pump(app, until, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if until():
            return True
        time.sleep(0.03)
    return until()


def test_follows_the_launcher_and_prefixes_by_module(qapp, fake_service, tmp_path,
                                                     monkeypatch, fast):
    import apps.control_panel as cp
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    svc = fake_service(15940, manifest=DEMO_MANIFEST)
    _module(tmp_path, "magnet", svc.cmd_port)
    _module(tmp_path, "rf", 15948, order=20)             # nothing listens there

    win = Suite(root=tmp_path, follow=True)
    try:
        assert _pump(qapp, lambda: win.lab is not None), "never connected"
        assert win.connected_ids == ["magnet"]           # only what answers
        assert win.registry.get("magnet.field") is not None
        assert "magnet" in win.source_lbl.text()
        rows = {win.mod_tree.topLevelItem(i).text(0): win.mod_tree.topLevelItem(i).text(1)
                for i in range(win.mod_tree.topLevelItemCount())}
        assert rows == {"magnet module": "connected", "rf module": "down"}

        # a second module starts: the suite adjusts on its own
        svc2 = fake_service(15948, manifest=DEMO_MANIFEST)
        assert _pump(qapp, lambda: set(win.connected_ids) == {"magnet", "rf"})
        assert win.registry.get("rf.field") is not None    # no id collision
    finally:
        win.close()


def test_does_not_throw_away_an_axis_stack(qapp, fake_service, tmp_path, monkeypatch, fast):
    import apps.control_panel as cp
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    svc = fake_service(15950, manifest=DEMO_MANIFEST)
    _module(tmp_path, "magnet", svc.cmd_port)
    _module(tmp_path, "rf", 15958, order=20)
    win = Suite(root=tmp_path, follow=True)
    try:
        assert _pump(qapp, lambda: win.lab is not None)
        win.builder.add_axis("magnet.field")
        fake_service(15958, manifest=DEMO_MANIFEST)
        assert _pump(qapp, lambda: "axis stack is kept" in win.logbox.toPlainText())
        assert win.connected_ids == ["magnet"]           # left alone
        assert len(win.builder.rows) == 1
    finally:
        win.close()


def test_remote_services_are_listed_and_connectable(qapp, fake_service, tmp_path,
                                                    monkeypatch, fast):
    import apps.control_panel as cp
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    svc = fake_service(15960, manifest=DEMO_MANIFEST)
    _module(tmp_path, "magnet", 15968)                   # local copy, not running
    rid = add_remote("127.0.0.1", svc.cmd_port, svc.cmd_port + 1, "magnet", root=tmp_path)
    win = Suite(root=tmp_path, follow=False)
    try:
        assert _pump(qapp, lambda: rid in win.available)
        assert win.lab is None                           # not following: no connect
        win.connect_modules([rid])
        assert win.connected_ids == [rid]
        assert win.registry.get("magnet_127_0_0_1.field") is not None
    finally:
        win.close()


def test_choosing_the_simulator_stops_following(qapp, tmp_path, monkeypatch, fast):
    import apps.control_panel as cp
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    win = Suite(root=tmp_path, follow=True)
    try:
        win._simulator_clicked()
        assert not win.follow_box.isChecked()
        assert win.lab is None
    finally:
        win.close()
