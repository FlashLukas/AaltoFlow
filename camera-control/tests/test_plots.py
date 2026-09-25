"""MiniPlot axis maths: round ticks and readable labels (no display needed)."""

import pytest

pytest.importorskip("PySide6")

from camera.apps.plots import fmt_tick, nice_ticks  # noqa: E402


@pytest.mark.parametrize("lo, hi, expect", [
    (0, 255, [0, 50, 100, 150, 200, 250]),
    (0, 5.3, [0, 1, 2, 3, 4, 5]),
    (-30, 0, [-30, -25, -20, -15, -10, -5, 0]),
    (19.0, 23.0, [19, 20, 21, 22, 23]),
    (0.0012, 0.0031, [0.0015, 0.002, 0.0025, 0.003]),
])
def test_nice_ticks_are_round_and_inside(lo, hi, expect):
    ticks = nice_ticks(lo, hi, 5)
    assert ticks == pytest.approx(expect)
    assert all(lo - 1e-12 <= t <= hi + 1e-12 for t in ticks)


def test_nice_ticks_degenerate_range():
    assert nice_ticks(3.0, 3.0) == [3.0]


def test_tick_labels_read_well():
    assert fmt_tick(250, 50) == "250"
    assert fmt_tick(0.0025, 0.0005) == "0.0025"
    assert fmt_tick(2.5, 0.5) == "2.5"
    assert fmt_tick(20000, 5000) == "20 k"
    assert fmt_tick(2_000_000, 500_000) == "2.0 M"
    assert "e" not in fmt_tick(2_121_856, 1_000_000)     # never scientific notation
