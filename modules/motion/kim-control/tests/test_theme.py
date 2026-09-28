"""Theme palette: set_theme mutates COLORS in place; stylesheet follows it.

No Qt needed for these -- set_theme / build_stylesheet operate on the plain
COLORS dict (apply_palette is the only Qt-touching helper).
"""

from kim.apps import theme
from kim.apps.theme import COLORS  # a live reference, like the GUI modules use


def _restore():
    theme.set_theme("dark")


def test_set_theme_mutates_in_place_not_rebinds():
    # the module-level dict object must stay the SAME object so `from .theme
    # import COLORS` importers keep seeing the active values
    before_id = id(theme.COLORS)
    imported_ref = COLORS  # captured at import time (like other modules do)

    theme.set_theme("light")
    assert id(theme.COLORS) == before_id            # same object, mutated
    assert imported_ref is theme.COLORS             # importer sees it too
    assert imported_ref["bg"] == theme.LIGHT["bg"]  # ...with light values

    theme.set_theme("dark")
    assert imported_ref["bg"] == theme.DARK["bg"]
    _restore()


def test_light_and_dark_differ_and_are_complete():
    theme.set_theme("dark")
    dark_bg, dark_text = COLORS["bg"], COLORS["text"]
    theme.set_theme("light")
    assert COLORS["bg"] != dark_bg
    assert COLORS["text"] != dark_text
    # both palettes carry the same keys
    assert set(theme.DARK) == set(theme.LIGHT)
    # every key the stylesheet references exists in both
    for key in theme.DARK:
        assert key in theme.LIGHT
    _restore()


def test_build_stylesheet_reflects_active_palette():
    theme.set_theme("dark")
    dark_qss = theme.build_stylesheet()
    assert theme.DARK["bg"] in dark_qss
    theme.set_theme("light")
    light_qss = theme.build_stylesheet()
    assert theme.LIGHT["bg"] in light_qss
    assert theme.DARK["bg"] not in light_qss   # no dark colour leaked through
    _restore()


def test_unknown_theme_falls_back_to_dark():
    theme.set_theme("chartreuse")
    assert COLORS["bg"] == theme.DARK["bg"]
    theme.set_theme(None)
    assert COLORS["bg"] == theme.DARK["bg"]
    _restore()
