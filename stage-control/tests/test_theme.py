"""The light/dark theme mechanism: in-place palette swap + config round-trip.

None of this needs PySide6 (the theme module imports Qt lazily inside
apply_palette), so it runs in the headless core suite.
"""

from stage.apps import theme
from stage.config import Config, load_config, save_config
from stage.net.protocol import apply_config_dict, config_to_dict


def test_set_theme_mutates_colors_in_place():
    """set_theme must clear()+update() the SAME dict object, so importers that
    captured `COLORS` keep seeing the active values (never rebound)."""
    captured = theme.COLORS  # what `from .theme import COLORS` would have grabbed
    theme.set_theme("light")
    try:
        assert theme.COLORS is captured  # same object, mutated in place
        assert captured["bg"] == theme.LIGHT["bg"]
        assert captured["accent"] == theme.LIGHT["accent"]
        theme.set_theme("dark")
        assert captured["bg"] == theme.DARK["bg"]
    finally:
        theme.set_theme("dark")  # leave the module in its default state


def test_unknown_theme_falls_back_to_dark():
    theme.set_theme("chartreuse")
    try:
        assert theme.COLORS["bg"] == theme.DARK["bg"]
    finally:
        theme.set_theme("dark")


def test_build_stylesheet_follows_active_palette():
    theme.set_theme("light")
    try:
        sheet = theme.build_stylesheet()
        assert theme.LIGHT["bg"] in sheet
        assert theme.LIGHT["code_bg"] in sheet   # log/plot areas use code_bg
        assert theme.DARK["bg"] not in sheet
    finally:
        theme.set_theme("dark")


def test_palettes_share_keys():
    """Every key must exist in both palettes, or a widget goes unstyled in one."""
    assert set(theme.DARK) == set(theme.LIGHT)


def test_ui_theme_roundtrips_through_ini(tmp_path):
    cfg = Config()
    cfg.ui.theme = "light"
    path = tmp_path / "stage.ini"
    save_config(cfg, str(path))
    back = load_config(str(path))
    assert back.ui.theme == "light"


def test_ui_theme_roundtrips_over_the_wire():
    cfg = Config()
    cfg.ui.theme = "light"
    d = config_to_dict(cfg)
    assert d["ui"]["theme"] == "light"

    target = Config()  # defaults to dark
    apply_config_dict(target, d)
    assert target.ui.theme == "light"
