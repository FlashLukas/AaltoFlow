"""The 20-slot position list (step coordinates, two axes)."""

import pytest

from agilis.positions import N_SLOTS, PositionList


def test_store_save_load_roundtrip(tmp_path):
    pl = PositionList()
    pl.store(3, 120, -45, name="corner")
    path = tmp_path / "p.json"
    pl.save(str(path))
    other = PositionList()
    other.load(str(path))
    p = other.get(3)
    assert p.used and p.name == "corner" and (p.x, p.y) == (120, -45)
    assert len(other.slots) == N_SLOTS
    assert not other.get(0).used


def test_bad_slot_raises():
    with pytest.raises(IndexError):
        PositionList().store(N_SLOTS, 0, 0)
