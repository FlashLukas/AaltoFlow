"""Sending a run to eLabFTW (scan_core/eln.py), against a FAKE eLabFTW.

The fake is a tiny http.server in a thread on a non-default local port. It
checks the Authorization header like the real one (401 otherwise), records
every request, and answers the few endpoints eln.py uses the way the API v2
spec says (201 + Location for creations). No real instance is contacted.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_core import eln                                         # noqa: E402
from test_catalogue import _write                                 # noqa: E402

GOOD_KEY = "3-0123456789abcdef-test-key"


class FakeElab:
    """Records requests; `fail` maps (method, path regex) -> status."""

    def __init__(self, key=GOOD_KEY):
        self.key = key
        self.requests: list[dict] = []
        self.fail: dict = {}
        self.next_id = 41
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):          # keep the test output clean
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                rec = {"method": self.command, "path": self.path,
                       "auth": self.headers.get("Authorization"),
                       "ctype": self.headers.get("Content-Type", ""), "body": body}
                fake.requests.append(rec)
                for (m, rx), status in fake.fail.items():
                    if m == self.command and re.search(rx, self.path):
                        return self._send(status, {"description": "forced failure"})
                if rec["auth"] != fake.key:
                    return self._send(401, {"code": 401, "description": "No access token provided!"})
                p = self.path
                if self.command == "GET" and p == "/api/v2/users/me":
                    return self._send(200, {"userid": 7, "fullname": "Test Person",
                                            "team_name": "NanoSpin"})
                if self.command == "POST" and p == "/api/v2/experiments":
                    fake.next_id += 1
                    return self._send(201, None,
                                      {"Location": f"http://x/api/v2/experiments/{fake.next_id}"})
                if self.command == "PATCH" and re.fullmatch(r"/api/v2/experiments/\d+", p):
                    return self._send(200, {"id": int(p.rsplit("/", 1)[1])})
                if self.command == "POST" and re.fullmatch(r"/api/v2/experiments/\d+/(tags|uploads)", p):
                    return self._send(201, None, {"Location": p + "/1"})
                return self._send(404, {"description": "no route"})

            def _send(self, status, payload, headers=None):
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)   # a free port
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def calls(self):
        return [(r["method"], r["path"]) for r in self.requests]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fake():
    f = FakeElab()
    yield f
    f.close()


@pytest.fixture()
def run_file(tmp_path):
    d = tmp_path / "data"
    p = _write(d, "2026-10-01/100000_fmr_map.nc", name="fmr map",
               fixed={"rf_power": 5.0}, created="2026-10-01T10:00:00",
               info={"sample": "B7", "operator": "lukas", "tags": "fmr, map",
                     "setup_name": "TR-MOKE", "comment": "first look"})
    return d, p


def test_base_url_is_cut_back_to_the_instance_root():
    assert eln.normalise_base_url("eln.example.org/") == "https://eln.example.org"
    assert eln.normalise_base_url("https://eln.example.org/api/v2/") == "https://eln.example.org"
    assert eln.normalise_base_url(
        "https://eln.example.org/experiments.php?mode=view&id=3") == "https://eln.example.org"
    with pytest.raises(eln.ElabError):
        eln.normalise_base_url("  ")


def test_key_never_shows_in_repr_or_errors(fake):
    c = eln.ElabClient(fake.url, "wrong-secret-key")
    assert "wrong-secret-key" not in repr(c)
    with pytest.raises(eln.ElabError) as exc:
        c.me()
    assert "eLabFTW refused the API key (401)" in str(exc.value)
    assert "wrong-secret-key" not in str(exc.value)


def test_connection_check_names_the_user(fake):
    c = eln.ElabClient(fake.url, GOOD_KEY)
    line = eln.check_connection(c)
    assert "Test Person" in line and "NanoSpin" in line
    assert fake.requests[-1]["auth"] == GOOD_KEY


def test_entry_has_title_body_tags(run_file):
    _d, p = run_file
    e = eln.entry_from_run(p)
    assert e.title.startswith("fmr map") and "2026-10-01 10:00" in e.title
    for bit in ("TR-MOKE", "B7", "lukas", "first look", "field", "rf_power", "lockin_r",
                str(p)):
        assert bit in e.body_html, bit
    assert e.tags[0] == "AaltoFlow"
    for t in ("TR-MOKE", "fmr", "map", "field", "lockin_r"):
        assert t in e.tags
    # a catalogue row works too
    assert eln.entry_from_run({"path": str(p)}).title == e.title


def test_preview_is_a_png(run_file):
    pytest.importorskip("matplotlib")
    png = eln.preview_png(run_file[1])
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_upload_sequence_and_ledger(fake, run_file):
    d, p = run_file
    c = eln.ElabClient(fake.url, GOOD_KEY)
    res = eln.upload_run(p, c, data_dir=d)
    assert res.action == "created" and res.exp_id == 42
    assert res.url == f"{fake.url}/experiments.php?mode=view&id=42"
    calls = fake.calls()
    assert calls[0] == ("POST", "/api/v2/experiments")
    assert calls[1] == ("PATCH", "/api/v2/experiments/42")
    tag_calls = [c_ for c_ in calls if c_[1].endswith("/tags")]
    assert len(tag_calls) >= 4
    assert all(r["auth"] == GOOD_KEY for r in fake.requests)
    patch = json.loads(fake.requests[1]["body"])
    assert patch["content_type"] == 1 and "<h2>fmr map</h2>" in patch["body"]
    tags = [json.loads(r["body"])["tag"] for r in fake.requests if r["path"].endswith("/tags")]
    assert "AaltoFlow" in tags and "TR-MOKE" in tags
    ups = [r for r in fake.requests if r["path"].endswith("/uploads")]
    # the preview only (data file is off by default) -- if matplotlib exists
    try:
        import matplotlib  # noqa: F401
        assert len(ups) == 1
        assert ups[0]["ctype"].startswith("multipart/form-data; boundary=")
        assert b'name="file"; filename="100000_fmr_map_preview.png"' in ups[0]["body"]
        assert b"\x89PNG" in ups[0]["body"]
    except ImportError:
        assert ups == []
    rec = eln.upload_record(d, p)
    assert rec["id"] == 42 and rec["complete"] is True and rec["data_attached"] is False
    assert json.loads((d / eln.LEDGER_NAME).read_text(encoding="utf-8"))["runs"][
        "2026-10-01/100000_fmr_map.nc"]["url"] == res.url


def test_second_upload_asks_and_does_not_duplicate(fake, run_file):
    d, p = run_file
    c = eln.ElabClient(fake.url, GOOD_KEY)
    eln.upload_run(p, c, data_dir=d)
    n = len(fake.requests)
    with pytest.raises(eln.AlreadyUploaded) as exc:
        eln.upload_run(p, c, data_dir=d)
    assert exc.value.record["id"] == 42
    assert len(fake.requests) == n                       # nothing sent
    res = eln.upload_run(p, c, data_dir=d, if_exists="skip")
    assert res.action == "skipped" and res.exp_id == 42 and len(fake.requests) == n
    res = eln.upload_run(p, c, data_dir=d, if_exists="update")
    assert res.action == "updated" and res.exp_id == 42
    new_calls = fake.calls()[n:]
    assert ("POST", "/api/v2/experiments") not in new_calls
    assert new_calls[0] == ("PATCH", "/api/v2/experiments/42")
    res = eln.upload_run(p, c, data_dir=d, if_exists="new")
    assert res.exp_id == 43 and eln.upload_record(d, p)["id"] == 43


def test_attach_data_and_size_limit(fake, run_file):
    d, p = run_file
    c = eln.ElabClient(fake.url, GOOD_KEY)
    res = eln.upload_run(p, c, data_dir=d, attach_data=True, max_data_mb=1e-6)
    assert any("not attached" in w and "limit" in w for w in res.warnings)
    ups = [r for r in fake.requests if r["path"].endswith("/uploads")]
    assert not any(b'filename="100000_fmr_map.nc"' in r["body"] for r in ups)
    body = json.loads(next(r for r in fake.requests if r["method"] == "PATCH")["body"])["body"]
    assert "not attached" in body
    fake.requests.clear()
    res = eln.upload_run(p, c, data_dir=d, attach_data=True, if_exists="new")
    ups = [r for r in fake.requests if r["path"].endswith("/uploads")]
    data_up = [r for r in ups if b'filename="100000_fmr_map.nc"' in r["body"]]
    assert len(data_up) == 1 and p.read_bytes() in data_up[0]["body"]
    assert eln.upload_record(d, p)["data_attached"] is True


def test_failure_after_create_is_recorded_so_a_retry_updates(fake, run_file):
    d, p = run_file
    c = eln.ElabClient(fake.url, GOOD_KEY)
    fake.fail[("PATCH", r"/experiments/\d+$")] = 500
    with pytest.raises(eln.ElabError) as exc:
        eln.upload_run(p, c, data_dir=d)
    assert "server error (500)" in str(exc.value)
    rec = eln.upload_record(d, p)
    assert rec["id"] == 42 and rec["complete"] is False
    fake.fail.clear()
    res = eln.upload_run(p, c, data_dir=d, if_exists="update")
    assert res.exp_id == 42 and eln.upload_record(d, p)["complete"] is True


def test_403_and_unreachable_messages(fake, run_file):
    d, p = run_file
    fake.fail[("POST", r"/experiments$")] = 403
    with pytest.raises(eln.ElabError, match="READ/WRITE"):
        eln.upload_run(p, eln.ElabClient(fake.url, GOOD_KEY), data_dir=d)
    fake.close()
    with pytest.raises(eln.ElabError, match="cannot reach eLabFTW"):
        eln.ElabClient(fake.url, GOOD_KEY, timeout=2).me()
    fake.server = type("S", (), {"shutdown": lambda s: None, "server_close": lambda s: None})()


def test_settings_round_trip(tmp_path):
    root = tmp_path
    s = eln.ElnSettings.load(root)
    assert s.url == "" and not s.attach_data and not s.auto_upload
    assert s.max_data_mb == eln.DEFAULT_MAX_MB and s.verify_tls
    eln.ElnSettings(url="https://eln.example.org", attach_data=True, auto_upload=True,
                    max_data_mb=50, verify_tls=False).save(root)
    got = eln.ElnSettings.load(root)
    assert (got.url, got.attach_data, got.auto_upload, got.max_data_mb, got.verify_tls) == \
        ("https://eln.example.org", True, True, 50.0, False)
    # the key is never in suite_local.json
    txt = (root / "suite_local.json").read_text(encoding="utf-8")
    assert "key" not in txt.lower().replace("eln_", "")


def test_key_file_fallback(tmp_path, monkeypatch):
    """Without keyring and Credential Manager the key goes to a per-user file
    (and key_store_name says it is a plain file)."""
    monkeypatch.setattr(eln, "_keyring", lambda: None)
    monkeypatch.setattr(eln, "_wincred", lambda: None)
    monkeypatch.setattr(eln, "_key_file", lambda: tmp_path / "AaltoFlow" / "eln_key")
    monkeypatch.delenv(eln.KEY_ENV, raising=False)
    assert eln.get_api_key() is None
    assert "plain file" in eln.set_api_key("abc123")
    assert eln.get_api_key() == "abc123" and not eln.key_is_secure()
    monkeypatch.setenv(eln.KEY_ENV, "from-env")
    assert eln.get_api_key() == "from-env"
    monkeypatch.delenv(eln.KEY_ENV)
    eln.set_api_key(None)
    assert eln.get_api_key() is None


def test_auto_upload_off_does_nothing_and_errors_never_raise(fake, run_file, tmp_path):
    d, p = run_file
    root = tmp_path / "root"
    root.mkdir()
    log = []
    assert eln.auto_upload(p, root=root, data_dir=d, log=log.append) is None
    assert fake.requests == [] and log == []
    eln.ElnSettings(url=fake.url, auto_upload=True).save(root)
    # a wrong key: logged, not raised
    bad = eln.ElabClient(fake.url, "nope")
    assert eln.auto_upload(p, root=root, data_dir=d, log=log.append, client=bad) is None
    assert any("401" in m for m in log)
    log.clear()
    res = eln.auto_upload(p, root=root, data_dir=d, log=log.append,
                          client=eln.ElabClient(fake.url, GOOD_KEY))
    assert res.exp_id == 42 and any("experiment 42" in m for m in log)
    # again: skipped, no duplicate
    n = len(fake.requests)
    res = eln.auto_upload(p, root=root, data_dir=d, log=log.append,
                          client=eln.ElabClient(fake.url, GOOD_KEY))
    assert res.action == "skipped" and len(fake.requests) == n


def test_cli_upload(fake, run_file, tmp_path, monkeypatch, capsys):
    d, p = run_file
    monkeypatch.setenv(eln.KEY_ENV, GOOD_KEY)
    import suite_common
    monkeypatch.setattr(suite_common, "get_setting",
                        lambda name, default=None, root=None: default)
    rc = eln.main(["--url", fake.url, "upload", str(p), "--data-dir", str(d)])
    out = capsys.readouterr().out
    assert rc == 0 and "created experiment 42" in out and GOOD_KEY not in out
    rc = eln.main(["--url", fake.url, "upload", str(p), "--data-dir", str(d)])
    assert rc == 3 and "--update" in capsys.readouterr().out
    monkeypatch.setenv(eln.KEY_ENV, "bad")
    rc = eln.main(["--url", fake.url, "test"])
    assert rc == 2 and "401" in capsys.readouterr().err
