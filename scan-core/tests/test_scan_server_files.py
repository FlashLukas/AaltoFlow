"""A scan definition from ANOTHER PC may not read files on the server's PC
(security review 2026-11-26); the client puts a file axis's values in itself."""

from __future__ import annotations

from scan_core.scan_server import ScanServer, recipe_file_refs
from scan_core.scan_server_client import inline_files


def _recipe(path):
    return {"name": "t", "axes": [{"type": "file", "param": "field", "path": str(path)}],
            "detectors": ["lockin_r"]}


def test_file_references_are_found():
    assert recipe_file_refs(_recipe("C:/secret.txt"))
    assert recipe_file_refs({"axes": [{"type": "zip", "members": [
        {"param": "a", "path": "x.txt"}]}]})
    assert recipe_file_refs({"axes": [], "scout": {"from": "mask.png"}})
    assert not recipe_file_refs({"axes": [{"type": "linear", "param": "f",
                                           "start": 0, "stop": 1, "num": 3}]})


def test_a_definition_from_another_pc_that_reads_a_file_is_refused(tmp_path):
    f = tmp_path / "values.txt"
    f.write_text("1\n2\n3\n", encoding="utf-8")
    r = ScanServer._parse_entries([{"recipe": _recipe(f)}], {}, remote=True)
    assert isinstance(r, dict) and r["refused"] == "own_pc"
    ok = ScanServer._parse_entries([{"recipe": _recipe(f)}], {}, remote=False)
    assert isinstance(ok, list)                    # the own PC: as before


def test_the_client_puts_the_values_in(tmp_path):
    f = tmp_path / "values.txt"
    f.write_text("1\n2\n3.5\n", encoding="utf-8")
    d = inline_files(_recipe(f))
    ax = d["axes"][0]
    assert "path" not in ax and ax["type"] == "array" and ax["values"] == [1.0, 2.0, 3.5]
    assert not recipe_file_refs(d)
    assert isinstance(ScanServer._parse_entries([{"recipe": d}], {}, remote=True), list)
