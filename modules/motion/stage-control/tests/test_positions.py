"""PositionList unit tests (§9)."""

from stage.positions import N_SLOTS, PositionList


def test_defaults_empty():
    pl = PositionList()
    assert len(pl.slots) == N_SLOTS
    assert all(not p.used for p in pl.slots)


def test_store_clear():
    pl = PositionList()
    pl.store(3, 1.0, 2.0, 3.0, name="x")
    assert pl.get(3).used
    assert pl.get(3).name == "x"
    pl.clear(3)
    assert not pl.get(3).used


def test_store_default_name():
    pl = PositionList()
    pl.store(7, 0, 0, 0)
    assert pl.get(7).name == "P07"


def test_roundtrip_list():
    pl = PositionList()
    pl.store(0, 1, 2, 3, "a")
    data = pl.to_list()
    pl2 = PositionList()
    pl2.from_list(data)
    assert pl2.get(0).name == "a"
    assert pl2.get(0).x == 1


def test_save_load(tmp_path):
    pl = PositionList()
    pl.store(0, 9, 8, 7, "corner")
    path = tmp_path / "p.json"
    pl.save(str(path))
    pl2 = PositionList()
    pl2.load(str(path))
    assert pl2.get(0).name == "corner"
    assert pl2.get(0).z == 7
