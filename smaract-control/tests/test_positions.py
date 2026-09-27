"""PositionList unit tests (section 9)."""

from smaract.positions import N_SLOTS, PositionList


def test_defaults_empty():
    pl = PositionList()
    assert len(pl.slots) == N_SLOTS
    assert all(not p.used for p in pl.slots)


def test_store_clear_and_default_name():
    pl = PositionList()
    pl.store(3, 12.5, name="x")
    assert pl.get(3).used and pl.get(3).name == "x" and pl.get(3).position_mm == 12.5
    pl.clear(3)
    assert not pl.get(3).used
    pl.store(7, 0.0)
    assert pl.get(7).name == "P07"


def test_save_load(tmp_path):
    pl = PositionList()
    pl.store(0, -9.25, "corner")
    path = tmp_path / "p.json"
    pl.save(str(path))
    pl2 = PositionList()
    pl2.load(str(path))
    assert pl2.get(0).name == "corner" and pl2.get(0).position_mm == -9.25
    assert not pl2.get(1).used
