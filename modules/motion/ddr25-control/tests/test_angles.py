"""The wrap arithmetic -- the one place an angle becomes a controller target."""

import pytest

from ddr25.angles import AngleList, display_angle, raw_target, wrap360


def test_wrap360():
    assert wrap360(370) == 10
    assert wrap360(-10) == 350
    assert wrap360(360) == 0
    assert 0 <= wrap360(-1e-15) < 360


def test_display_angle():
    assert display_angle(370.0, 0.0, "literal") == 370.0
    assert display_angle(370.0, 0.0, "shortest") == pytest.approx(10.0)
    assert display_angle(5.0, 10.0, "shortest") == pytest.approx(355.0)


def test_literal_is_linear():
    assert raw_target("literal", 350.0, 0.0, 10.0) == 10.0       # back 340
    assert raw_target("literal", 0.0, 5.0, 10.0) == 15.0         # zero offset


@pytest.mark.parametrize("now,want,expect", [
    (350.0, 10.0, 370.0),      # +20 across the seam
    (10.0, 350.0, -10.0),      # -20 across the seam
    (720.0 + 90.0, 100.0, 820.0),   # third turn: stays in its turn
    (0.0, 180.0, -180.0),      # exact tie goes negative
])
def test_shortest(now, want, expect):
    assert raw_target("shortest", now, 0.0, want) == pytest.approx(expect)


def test_positive_and_negative():
    assert raw_target("positive", 10.0, 0.0, 350.0) == pytest.approx(350.0)
    assert raw_target("positive", 350.0, 0.0, 10.0) == pytest.approx(370.0)
    assert raw_target("negative", 350.0, 0.0, 10.0) == pytest.approx(10.0)
    assert raw_target("negative", 10.0, 0.0, 350.0) == pytest.approx(-10.0)
    # already there: no move in any policy
    for p in ("shortest", "positive", "negative"):
        assert raw_target(p, 45.0, 0.0, 45.0) == pytest.approx(45.0)


def test_modulo_respects_zero():
    # zero at controller 100: angle 0 means controller 100 (+/- turns)
    assert raw_target("shortest", 90.0, 100.0, 0.0) == pytest.approx(100.0)


def test_unknown_policy_raises():
    with pytest.raises(ValueError):
        raw_target("sideways", 0.0, 0.0, 1.0)


def test_angle_list_roundtrip(tmp_path):
    al = AngleList()
    al.store(2, 123.5, "p-pol")
    path = tmp_path / "a.json"
    al.save(str(path))
    other = AngleList()
    other.load(str(path))
    assert other.get(2).used and other.get(2).raw == 123.5 and other.get(2).name == "p-pol"
    assert not other.get(0).used
    with pytest.raises(IndexError):
        al.store(99, 0.0)
