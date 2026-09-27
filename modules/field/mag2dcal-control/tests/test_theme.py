"""The theme mechanism: set_theme must mutate the SAME COLORS dict in place
(so `from .theme import COLORS` elsewhere keeps seeing live values), and
build_stylesheet must reflect the active palette. No Qt needed for these."""

from mag2dcal.apps import theme
from mag2dcal.apps.theme import COLORS, DARK, LIGHT, set_theme, build_stylesheet


def test_default_is_dark():
    set_theme("dark")
    assert COLORS["bg"] == DARK["bg"]


def test_set_theme_mutates_in_place():
    # the dict OBJECT must stay the same across a theme switch
    before = id(COLORS)
    set_theme("light")
    assert id(COLORS) == before                 # not rebound
    assert id(theme.COLORS) == before           # module attr is the same object too
    assert COLORS["bg"] == LIGHT["bg"]
    assert COLORS["accent"] == LIGHT["accent"]
    set_theme("dark")                           # restore
    assert COLORS["bg"] == DARK["bg"]


def test_unknown_and_empty_fall_back_to_dark():
    set_theme("bogus"); assert COLORS["bg"] == DARK["bg"]
    set_theme(""); assert COLORS["bg"] == DARK["bg"]
    set_theme(None); assert COLORS["bg"] == DARK["bg"]


def test_build_stylesheet_follows_active_palette():
    set_theme("light")
    css = build_stylesheet()
    assert LIGHT["bg"] in css
    assert LIGHT["code_bg"] in css              # log/code area uses code_bg
    assert LIGHT["pressed"] in css              # pressed state uses pressed
    set_theme("dark")
    css = build_stylesheet()
    assert DARK["bg"] in css
    assert DARK["code_bg"] in css
