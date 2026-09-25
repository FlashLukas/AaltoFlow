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
