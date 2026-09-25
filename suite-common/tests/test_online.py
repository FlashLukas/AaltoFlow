"""The online catalog: a release (catalog.json + packs) served over HTTP.

Lukas, 2026-09-24: "release downloads is good way. I will make the repo public
soon." GitHub serves a release as plain files, so a local HTTP server holding
the same files is a faithful stand-in -- and so is a folder on a share, which is
exactly how a lab mirror works.
"""

import http.server
import json
import threading
import zipfile
from functools import partial
from pathlib import Path

import pytest

from suite_common import catalog as K
from suite_common import modules as M

from test_modules import make_module


def _pack(src_root: Path, folder: str, dest: Path) -> Path:
    """A code-only pack of one made-up module (what pack_module.py makes)."""
    with zipfile.ZipFile(dest, "w") as zf:
        for p in (src_root / folder).rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src_root).as_posix())
    return dest


@pytest.fixture
def release(tmp_path):
    """A release folder: two packs + catalog.json, like release_modules.py makes."""
    src = tmp_path / "src"
    make_module(src, "pm-control", "pm", 5601)
    make_module(src, "kim-control", "kim", 5567)
    for key in ("pm", "kim"):
        (src / f"{key}-control" / "pyproject.toml").write_text(
            f'[project]\nname = "{key}"\nversion = "2.0"\n', encoding="utf-8")
    mods = M.discover_local(src)[0]
    rel = tmp_path / "release"
    rel.mkdir()
    assets = {m.key: {"code": _pack(src, m.dir.name, rel / f"{m.key}-1.0-abc.zip")}
              for m in mods}
    doc = K.release_catalog(mods, assets, "abc1234", "modules-2026.09.24-abc1234", None)
    (rel / K.CATALOG_FILE).write_text(json.dumps(doc), encoding="utf-8")
    return rel


@pytest.fixture
def server(release):
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(release))
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


def test_fetch_the_catalog_and_download_a_verified_pack(server, tmp_path):
    doc = K.fetch_catalog(server + "catalog.json")
    assert doc["release"] == "modules-2026.09.24-abc1234"
    entry = next(e for e in doc["modules"] if e["key"] == "pm")
    dl = entry["downloads"]["code"]
    url = K.asset_url(doc, dl["file"])
    assert url == server + dl["file"]                    # relative to the catalog
    seen = []
    out = K.download(url, tmp_path / dl["file"], dl["sha256"], dl["size"],
                     progress=lambda d, t: seen.append((d, t)))
    assert out.stat().st_size == dl["size"] and seen[-1] == (dl["size"], dl["size"])
    with K.ModuleSource(out) as src:                   # and it is an installable pack
        assert [m.key for m in src.modules] == ["pm"]


def test_a_download_that_does_not_match_its_checksum_is_thrown_away(server, tmp_path):
    doc = K.fetch_catalog(server + "catalog.json")
    dl = doc["modules"][0]["downloads"]["code"]
    dest = tmp_path / "downloads"
    dest.mkdir()
    with pytest.raises(K.CatalogError, match="checksum mismatch"):
        K.download(K.asset_url(doc, dl["file"]), dest / "x.zip", "0" * 64)
    assert list(dest.iterdir()) == []                    # no file, no .part left


def test_not_found_says_what_to_check(server):
    with pytest.raises(K.CatalogError, match="Is the repository public"):
        K.fetch_catalog(server + "nothing-here.json")


def test_no_network_says_use_a_pack():
    with pytest.raises(K.CatalogError, match="module pack"):
        K.fetch_catalog("http://127.0.0.1:9/catalog.json", timeout=2)


def test_a_folder_on_a_share_works_as_a_mirror(release, tmp_path):
    """Copy a release folder anywhere and point at its catalog.json -- as a
    Windows path, not even a URL. The downloads resolve next to it."""
    doc = K.fetch_catalog(str(release / K.CATALOG_FILE))
    dl = doc["modules"][0]["downloads"]["code"]
    out = K.download(K.asset_url(doc, dl["file"]), tmp_path / dl["file"], dl["sha256"])
    assert K.file_digest(out) == dl["sha256"]


def test_something_else_is_not_mistaken_for_a_catalog(release):
    (release / "other.json").write_text('{"format": "something/1"}')
    with pytest.raises(K.CatalogError, match="not an AaltoFlow catalog"):
        K.fetch_catalog(str(release / "other.json"))


def test_catalog_entries_plan_like_modules_on_disk(release, tmp_path):
    """Before downloading anything, the wizard can say new / update / ports --
    and the placeholder folder can never be taken for an installed copy."""
    doc = K.fetch_catalog(str(release / K.CATALOG_FILE))
    root = tmp_path / "suite"
    make_module(root, "kim-control", "kim", 5567)
    make_module(root, "x-control", "x", 5601)          # takes pm's ports
    specs = [K.spec_from_entry(e) for e in doc["modules"]]
    plans = {p.spec.key: p for p in K.plan_install(specs, root)}
    assert plans["kim"].action == "update"
    assert plans["kim"].reason.startswith("replaces version ? with 2.0")   # from the catalog
    assert plans["pm"].action == "new" and plans["pm"].ports == (5555, 5556)
    assert not specs[0].dir.exists()


def test_release_catalog_lists_downloads_and_drops_modules_without(release):
    doc = json.loads((release / K.CATALOG_FILE).read_text(encoding="utf-8"))
    assert doc["format"] == K.CATALOG_FORMAT and doc["commit"] == "abc1234"
    for e in doc["modules"]:
        assert set(e["downloads"]) == {"code"}
        assert len(e["downloads"]["code"]["sha256"]) == 64
