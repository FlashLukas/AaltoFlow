"""
eln.py -- send a measurement run to an eLabFTW electronic lab notebook (2026-10-11).

Lukas chose eLabFTW (open source, self-hosted by many universities, a REST API
"v2"). One run becomes one EXPERIMENT there:

  * title  : the scan's name + the day it ran ("fmr map 2026-10-01 10:00");
  * body   : an HTML summary of the file's HEADER -- setup, start / end /
             duration, the axes with their ranges and points, the detectors,
             the fixed conditions, the run info (sample, operator, ...) and the
             path of the file on the measurement PC;
  * tags   : "AaltoFlow", the setup name, the run's own tags, every axis and
             detector id;
  * files  : a preview figure (PNG), and -- only if asked, off by default --
             the .nc file itself (refused above a size limit, 200 MB default:
             an ELN is a notebook, not the data archive).

No Qt here (the suite's Catalogue tab and the auto-upload use it from a worker
thread; the command line uses it directly):

    python -m scan_core.eln test
    python -m scan_core.eln set-key                 (asks for the key, no echo)
    python -m scan_core.eln upload <file.nc> [--attach-data] [--update|--skip|--new]

THE API (https://doc.elabftw.net/api/v2/), as used here:
    POST  /api/v2/experiments              -> 201, header Location: .../experiments/<id>
    PATCH /api/v2/experiments/<id>         {"title", "body", "content_type": 1 (HTML)}
    POST  /api/v2/experiments/<id>/tags    {"tag": "..."}         (one per call)
    POST  /api/v2/experiments/<id>/uploads multipart: "file", "comment"
    GET   /api/v2/users/me                 (the connection test)
Every request carries the header  Authorization: <api key>.

Only the standard library is used (urllib): scan-core has no `requests`, and a
lab PC behind a strict proxy is easier to get going with fewer packages.

WHO HAS BEEN SENT WHAT -- the upload LEDGER. `<data dir>/eln_uploads.json`
records, per run (path relative to the data folder), the experiment id and URL.
A second "Send to eLabFTW" of the same run then asks: update that entry, skip,
or make a new one -- instead of quietly filling the notebook with duplicates.
It is a file of its own and NOT a table of catalogue.sqlite on purpose: the
catalogue index is disposable (developer notes 4c, it is rebuilt from the .nc
files at any time), and the fact "this run is experiment 42" exists nowhere in
the .nc file, so it must survive a rebuild. The Catalogue tab shows it.

THE API KEY is a password: it is never printed, logged, or put into
suite_local.json. It is kept, in this order of preference, by
  1. the `keyring` package, if it is installed (Windows Credential Manager,
     macOS Keychain, Secret Service on Linux);
  2. on Windows without keyring: the Windows Credential Manager directly
     (advapi32 CredWrite/CredRead through ctypes -- the same store);
  3. otherwise a per-user file, %APPDATA%\\AaltoFlow\\eln_key (or
     ~/.config/aaltoflow/eln_key), readable by anyone who can read your files
     -- key_store_name() says so, and the Settings tab shows it.
The environment variable AALTOFLOW_ELN_KEY, if set, wins (for scripts).
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import ssl
import sys
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

# ─────────────────────────────── settings names ───────────────────────────────
# Plain preferences of THIS PC, in suite_local.json under "settings" (gitignored,
# per PC; suite_common.get_setting / set_setting). The key is NOT one of them.
SETTING_URL = "eln_url"                  # e.g. https://eln.example.org
SETTING_ATTACH = "eln_attach_data"       # attach the .nc file too (default off)
SETTING_AUTO = "eln_auto_upload"         # upload after every saved scan (default off)
SETTING_MAX_MB = "eln_max_data_mb"       # the .nc size limit (default 200)
SETTING_VERIFY_TLS = "eln_verify_tls"    # check the server certificate (default on)

DEFAULT_MAX_MB = 200.0
LEDGER_NAME = "eln_uploads.json"
#: the name the key is stored under (Credential Manager "generic credential")
KEY_TARGET = "AaltoFlow/eLabFTW"
KEY_ENV = "AALTOFLOW_ELN_KEY"


class ElabError(RuntimeError):
    """Something the operator can act on; the message says what."""


class AlreadyUploaded(ElabError):
    """The run is in the ledger already; `.record` holds what was sent."""

    def __init__(self, record: dict):
        self.record = dict(record)
        super().__init__(f"already in eLabFTW as experiment {record.get('id')} "
                         f"({record.get('url')})")


# ═════════════════════════════════ the client ═════════════════════════════════

def normalise_base_url(url: str) -> str:
    """The instance root, whatever was pasted: "eln.example.org/" ->
    "https://eln.example.org"; a pasted ".../api/v2" or a page URL inside the
    instance is cut back to the root."""
    u = (url or "").strip()
    if not u:
        raise ElabError("no eLabFTW address set (Settings tab, ELN section)")
    if "://" not in u:
        u = "https://" + u
    u = u.rstrip("/")
    u = re.sub(r"/api/v2.*$", "", u)
    u = re.sub(r"/[^/]+\.php(\?.*)?$", "", u)   # experiments.php?mode=view...
    return u.rstrip("/")


class ElabClient:
    """A minimal eLabFTW API v2 client (urllib only).

    The key is kept in a private attribute and never appears in repr(), an
    exception message or a log line: an error names the URL and the status.
    """

    def __init__(self, base_url: str, api_key: str, verify_tls: bool = True,
                 timeout: float = 30.0):
        self.base_url = normalise_base_url(base_url)
        if not api_key:
            raise ElabError("no eLabFTW API key stored (Settings tab, ELN section)")
        self._key = str(api_key).strip()
        self.verify_tls = bool(verify_tls)
        self.timeout = float(timeout)
        self._ctx = None
        if self.base_url.startswith("https://") and not self.verify_tls:
            # a self-signed certificate on an in-house server: the operator
            # unticked "check the certificate". Nothing else changes.
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def __repr__(self) -> str:                 # never show the key
        return f"ElabClient({self.base_url!r}, api_key=<hidden>)"

    @property
    def api(self) -> str:
        return self.base_url + "/api/v2"

    def experiment_url(self, exp_id: int) -> str:
        """The page a person opens in the browser."""
        return f"{self.base_url}/experiments.php?mode=view&id={int(exp_id)}"

    # ---- one request ---------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body=None, data: bytes | None = None,
                 content_type: str | None = None):
        url = self.api + path
        headers = {"Authorization": self._key, "Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                body = resp.read()
                return resp.status, dict(resp.headers.items()), body
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                raw = exc.read().decode("utf-8", "replace")
                try:
                    j = json.loads(raw)
                    detail = str(j.get("description") or j.get("message") or j.get("error") or "")
                except ValueError:
                    detail = raw.strip()[:200]
            except Exception:
                pass
            raise ElabError(_http_message(exc.code, method, url, detail)) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper():
                raise ElabError(
                    f"eLabFTW at {self.base_url}: the server certificate could not be "
                    f"checked ({reason}). If it is an in-house server with its own "
                    f"certificate, untick 'check the server certificate'.") from None
            raise ElabError(f"cannot reach eLabFTW at {self.base_url}: {reason}") from None
        except (TimeoutError, OSError) as exc:
            raise ElabError(f"cannot reach eLabFTW at {self.base_url}: {exc}") from None

    # ---- the verbs used --------------------------------------------------------

    def me(self) -> dict:
        """GET /users/me -- who the key belongs to (the connection test)."""
        _st, _h, body = self._request("GET", "/users/me")
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            raise ElabError(f"{self.base_url} answered, but not like eLabFTW "
                            f"(no JSON from /api/v2/users/me)") from None

    def create_experiment(self, title: str) -> int:
        """POST /experiments; the new id is the end of the Location header."""
        _st, headers, _b = self._request("POST", "/experiments", json_body={"title": title})
        loc = next((v for k, v in headers.items() if k.lower() == "location"), "")
        m = re.search(r"/(\d+)/?$", loc or "")
        if not m:
            raise ElabError(f"eLabFTW created an experiment but did not say which "
                            f"(Location header: {loc!r})")
        return int(m.group(1))

    def update_experiment(self, exp_id: int, title: str | None = None,
                          body_html: str | None = None) -> None:
        payload = {}
        if title is not None:
            payload["title"] = title
        if body_html is not None:
            payload["body"] = body_html
            payload["content_type"] = 1          # 1 = HTML (2 would be Markdown)
        self._request("PATCH", f"/experiments/{int(exp_id)}", json_body=payload)

    def add_tag(self, exp_id: int, tag: str) -> None:
        self._request("POST", f"/experiments/{int(exp_id)}/tags", json_body={"tag": tag})

    def upload_file(self, exp_id: int, filename: str, content: bytes,
                    comment: str = "", mime: str = "application/octet-stream") -> None:
        """POST /experiments/<id>/uploads, multipart/form-data, field "file"."""
        boundary = "aaltoflow-" + uuid.uuid4().hex
        safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        buf = io.BytesIO()
        if comment:
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(b'Content-Disposition: form-data; name="comment"\r\n\r\n')
            buf.write(comment.encode("utf-8") + b"\r\n")
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
                  .encode("utf-8"))
        buf.write(f"Content-Type: {mime}\r\n\r\n".encode())
        buf.write(content)
        buf.write(f"\r\n--{boundary}--\r\n".encode())
        self._request("POST", f"/experiments/{int(exp_id)}/uploads", data=buf.getvalue(),
                      content_type=f"multipart/form-data; boundary={boundary}")


def _http_message(code: int, method: str, url: str, detail: str) -> str:
    """Say what a status means for the person at the measurement PC."""
    where = f"{method} {url}"
    if code == 401:
        msg = "eLabFTW refused the API key (401)"
        hint = "check the key in the Settings tab (eLabFTW: user panel > API keys)"
    elif code == 403:
        msg = "eLabFTW: the API key may not do this (403)"
        hint = "the key must be READ/WRITE (a read-only key cannot create experiments)"
    elif code == 404:
        msg = "eLabFTW: not found (404)"
        hint = "is the address the instance root (https://eln.example.org)? API v2 needs eLabFTW 4.4 or newer"
    elif code == 413:
        msg = "eLabFTW: the file is too large for the server (413)"
        hint = "the server's upload limit is lower than this file; do not attach the data file"
    elif code >= 500:
        msg = f"eLabFTW server error ({code})"
        hint = "try again later, or ask the instance's admin"
    else:
        msg = f"eLabFTW answered {code}"
        hint = ""
    parts = [msg]
    if detail:
        parts.append(detail)
    if hint:
        parts.append(hint)
    return " - ".join(parts) + f"  [{where}]"


# ═══════════════════════════════ the API key store ════════════════════════════

def _key_file() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "AaltoFlow" / "eln_key"
    return Path.home() / ".config" / "aaltoflow" / "eln_key"


def _keyring():
    try:
        import keyring                      # optional; not a dependency
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


class _WinCred:
    """The Windows Credential Manager through advapi32 (no extra package).

    A "generic credential" named KEY_TARGET, persisted for this user on this
    PC (Control Panel > Credential Manager > Windows Credentials shows it)."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                        ("UserName", wintypes.LPWSTR)]

        self.ct, self.CREDENTIALW = ctypes, CREDENTIALW
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.read = adv.CredReadW
        self.read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        self.read.restype = wintypes.BOOL
        self.write = adv.CredWriteW
        self.write.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self.write.restype = wintypes.BOOL
        self.delete = adv.CredDeleteW
        self.delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.delete.restype = wintypes.BOOL
        self.free = adv.CredFree
        self.free.argtypes = [ctypes.c_void_p]

    def get(self, target: str) -> str | None:
        ct = self.ct
        p = ct.POINTER(self.CREDENTIALW)()
        if not self.read(target, self.CRED_TYPE_GENERIC, 0, ct.byref(p)):
            return None
        try:
            c = p.contents
            raw = ct.string_at(c.CredentialBlob, c.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            self.free(p)

    def set(self, target: str, secret: str) -> None:
        ct = self.ct
        blob = secret.encode("utf-16-le")
        buf = ct.create_string_buffer(blob, len(blob))
        cred = self.CREDENTIALW()
        cred.Type = self.CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ct.cast(buf, ct.POINTER(ct.c_char))
        cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = "api-key"
        if not self.write(ct.byref(cred), 0):
            raise ElabError(f"Windows Credential Manager refused to store the key "
                            f"(error {ct.get_last_error()})")

    def remove(self, target: str) -> None:
        self.delete(target, self.CRED_TYPE_GENERIC, 0)


def _wincred():
    if os.name != "nt":
        return None
    try:
        return _WinCred()
    except Exception:
        return None


def key_store_name() -> str:
    """Where set_api_key() puts the key on this PC, in words."""
    if _keyring() is not None:
        return "the system keyring (keyring package)"
    if _wincred() is not None:
        return "Windows Credential Manager"
    return (f"a plain file, {_key_file()} -- readable by anyone who can read "
            f"your user files")


def key_is_secure() -> bool:
    return _keyring() is not None or _wincred() is not None


def get_api_key() -> str | None:
    """The stored key, or None. AALTOFLOW_ELN_KEY wins (scripts)."""
    env = os.environ.get(KEY_ENV)
    if env:
        return env.strip()
    kr = _keyring()
    if kr is not None:
        try:
            got = kr.get_password(KEY_TARGET, "api-key")
            if got:
                return got
        except Exception:
            pass
    wc = _wincred()
    if wc is not None:
        got = wc.get(KEY_TARGET)
        if got:
            return got
    try:
        txt = _key_file().read_text(encoding="utf-8").strip()
        return txt or None
    except OSError:
        return None


def set_api_key(key: str | None) -> str:
    """Store (or with None/"" forget) the key; returns where it went."""
    key = (key or "").strip()
    kr, wc = _keyring(), _wincred()
    if not key:
        if kr is not None:
            try:
                kr.delete_password(KEY_TARGET, "api-key")
            except Exception:
                pass
        if wc is not None:
            wc.remove(KEY_TARGET)
        try:
            _key_file().unlink()
        except OSError:
            pass
        return "forgotten"
    if kr is not None:
        kr.set_password(KEY_TARGET, "api-key", key)
        return key_store_name()
    if wc is not None:
        wc.set(KEY_TARGET, key)
        return key_store_name()
    f = _key_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(key, encoding="utf-8")
    try:
        os.chmod(f, 0o600)                  # owner only, where the OS honours it
    except OSError:
        pass
    return key_store_name()


# ═════════════════════════════════ settings ═══════════════════════════════════

@dataclass
class ElnSettings:
    url: str = ""
    attach_data: bool = False
    auto_upload: bool = False
    max_data_mb: float = DEFAULT_MAX_MB
    verify_tls: bool = True

    @classmethod
    def load(cls, root=None) -> "ElnSettings":
        from suite_common import get_setting

        def g(name, default):
            try:
                return get_setting(name, default, root=root)
            except Exception:
                return default
        try:
            mb = float(g(SETTING_MAX_MB, DEFAULT_MAX_MB) or DEFAULT_MAX_MB)
        except (TypeError, ValueError):
            mb = DEFAULT_MAX_MB
        return cls(url=str(g(SETTING_URL, "") or ""),
                   attach_data=bool(g(SETTING_ATTACH, False)),
                   auto_upload=bool(g(SETTING_AUTO, False)),
                   max_data_mb=mb,
                   verify_tls=bool(g(SETTING_VERIFY_TLS, True)))

    def save(self, root=None) -> None:
        from suite_common import set_setting
        set_setting(SETTING_URL, self.url.strip() or None, root=root)
        # defaults are stored as "absent", so suite_local.json stays short
        set_setting(SETTING_ATTACH, True if self.attach_data else None, root=root)
        set_setting(SETTING_AUTO, True if self.auto_upload else None, root=root)
        set_setting(SETTING_MAX_MB, None if self.max_data_mb == DEFAULT_MAX_MB
                    else float(self.max_data_mb), root=root)
        set_setting(SETTING_VERIFY_TLS, None if self.verify_tls else False, root=root)


def client_from_settings(root=None, settings: ElnSettings | None = None,
                         api_key: str | None = None) -> ElabClient:
    s = settings or ElnSettings.load(root)
    return ElabClient(s.url, api_key if api_key is not None else get_api_key(),
                      verify_tls=s.verify_tls)


def check_connection(client: ElabClient) -> str:
    """One line for the Settings tab: who the key belongs to."""
    me = client.me()
    who = me.get("fullname") or " ".join(
        x for x in (me.get("firstname"), me.get("lastname")) if x) or "?"
    team = me.get("team_name") or ""
    return f"connected to {client.base_url} as {who}" + (f" (team {team})" if team else "")


# ═════════════════════════════════ the ledger ═════════════════════════════════

_ledger_lock = threading.Lock()


def ledger_path(data_dir) -> Path:
    return Path(data_dir) / LEDGER_NAME


def _relkey(data_dir, path) -> str:
    p = Path(path).resolve()
    try:
        return p.relative_to(Path(data_dir).resolve()).as_posix()
    except ValueError:
        return p.as_posix()                  # a file outside the data folder


def uploads(data_dir) -> dict:
    """{relpath: record} -- every run sent from this data folder."""
    if data_dir is None:
        return {}
    try:
        got = json.loads(ledger_path(data_dir).read_text(encoding="utf-8"))
        return got.get("runs", {}) if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def upload_record(data_dir, path) -> dict | None:
    return uploads(data_dir).get(_relkey(data_dir, path))


def _record(data_dir, path, rec: dict) -> None:
    """Write one run's record ATOMICALLY (temp file + replace): two uploads
    finishing together must not leave a torn ledger."""
    lp = ledger_path(data_dir)
    with _ledger_lock:
        runs = uploads(data_dir)
        runs[_relkey(data_dir, path)] = rec
        tmp = lp.with_name(lp.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"version": 1, "runs": runs}, indent=1,
                                  ensure_ascii=True), encoding="utf-8")
        os.replace(tmp, lp)


# ════════════════════════════════ the entry ═══════════════════════════════════

@dataclass
class Entry:
    title: str
    body_html: str
    tags: list[str]
    path: Path
    meta: dict = field(default_factory=dict)


def _fmt_num(v) -> str:
    try:
        return f"{float(v):.6g}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_duration(s) -> str:
    if s is None:
        return "?"
    s = int(round(float(s)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h} h {m:02d} min {sec:02d} s" if h else f"{m} min {sec:02d} s"


def _tag_ok(t: str) -> str:
    t = re.sub(r"\s+", " ", str(t)).strip()
    return t[:100]


def entry_from_run(run, setup_name: str | None = None) -> Entry:
    """Title, HTML body and tags for one run.

    `run` is a path to the .nc file or a catalogue row (a dict with "path",
    from catalogue.search). The header is read again from the file either way
    (milliseconds; the catalogue row has no units or conditions). Nothing of
    the data itself is loaded here.
    """
    from scan_core.catalogue import read_entry
    path = Path(run["path"] if isinstance(run, dict) else run)
    if not path.is_file():
        raise ElabError(f"no such file: {path}")
    e = read_entry(path)
    name = e.get("name") or path.stem
    created = e.get("created") or ""
    try:
        start = datetime.fromisoformat(created)
    except ValueError:
        start = None
    dur = e.get("duration")
    end = start + timedelta(seconds=float(dur)) if (start and dur is not None) else None
    setup = e.get("setup_name") or setup_name or ""
    title = name + (f"  {start:%Y-%m-%d %H:%M}" if start else "")

    esc = html.escape
    rows = []

    def row(k, v):
        if v not in (None, ""):
            rows.append(f"<tr><th style='text-align:left;padding-right:12px'>{esc(k)}</th>"
                        f"<td>{esc(str(v))}</td></tr>")

    row("setup", setup)
    row("started", f"{start:%Y-%m-%d %H:%M:%S}" if start else created)
    row("ended", f"{end:%Y-%m-%d %H:%M:%S}" if end else "")
    row("duration", _fmt_duration(dur) if dur is not None else "")
    row("points", e.get("n_points"))
    row("status", e.get("status"))
    for k in ("sample", "structure", "operator", "project", "series"):
        row(k, e.get(k))
    row("tags", ", ".join(e.get("tags") or []))
    row("recipe", e.get("recipe_name") if e.get("recipe_name") not in (None, name) else "")
    row("instruments", ", ".join(s for s, _src in e.get("instruments") or []))
    row("software", f"AaltoFlow {e['aaltoflow_version']}" if e.get("aaltoflow_version") else "")

    parts = [f"<h2>{esc(name)}</h2>"]
    if e.get("comment"):
        parts.append(f"<p>{esc(e['comment'])}</p>")
    parts.append("<table>" + "".join(rows) + "</table>")

    dims = e.get("dims") or []
    if dims:
        li = []
        for d in dims:
            rng = ""
            if "min" in d:
                rng = f": {_fmt_num(d['min'])} .. {_fmt_num(d['max'])} {d.get('units', '')}".rstrip()
            param = f" (parameter {d['param']})" if d.get("param") and d["param"] != d["name"] else ""
            li.append(f"<li><b>{esc(d['name'])}</b>{esc(param)}{esc(rng)}, "
                      f"{int(d['size'])} points</li>")
        parts.append("<h3>Axes (outer to inner)</h3><ul>" + "".join(li) + "</ul>")
    dets = e.get("detectors") or []
    if dets:
        li = []
        for d in dets:
            unit = f" [{d['units']}]" if d.get("units") else ""
            label = f" - {d['label']}" if d.get("label") and d["label"] != d["name"] else ""
            kind = f" ({d['type']})" if d.get("type") and d["type"] != "float" else ""
            li.append(f"<li><b>{esc(d['name'])}</b>{esc(unit + kind + label)}</li>")
        parts.append("<h3>Detectors</h3><ul>" + "".join(li) + "</ul>")
    conds = e.get("conditions") or []
    if conds:
        li = []
        for c in conds:
            val = _fmt_num(c["num"]) if c.get("num") is not None else (c.get("text") or "")
            li.append(f"<li><b>{esc(c['key'])}</b> = {esc(val)} {esc(c.get('units') or '')}</li>")
        parts.append("<h3>Conditions (set once before the scan)</h3><ul>"
                     + "".join(li) + "</ul>")
    parts.append(f"<p><b>Data file</b> on the measurement PC: <code>{esc(str(path))}</code> "
                 f"({path.stat().st_size / 1e6:.1f} MB)</p>")
    parts.append("<p><i>Sent by AaltoFlow (scan-core). The .nc file holds the full "
                 "scan definition and every instrument's settings.</i></p>")

    tags = ["AaltoFlow"]
    if setup:
        tags.append(setup)
    tags += list(e.get("tags") or [])
    for d in dims:
        tags.append(d.get("param") or d["name"])
    tags += [d["name"] for d in dets]
    seen, clean = set(), []
    for t in tags:
        t = _tag_ok(t)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            clean.append(t)
    return Entry(title=title, body_html="\n".join(parts), tags=clean, path=path,
                 meta={"name": name, "created": created, "size": path.stat().st_size,
                       "detectors": [d["name"] for d in dets]})


# ════════════════════════════════ the preview ═════════════════════════════════

def preview_png(path, detector: str | None = None) -> bytes | None:
    """A small figure of the run: the first detector, as a line (1-D) or a map
    (the two innermost axes; any further axis is averaged). None if
    matplotlib is not installed or the data has nothing plottable -- an upload
    never fails for want of a picture.

    Drawn with matplotlib's Agg canvas directly (no pyplot): it is safe from a
    worker thread and does not touch the backend of a Qt application."""
    try:
        import numpy as np
        import xarray as xr
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except Exception:
        return None
    try:
        with xr.open_dataset(path, engine="h5netcdf") as ds:
            names = [n for n in ds.data_vars
                     if ds[n].dtype.kind in "iufb" and ds[n].ndim >= 1
                     and not str(n).endswith("_imag")]
            if detector:
                pick = next((n for n in names if n == detector or n == f"{detector}_real"), None)
            else:
                pick = None
            pick = pick or (names[0] if names else None)
            if pick is None:
                return None
            da = ds[pick]
            label = str(pick)
            pair = da.attrs.get("complex_pair")
            if pair and f"{pair}_imag" in ds:
                da = np.hypot(da.astype(float), ds[f"{pair}_imag"].astype(float))
                label = f"|{pair}|"
            da = da.astype(float).load()
            if da.ndim > 2:
                da = da.mean(dim=list(da.dims[:-2]), skipna=True)
            units = ds[pick].attrs.get("units", "")
            title = ds.attrs.get("name") or Path(path).stem
        fig = Figure(figsize=(6.4, 4.0), dpi=100)
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)

        def coord(dim):
            if dim in da.coords and da.coords[dim].ndim == 1 and \
                    da.coords[dim].dtype.kind in "iuf":
                return np.asarray(da.coords[dim].values, dtype=float)
            return np.arange(da.sizes[dim], dtype=float)

        def dlabel(dim):
            u = da.coords[dim].attrs.get("units", "") if dim in da.coords else ""
            return f"{dim} [{u}]" if u else str(dim)

        if da.ndim == 1:
            ax.plot(coord(da.dims[0]), np.asarray(da.values), lw=1.2, marker=".")
            ax.set_xlabel(dlabel(da.dims[0]))
            ax.set_ylabel(f"{label} [{units}]" if units else label)
        else:
            ydim, xdim = da.dims
            mesh = ax.pcolormesh(coord(xdim), coord(ydim), np.asarray(da.values),
                                 shading="auto", cmap="viridis")
            fig.colorbar(mesh, ax=ax, label=f"{label} [{units}]" if units else label)
            ax.set_xlabel(dlabel(xdim))
            ax.set_ylabel(dlabel(ydim))
        ax.set_title(str(title), fontsize=10)
        fig.tight_layout()
        out = io.BytesIO()
        fig.savefig(out, format="png")
        return out.getvalue()
    except Exception:
        return None


# ═════════════════════════════════ uploading ══════════════════════════════════

@dataclass
class UploadResult:
    path: Path
    exp_id: int | None = None
    url: str = ""
    action: str = "created"         # created / updated / skipped
    warnings: list[str] = field(default_factory=list)


def upload_run(path, client: ElabClient, *, data_dir=None, attach_data: bool = False,
               max_data_mb: float = DEFAULT_MAX_MB, if_exists: str = "error",
               setup_name: str | None = None,
               progress: Callable[[str], None] | None = None) -> UploadResult:
    """Send one run to eLabFTW and record it in the ledger.

    if_exists, when the ledger already has this run:
      "error"  -> raise AlreadyUploaded (the GUI asks, the CLI tells you the flags)
      "update" -> rewrite that experiment's title/body, add tags, attach a fresh
                  preview (and the data file if asked and not attached yet)
      "skip"   -> do nothing, return action "skipped"
      "new"    -> make another experiment anyway
    The data file is attached only with attach_data and only up to
    max_data_mb; a larger one is NOT attached and the result (and the entry's
    body) says so -- the notebook entry itself is still made.
    """
    say = progress or (lambda msg: None)
    path = Path(path)
    data_dir = Path(data_dir) if data_dir else path.parent
    old = upload_record(data_dir, path)
    if old and if_exists == "error":
        raise AlreadyUploaded(old)
    if old and if_exists == "skip":
        return UploadResult(path, old.get("id"), old.get("url", ""), "skipped")
    if if_exists not in ("error", "update", "skip", "new"):
        raise ValueError(f"if_exists: {if_exists!r}")

    entry = entry_from_run(path, setup_name=setup_name)
    result = UploadResult(path)
    size = entry.meta["size"]
    attach = bool(attach_data)
    body = entry.body_html
    if attach and size > max_data_mb * 1e6:
        attach = False
        msg = (f"data file not attached: {size / 1e6:.0f} MB is above the "
               f"{max_data_mb:g} MB limit")
        result.warnings.append(msg)
        body += f"\n<p><i>{html.escape(msg)}.</i></p>"
    if old and if_exists == "update" and old.get("data_attached"):
        attach = False                       # it is there already

    if old and if_exists == "update":
        exp_id = int(old["id"])
        say(f"updating experiment {exp_id}")
        result.action = "updated"
    else:
        say("creating the experiment")
        exp_id = client.create_experiment(entry.title)
    result.exp_id = exp_id
    result.url = client.experiment_url(exp_id)
    if result.action == "created":
        # Recorded AT ONCE, marked incomplete: if the body or an attachment
        # fails next, a second try finds this experiment and offers to UPDATE
        # it, instead of leaving an empty entry behind and making another.
        _record(data_dir, path, {"id": exp_id, "url": result.url,
                                 "base_url": client.base_url, "complete": False,
                                 "uploaded": datetime.now().isoformat(timespec="seconds"),
                                 "data_attached": False, "title": entry.title})
    client.update_experiment(exp_id, title=entry.title, body_html=body)
    say(f"adding {len(entry.tags)} tags")
    for t in entry.tags:
        try:
            client.add_tag(exp_id, t)
        except ElabError as exc:
            # an existing tag on an update, or a tag the team forbids: the
            # entry is still useful -- note it, go on
            result.warnings.append(f"tag {t!r}: {exc}")
    png = preview_png(path)
    if png:
        say("attaching the preview")
        client.upload_file(exp_id, path.stem + "_preview.png", png,
                           comment="preview (AaltoFlow)", mime="image/png")
    else:
        result.warnings.append("no preview figure (matplotlib missing or nothing to plot)")
    if attach:
        say(f"attaching the data file ({size / 1e6:.1f} MB)")
        client.upload_file(exp_id, path.name, path.read_bytes(),
                           comment="measurement data (netCDF)",
                           mime="application/x-netcdf")
    st = path.stat()
    _record(data_dir, path, {
        "id": exp_id, "url": result.url, "base_url": client.base_url,
        "uploaded": datetime.now().isoformat(timespec="seconds"),
        "size": st.st_size, "mtime_ns": st.st_mtime_ns,
        "data_attached": bool(attach or (old or {}).get("data_attached")),
        "title": entry.title, "complete": True,
    })
    return result


def auto_upload(path, root=None, data_dir=None, log: Callable[[str], None] | None = None,
                client: ElabClient | None = None) -> UploadResult | None:
    """The after-scan hook: upload if the setting is on. NEVER raises -- a
    scan is never failed by its notebook entry; every problem goes to `log`."""
    say = log or (lambda m: None)
    try:
        s = ElnSettings.load(root)
        if not s.auto_upload:
            return None
        if not s.url:
            say("eLabFTW: automatic upload is on, but no address is set (Settings tab)")
            return None
        cl = client or client_from_settings(root, s)
        from suite_common import setup_name as _setup
        try:
            setup = _setup(root)
        except Exception:
            setup = None
        res = upload_run(path, cl, data_dir=data_dir, attach_data=s.attach_data,
                         max_data_mb=s.max_data_mb, if_exists="skip", setup_name=setup)
        if res.action == "skipped":
            say(f"eLabFTW: {Path(path).name} is already experiment {res.exp_id}")
        else:
            say(f"eLabFTW: {Path(path).name} -> experiment {res.exp_id}  {res.url}")
        for w in res.warnings:
            say(f"eLabFTW: {w}")
        return res
    except Exception as exc:
        say(f"eLabFTW upload of {Path(path).name} failed: {exc}")
        return None


def start_auto_upload(path, root=None, data_dir=None,
                      log: Callable[[str], None] | None = None) -> threading.Thread:
    """auto_upload in a daemon thread: the caller (the GUI, right after a scan
    is saved) returns at once."""
    t = threading.Thread(target=auto_upload, args=(path, root, data_dir, log),
                         name="eln-auto-upload", daemon=True)
    t.start()
    return t


# ═════════════════════════════════════ CLI ════════════════════════════════════

def _ascii(s) -> str:
    """Printed text is ASCII (gotcha #14)."""
    return str(s).encode("ascii", "replace").decode("ascii")


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scan_core.eln",
                                 description="Send AaltoFlow runs to eLabFTW.")
    ap.add_argument("--url", help="eLabFTW address (default: the suite setting eln_url)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("test", help="check the address and the stored API key")
    sk = sub.add_parser("set-key", help="store the API key (asked for, not echoed)")
    sk.add_argument("--forget", action="store_true", help="remove the stored key")
    up = sub.add_parser("upload", help="send one or more .nc files")
    up.add_argument("files", nargs="+")
    up.add_argument("--attach-data", action="store_true", default=None,
                    help="attach the .nc file too (default: the suite setting)")
    up.add_argument("--max-mb", type=float, default=None)
    up.add_argument("--data-dir", help="the data folder the ledger is in "
                                       "(default: the suite's data folder)")
    g = up.add_mutually_exclusive_group()
    g.add_argument("--update", action="store_true", help="already sent: update that entry")
    g.add_argument("--skip", action="store_true", help="already sent: leave it")
    g.add_argument("--new", action="store_true", help="already sent: make another entry")
    a = ap.parse_args(list(argv) if argv is not None else None)

    if a.cmd == "set-key":
        if a.forget:
            set_api_key(None)
            print("the eLabFTW API key is forgotten")
            return 0
        import getpass
        key = getpass.getpass("eLabFTW API key (not shown): ")
        if not key.strip():
            print("nothing stored")
            return 1
        print(_ascii(f"stored in {set_api_key(key)}"))
        return 0

    s = ElnSettings.load()
    if a.url:
        s.url = a.url
    try:
        client = client_from_settings(settings=s)
        if a.cmd == "test":
            print(_ascii(check_connection(client)))
            return 0
        from scan_core.catalogue import _default_dir
        data_dir = Path(a.data_dir) if a.data_dir else _default_dir()
        mode = "update" if a.update else "skip" if a.skip else "new" if a.new else "error"
        attach = s.attach_data if a.attach_data is None else a.attach_data
        rc = 0
        for f in a.files:
            p = Path(f).resolve()
            ddir = data_dir if _relkey(data_dir, p) != p.as_posix() else p.parent
            try:
                res = upload_run(p, client, data_dir=ddir, attach_data=attach,
                                 max_data_mb=a.max_mb or s.max_data_mb, if_exists=mode,
                                 progress=lambda m: print(_ascii(f"  {m}")))
                print(_ascii(f"{p.name}: {res.action} experiment {res.exp_id}  {res.url}"))
                for w in res.warnings:
                    print(_ascii(f"  warning: {w}"))
            except AlreadyUploaded as exc:
                print(_ascii(f"{p.name}: {exc} -- use --update, --skip or --new"))
                rc = 3
    except ElabError as exc:
        print(_ascii(f"error: {exc}"), file=sys.stderr)
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
