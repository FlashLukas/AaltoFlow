"""The catalogue's double-click (apps/catalogue.py open_in_viewer) reuses the
viewer window that is already running instead of starting a viewer per file.

A fake viewer listens on a test-only name (AALTOVIEW_INSTANCE_NAME, so a real
viewer on this PC is never touched). It runs in a CHILD process: the handover
blocks while it waits for the answer, so a listener in this process could not
answer it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("aaltoview.apps.single_instance")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps import catalogue  # noqa: E402

# a stand-in for a running viewer: answers "ok" to the first request and
# writes what it was sent to a file
FAKE_VIEWER = r"""
import sys
from PySide6 import QtCore, QtNetwork
name, out = sys.argv[1], sys.argv[2]
app = QtCore.QCoreApplication([])
server = QtNetwork.QLocalServer()
server.listen(name)
print("listening", flush=True)

def connected():
    sock = server.nextPendingConnection()
    def read():
        if sock.canReadLine():
            with open(out, "w", encoding="utf-8") as f:
                f.write(bytes(sock.readLine()).decode("utf-8"))
            sock.write(b"ok\n")
            sock.flush()
            sock.waitForBytesWritten(1000)
            QtCore.QTimer.singleShot(200, app.quit)
    sock.readyRead.connect(read)

server.newConnection.connect(connected)
QtCore.QTimer.singleShot(15000, app.quit)
app.exec()
"""


@pytest.fixture
def name(monkeypatch):
    n = f"AaltoView-test-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("AALTOVIEW_INSTANCE_NAME", n)
    return n


def _no_new_viewer(*a, **kw):
    raise AssertionError(f"a viewer process was started: {a}")


def test_a_double_click_goes_to_the_running_viewer(name, tmp_path, monkeypatch):
    out = tmp_path / "received.json"
    fake = subprocess.Popen([sys.executable, "-c", FAKE_VIEWER, name, str(out)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert fake.stdout.readline().strip() == "listening"
        monkeypatch.setattr(catalogue.subprocess, "Popen", _no_new_viewer)
        run = tmp_path / "2026-10-06" / "120000_scan.nc"
        t = time.perf_counter()
        assert catalogue.open_in_viewer(run) is None           # handed over
        assert time.perf_counter() - t < 0.6                    # the GUI thread waits this
        fake.wait(10)
    finally:
        fake.kill()
    assert Path(json.loads(out.read_text(encoding="utf-8"))["open"]) == run.resolve()


def test_with_no_viewer_running_one_is_started(name, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(catalogue.subprocess, "Popen",
                        lambda cmd, **kw: started.append(cmd) or "process")
    run = tmp_path / "scan.nc"
    t = time.perf_counter()
    assert catalogue.open_in_viewer(run) == "process"
    assert time.perf_counter() - t < 0.5                        # nobody there: no wait
    assert started == [[sys.executable, str(catalogue.VIEWER), str(run)]]
