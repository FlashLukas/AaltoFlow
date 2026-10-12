"""Recipes are UTF-8 on every PC (docs/DEVELOPER_NOTES.md gotcha #27)."""


def test_a_recipe_comment_survives_a_round_trip_in_utf8(tmp_path):
    """Found by the viewer's screenshot: Recipe.load used the Windows code page,
    so '—' came back as 'â€”' and went into every measurement file."""
    from scan_core import Recipe
    p = tmp_path / "r.yaml"
    p.write_text("name: t\ncomment: field — frequency, 5 µm\naxes: []\ndetectors: []\n",
                 encoding="utf-8")
    r = Recipe.load(p)
    assert r.comment == "field — frequency, 5 µm"
    r.save(tmp_path / "r2.yaml")
    assert Recipe.load(tmp_path / "r2.yaml").comment == r.comment


def test_an_old_settle_block_is_dropped_on_load(tmp_path):
    """`settle: {default_timeout_s}` was written into every definition until
    2026-10-12 and read by nothing (a knob waits for its module's describe
    timeout_s, 60 s when undeclared). Lukas: remove it. An old definition
    still loads; saved again it has no settle block."""
    from scan_core import Recipe
    p = tmp_path / "old.yaml"
    p.write_text("name: t\naxes: []\ndetectors: []\n"
                 "settle: {default_timeout_s: 20.0}\n", encoding="utf-8")
    r = Recipe.load(p)
    assert not hasattr(r, "settle")
    assert "settle" not in r.to_dict()
    assert "settle" not in Recipe().to_dict()
