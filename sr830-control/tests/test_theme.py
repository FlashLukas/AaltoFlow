"""The theme mechanism: set_theme mutates the SAME COLORS dict in place, and
both palettes carry the same keys (including sr830's own `quad`)."""

from sr830.apps import theme
from sr830.apps.theme import COLORS, DARK, LIGHT, set_theme, build_stylesheet


def test_palettes_have_identical_keys():
    assert set(DARK) == set(LIGHT)
    assert "quad" in DARK


def test_set_theme_mutates_in_place():
    before = id(COLORS)
    set_theme("light")
    assert id(COLORS) == before and id(theme.COLORS) == before
    assert COLORS["quad"] == LIGHT["quad"]
    set_theme("dark")
    assert COLORS["bg"] == DARK["bg"]


def test_unknown_falls_back_to_dark():
    set_theme("bogus")
    assert COLORS["bg"] == DARK["bg"]
    set_theme(None)
    assert COLORS["bg"] == DARK["bg"]


def test_stylesheet_follows_active_palette():
    set_theme("light")
    assert LIGHT["bg"] in build_stylesheet()
    set_theme("dark")
    assert DARK["bg"] in build_stylesheet()
