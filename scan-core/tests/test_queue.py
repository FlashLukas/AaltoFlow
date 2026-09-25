"""A QUEUE of scans: load several definitions, name/order/prune them, run them.

Rules (Lukas, 2026-09-24): each scan is its own file; Abort skips the CURRENT
scan and the next one starts; an ERROR stops the queue; "Stop queue" ends it.
A queue can be loaded from several files at once or from a saved queue file,
which holds the recipes inline (a snapshot).
"""

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan_core import Recipe                                          # noqa: E402
from scan_core import scan_queue                                      # noqa: E402
from scan_core.registry import Gettable, Registry, Settable           # noqa: E402


def _recipe(name, values, param="a"):
    return Recipe(name=name, axes=[{"type": "array", "param": param, "values": values}],
                  detectors=["d"])


# ─────────────────────────────── the file layer ───────────────────────────────

def test_load_definitions_names_and_order(tmp_path):
    _recipe("scan", [1, 2]).save(tmp_path / "field_map.yaml")     # default name -> stem
    _recipe("rf sweep", [3]).save(tmp_path / "other.yaml")        # own name wins
    entries = scan_queue.load_definitions([tmp_path / "field_map.yaml",
                                           tmp_path / "other.yaml"])
    assert [e.name for e in entries] == ["field_map", "rf sweep"]
    assert entries[0].source.endswith("field_map.yaml")


def test_an_autosaved_nc_is_named_without_its_time(tmp_path):
    import xarray as xr
    ds = xr.Dataset(attrs={"recipe_json": _recipe("scan", [1]).to_json()})
    ds.to_netcdf(tmp_path / "134501_islands.nc")
    [e] = scan_queue.load_definitions([tmp_path / "134501_islands.nc"])
    assert e.name == "islands"


def test_queue_file_round_trip_is_a_snapshot(tmp_path):
    src = tmp_path / "a.yaml"
    _recipe("A", [1, 2]).save(src)
    entries = scan_queue.load_definitions([src])
    entries[0].name = "renamed"
    q = tmp_path / "q.yaml"
    scan_queue.save_queue_file(q, entries + entries)
    _recipe("A", [9, 9, 9]).save(src)                 # edited afterwards...
    assert scan_queue.is_queue_file(q) and not scan_queue.is_queue_file(src)
    back = scan_queue.load_definitions([q])
    assert [e.name for e in back] == ["renamed", "renamed"]
    assert back[0].recipe.axes[0]["values"] == [1, 2]  # ...the queue kept its copy
    assert back[0].recipe.name == "renamed"


def test_queue_file_may_reference_recipes_by_relative_path(tmp_path):
    (tmp_path / "sub").mkdir()
    _recipe("B", [5]).save(tmp_path / "sub" / "b.yaml")
    (tmp_path / "q.yaml").write_text(
        "queue:\n  - name: first\n    recipe: sub/b.yaml\n  - recipe: sub/b.yaml\n",
        encoding="utf-8")
    entries = scan_queue.load_definitions([tmp_path / "q.yaml"])
    assert [e.name for e in entries] == ["first", "B"]


def test_an_unreadable_file_is_named(tmp_path):
    (tmp_path / "bad.yaml").write_text("axes: [", encoding="utf-8")
    with pytest.raises(ValueError, match="bad.yaml"):
        scan_queue.load_definitions([tmp_path / "bad.yaml"])


def test_validate_queue_checks_every_entry():
    reg, _ = _registry()
    entries = [scan_queue.QueueEntry("ok", _recipe("x", [1])),
               scan_queue.QueueEntry("bad", _recipe("x", [1], param="gone")),
               scan_queue.QueueEntry(" ", _recipe("x", [1]))]
    probs = scan_queue.validate_queue(entries, reg)
    assert probs[0] == []
    assert any("gone" in m for m in probs[1])
    assert probs[2] == ["no name"]


# ─────────────────────────────── running it ───────────────────────────────────

def _registry(builder_ref=None):
    """Settable a: 99 presses Abort, 666 raises. Detector d reads a."""
    state, seen = {"a": 0.0}, []

    def set_a(v):
        if v == 666:
            raise RuntimeError("magnet quenched")
        if v == 99 and builder_ref:
            builder_ref[0]._abort()
        state["a"] = v
        seen.append(v)

    reg = Registry()
    reg.add(Settable("a", "A", "", (-1000, 1000), set_a, lambda: state["a"]))
    reg.add(Gettable("d", "D", "", lambda: state["a"]))
    return reg, seen


@pytest.fixture
def qbuilder(tmp_path):
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    from apps.scan_builder import ScanBuilder
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ref = [None]
    reg, seen = _registry(ref)
    win = ScanBuilder(reg)
    ref[0] = win
    win.autosave_dir = str(tmp_path)
    win.ask_before_unsaved = False
    win.seen = seen
    yield win
    win.close()


def _wait(builder, timeout=20.0):
    from PySide6 import QtWidgets
    t0 = time.monotonic()
    while builder.queue_running() and time.monotonic() - t0 < timeout:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)
    for _ in range(20):
        QtWidgets.QApplication.processEvents()
    assert not builder.queue_running(), "queue did not finish"


def _entries(*specs):
    return [scan_queue.QueueEntry(n, _recipe("x", v)) for n, v in specs]


def test_runs_every_scan_into_its_own_file(qbuilder, tmp_path):
    assert qbuilder.run_queue(_entries(("one", [1, 2]), ("two", [3]), ("two", [4])))
    _wait(qbuilder)
    assert qbuilder.queue_results == [("one", "done"), ("two", "done"), ("two", "done")]
    files = sorted(p.name for p in tmp_path.rglob("*.nc"))
    # same name within one second -> a counter, not an overwrite
    assert len(files) == 3 and sum("_two" in f for f in files) == 2
    assert qbuilder.queue_lbl.text() == "Queue finished: 3 done"
    assert qbuilder.run_btn.isEnabled() and qbuilder.stop_queue_btn.isHidden()
    assert any(m.startswith("queue: scan 2 of 3 'two' started") for m in qbuilder.run_log)
    import xarray as xr
    with xr.open_dataset(next(tmp_path.rglob("*_one.nc"))) as ds:
        assert ds.attrs["name"] == "one"


def test_abort_skips_to_the_next_scan(qbuilder):
    qbuilder.run_queue(_entries(("first", [1]), ("aborted", [2, 99, 5, 6]), ("third", [7])))
    _wait(qbuilder)
    assert qbuilder.queue_results == [("first", "done"), ("aborted", "aborted"),
                                      ("third", "done")]
    assert 7 in qbuilder.seen and 6 not in qbuilder.seen


def test_an_error_stops_the_queue(qbuilder):
    qbuilder.run_queue(_entries(("first", [1]), ("broken", [2, 666]), ("never", [7])))
    _wait(qbuilder)
    assert qbuilder.queue_results == [("first", "done"), ("broken", "error")]
    assert 7 not in qbuilder.seen
    assert "1 not run" in qbuilder.queue_lbl.text()
    assert "magnet quenched" in qbuilder.queue_lbl.text()


def test_stop_queue_ends_it(qbuilder):
    from PySide6 import QtWidgets
    qbuilder.run_queue(_entries(("long", list(range(100, 500))), ("never", [7])))
    t0 = time.monotonic()
    while len(qbuilder.seen) < 3 and time.monotonic() - t0 < 10:
        QtWidgets.QApplication.processEvents()
    qbuilder.stop_queue()
    _wait(qbuilder)
    assert qbuilder.queue_results == [("long", "aborted")]
    assert 7 not in qbuilder.seen
    assert "stopped by the operator" in qbuilder.queue_lbl.text()


def test_an_invalid_queue_does_not_start(qbuilder):
    ok = qbuilder.run_queue(_entries(("fine", [1]))
                            + [scan_queue.QueueEntry("bad", _recipe("x", [1], "gone"))])
    assert not ok and not qbuilder.queue_running()
    assert "queue NOT started" in qbuilder.detail.text()
    assert qbuilder.seen == []


def test_the_editor_definition_is_left_alone(qbuilder):
    qbuilder.add_axis("a")
    before = qbuilder.build_recipe().to_dict()
    qbuilder.run_queue(_entries(("q", [1])))
    _wait(qbuilder)
    assert qbuilder.build_recipe().to_dict() == before


# ─────────────────────────────── the dialog ─────────────────────────────────

def test_dialog_rename_reorder_delete(qbuilder):
    from apps.scan_builder import QueueDialog
    dlg = QueueDialog(_entries(("a", [1]), ("b", [1, 2]), ("c", [1])), qbuilder.registry, 1.0)
    assert "3 scans  ·  4 points" in dlg.total.text()
    dlg.list.item(0).setText("renamed")
    dlg.list.setCurrentRow(2); dlg._move(-1)
    dlg.list.setCurrentRow(0); dlg._delete()
    assert [e.name for e in dlg.entries()] == ["c", "b"]
    assert dlg.run_btn.isEnabled()
    dlg.list.item(0).setText("renamed")
    assert dlg.entries()[0].name == "renamed"


def test_dialog_refuses_an_invalid_entry(qbuilder):
    from apps.scan_builder import QueueDialog
    entries = _entries(("a", [1])) + [scan_queue.QueueEntry("bad", _recipe("x", [1], "gone"))]
    dlg = QueueDialog(entries, qbuilder.registry)
    assert not dlg.run_btn.isEnabled()
    assert "1 cannot run" in dlg.total.text()
    dlg.list.setCurrentRow(1)
    assert "gone" in dlg.detail.text()
    dlg._delete()
    assert dlg.run_btn.isEnabled()
