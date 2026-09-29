"""The Control tab speaks as a PERSON; scans stay a machine (control.py).

Lukas (2026-09-29): a person at the suite's Control tab is exactly who control
exists for -- a trainee at another PC must not change what someone else
controls -- while a scan must never be locked out. Control belongs to a PC: the
kim GUI and the suite on the same PC share it. Ports 15890-15895.
"""

from __future__ import annotations

import copy
import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from conftest import CONTROLLED_MANIFEST as MANIFEST, ControlledFake  # noqa: E402
from scan_core.lab import build_lab_registry  # noqa: E402
from suite_common.control import make_identity  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _tick(panel, pid):
    from PySide6.QtCore import Qt
    it = QtWidgets.QTreeWidgetItemIterator(panel.tree)
    while it.value():
        if it.value().data(0, Qt.UserRole) == pid:
            it.value().setCheckState(0, Qt.Checked)
            return True
        it += 1
    return False


def _wait(pred, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        QtWidgets.QApplication.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def rig(qapp, tmp_path, monkeypatch):
    import apps.control_panel as cp
    from apps.control_panel import ControlPanel
    monkeypatch.setattr(cp, "LAYOUTS_PATH", tmp_path / "layouts.json")
    svc = ControlledFake(15890, manifest=MANIFEST).start()
    reg, lab = build_lab_registry(host="127.0.0.1", include=("clMag",),
                                  ports={"clMag": svc.cmd_port}, prefix=True)
    panel = ControlPanel(on_log=lambda m: logs.append(m))
    logs: list[str] = []
    panel.set_source(registry=reg, lab=lab, prefix=True)
    for pid in ("fake.rf_power", "fake.demag", "fake.stop", "fake.measured_field"):
        assert _tick(panel, pid)
    yield svc, lab, panel, logs
    panel.timer.stop()
    lab.close()
    svc.stop()


def test_a_panel_at_another_pc_is_a_viewer_and_can_take_control(rig, monkeypatch):
    svc, lab, panel, logs = rig
    trainer = make_identity("gui", "kim GUI")
    trainer["host"] = "trainer@lab-pc-elsewhere"
    assert svc.lease.handle({"cmd": "take_control", "client": trainer})["granted"]
    inst = next(iter(lab.instruments.values()))

    # (a status frame from before the take may come first: wait for the viewer)
    assert _wait(lambda: (panel._refresh(), "fake" in panel.control_rows and
                          "VIEWER" in panel.control_rows["fake"][1].text())[-1])
    _row, label, btn = panel.control_rows["fake"]
    assert "kim GUI" in label.text()
    assert btn.text() == "Take control"
    w = panel.widgets
    assert not w["fake.rf_power"].isEnabled()          # a knob: greyed
    assert not w["fake.demag"].isEnabled()             # an action it may not send
    assert w["fake.stop"].isEnabled()                  # always allowed (safety)
    assert w["fake.measured_field"].isEnabled()        # a readout: never greyed

    # the panel's clicks go as a person: refused while the trainer holds it...
    w["fake.rf_power"]._send(-5.0)
    assert any("read-only" in m for m in logs)
    # ...while the SCAN's commands (machine) pass
    inst.command("set_power", power_dBm=-7.0)
    assert svc.sent[-1]["client"]["kind"] == "machine"

    # take it over: asks first, then the panel controls the module
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.Yes)
    btn.click()
    assert svc.lease.status()["holder"]["id"] == inst.gui_identity["id"]
    assert _wait(lambda: (panel._refresh(), w["fake.rf_power"].isEnabled())[-1])
    assert "you have control" in label.text() and btn.text() == "Release"
    w["fake.rf_power"]._send(-5.0)
    assert svc.sent[-1]["cmd"] == "set_power" and svc.sent[-1]["client"]["kind"] == "gui"


def test_the_same_pc_shares_control_and_a_scan_shows_as_driving(rig):
    svc, lab, panel, logs = rig
    inst = next(iter(lab.instruments.values()))
    gui = make_identity("gui", "kim GUI")
    gui["host"] = inst.gui_identity["host"]            # a kim GUI on THIS PC
    svc.lease.handle({"cmd": "take_control", "client": gui})
    inst.command("set_power", power_dBm=-3.0)          # the scan changes something
    assert _wait(lambda: (panel._refresh(), "fake" in panel.control_rows)[-1])
    _row, label, _btn = panel.control_rows["fake"]
    assert _wait(lambda: (panel._refresh(), "also driving: scan-core" in label.text())[-1])
    assert "you have control (this PC)" in label.text()
    assert panel.widgets["fake.rf_power"].isEnabled()
