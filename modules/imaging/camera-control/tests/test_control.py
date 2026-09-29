"""One controller, many viewers on the camera (control.py, apps/control_bar.py).

Lukas (2026-09-29): the first GUI gets control, later ones are VIEWERS; the
service refuses their changes; Kill AF always works; the camera itself talks
to kim as a "machine" client (it must keep moving the stage while a person's
kim GUI holds control). Ports 15790/15791.
"""

from __future__ import annotations

import os
import time

import pytest

from camera.config import Config
from camera.sim_system import build_sim_system

CMD, PUB = 15790, 15791
pytest.importorskip("zmq")


def _wait(cond, timeout=4.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.03)
    return False


@pytest.fixture
def svc():
    from camera.net.service import CameraService
    cfg = Config()
    cfg.camera.frame_rate = 100.0
    brain, *_ = build_sim_system(cfg)
    s = CameraService(brain, host="127.0.0.1", cmd_port=CMD, pub_port=PUB, status_hz=20)
    s.start()
    yield s
    s.stop()


@pytest.fixture
def clients():
    from camera.net.client import CameraClient
    made = []

    def make(kind="gui", name="camera GUI"):
        c = CameraClient("127.0.0.1", CMD, PUB, timeout_ms=3000, kind=kind, name=name)
        c.start()
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def test_a_viewer_can_look_and_kill_af_but_not_change(svc, clients):
    from camera.control import ControlRefused
    a = clients(name="camera GUI A")
    b = clients(name="camera GUI B")
    assert a.take_control() and not b.take_control()
    with pytest.raises(ControlRefused, match="camera GUI A"):
        b.set_stabilize(True)
    with pytest.raises(ControlRefused):
        b.set_config({})
    b.kill_af()                                    # safety: always
    assert b.get_config() and b.info()
    assert b.camera_features() is not None         # a read without a get_ prefix
    a.set_tracking(False)                          # the holder may


def test_status_carries_control_and_the_client_follows_it(svc, clients):
    a = clients(name="camera GUI A")
    b = clients(name="camera GUI B")
    a.take_control()
    assert _wait(lambda: (b.control() or {}).get("holder") is not None)
    assert b.control()["holder"]["name"] == "camera GUI A"
    assert not b.has_control() and a.has_control()


def test_the_camera_is_a_machine_client_of_kim():
    from camera.backends.remote_kim import KimLink
    link = KimLink()
    assert link.identity["kind"] == "machine" and link.identity["name"] == "camera"


# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(qapp, s=0.3):
    t0 = time.monotonic()
    while time.monotonic() - t0 < s:
        qapp.processEvents()
        time.sleep(0.02)


def test_gui_viewer_blocks_find_focus_but_not_kill_af(qapp, svc, clients, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton

    from camera.apps.gui import MainWindow
    a = clients(name="camera GUI A")
    b = clients(name="camera GUI B")
    wa = MainWindow(a, Config(), remote=True)
    wb = MainWindow(b, Config(), remote=True)
    wa.show(); wb.show()
    _pump(qapp)
    try:
        assert a.has_control() and not wa._control_bar.viewer
        assert wb._control_bar.viewer and "camera GUI A" in wb._control_bar.label.text()
        calls = []
        monkeypatch.setattr(b, "autofocus", lambda: calls.append("af") or 1)
        monkeypatch.setattr(b, "kill_af", lambda: calls.append("kill"))
        buttons = {x.text(): x for x in wb.findChildren(QPushButton)}
        QTest.mouseClick(buttons["Find focus"], Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == []
        QTest.mouseClick(buttons["Kill AF"], Qt.MouseButton.LeftButton)
        _pump(qapp, 0.1)
        assert calls == ["kill"]
    finally:
        wa.close(); wb.close()
