"""suite_common.fileio.replace_retry: a rename Windows refuses for a moment
(virus scanner, indexer) is retried; a lasting refusal still raises."""

import os

import pytest

from suite_common import fileio


def test_a_brief_refusal_is_retried(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a.nc"
    src.write_text("x")
    real, calls = os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real(a, b)
    monkeypatch.setattr(fileio.os, "replace", flaky)
    fileio.replace_retry(src, dst)
    assert dst.read_text() == "x" and len(calls) == 3


def test_a_lasting_refusal_still_raises(tmp_path, monkeypatch):
    def never(a, b):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(fileio.os, "replace", never)
    with pytest.raises(PermissionError):
        fileio.replace_retry(tmp_path / "a", tmp_path / "b", retry_s=0.1)
