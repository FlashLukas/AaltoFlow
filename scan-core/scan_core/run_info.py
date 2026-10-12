"""run_info.py -- WHAT was measured and by whom, written into every data file.

Lukas (2026-10-04): typed once in the measurement suite, kept between scans
and between restarts, and written into every .nc as file attributes. A run
catalogue indexes them, so the attribute names are a contract.

Which fields (2026-10-12: the suite is for any lab, not one rig):

* The STANDARD fields every lab has: sample, operator, project, series, tags
  and comment. Each is written as a file attribute of its own name (as since
  2026-10-04, so every older file reads the same way).
* The fields the panel SHOWS are configurable per PC: suite setting
  ``run_info_fields`` (a list of names in suite_local.json). Default:
  ``DEFAULT_FIELDS``. A lab that wants "substrate" or "thickness_nm" on every
  run puts them there.
* EXTRA fields: any other name a person adds in the panel ("+ field"). They
  are remembered with the values.
* A field that is not standard (configured or extra) is written as the
  attribute ``info_<name>`` -- the prefix keeps a person's "status" or
  "name" from overwriting an attribute the engine writes. The catalogue
  indexes every one of them under its plain name, so ``where thickness_nm > 20``
  and the free-text search find them.

Older files carry ``structure`` as a plain attribute (it was a standard field
until 2026-10-12): `from_attrs` and the catalogue still read it.

Rules:

* Remembered on this PC in suite_local.json (suite_common settings, key
  ``run_info``) -- the next scan, and the next launch, start from the last
  values. Extra fields are remembered even while empty, so the panel keeps
  showing them until they are removed.
* ``tags`` is a comma-separated list of keywords, stored normalised:
  "a,b ,  c" -> "a, b, c" (empty entries dropped, duplicates kept once).
* An EMPTY field is OMITTED from the file (no attribute), so "not given" and
  "given as nothing" are the same thing for a reader.
* ``comment`` is the SAME field as the scan definition's comment (one comment,
  not two): the builder puts it into the recipe, and the engine writes the
  recipe's comment as the ``comment`` attribute. It is always shown.
* A field name is lower case, starts with a letter, then letters, digits or
  "_" (at most 40): it becomes part of an attribute name and a `where` key.

Pure Python, no Qt.
"""

from __future__ import annotations

import re

#: the fields every lab has; written as attributes of exactly these names
STANDARD = ("sample", "operator", "project", "series", "tags", "comment")
#: the fields the panel shows when this PC has not chosen its own
DEFAULT_FIELDS = STANDARD
#: older files: plain attributes that are run info but no longer standard
LEGACY = ("structure",)
#: the suite setting holding the values (and the extra fields)
SETTING = "run_info"
#: the suite setting holding this PC's field list
FIELDS_SETTING = "run_info_fields"
#: attribute prefix of a non-standard field
PREFIX = "info_"

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def clean_name(text) -> str | None:
    """A typed field name as a valid one ("Film thickness" -> "film_thickness"),
    or None if nothing valid is left."""
    s = "_".join(str(text or "").strip().lower().split())
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s if _NAME.match(s) else None


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(_NAME.match(name))


def attr_name(field: str) -> str:
    """The file attribute a field is written as."""
    return field if field in STANDARD else PREFIX + field


def fields(root=None) -> tuple:
    """This PC's field list (suite setting run_info_fields), cleaned: valid
    names, no duplicates, `comment` always present and last."""
    raw = None
    try:
        from suite_common import get_setting
        raw = get_setting(FIELDS_SETTING, None, root=root)
    except Exception:
        raw = None
    if not isinstance(raw, (list, tuple)) or not raw:
        raw = DEFAULT_FIELDS
    out = []
    for f in raw:
        name = clean_name(f)
        if name and name not in out and name != "comment":
            out.append(name)
    return tuple(out) + ("comment",)


def set_fields(names, root=None) -> None:
    """Choose this PC's field list; None (or the default) forgets the choice."""
    from suite_common import set_setting
    cleaned = [n for n in (clean_name(x) for x in (names or [])) if n]
    cleaned = list(dict.fromkeys(cleaned))
    if not cleaned or tuple(cleaned) in (DEFAULT_FIELDS, DEFAULT_FIELDS[:-1]):
        set_setting(FIELDS_SETTING, None, root=root)
    else:
        set_setting(FIELDS_SETTING, cleaned, root=root)


def normalise_tags(text) -> str:
    """'a,b ,  c,,a' -> 'a, b, c'."""
    out = []
    for part in str(text or "").split(","):
        t = " ".join(part.split())
        if t and t not in out:
            out.append(t)
    return ", ".join(out)


def _one(field: str, v) -> str:
    v = "" if v is None else str(v)
    if field == "tags":
        return normalise_tags(v)
    if field == "comment":
        return v.strip()                   # a comment may have line breaks
    return " ".join(v.split())             # one line, single spaces


def normalise(values: dict, keys=STANDARD) -> dict:
    """Every field of `keys` as a stripped string ('' when missing), plus any
    other VALID field name present in `values` (an extra field); tags
    normalised. Invalid names are dropped."""
    values = values if isinstance(values, dict) else {}
    out = {}
    for f in list(keys) + [k for k in values if k not in keys]:
        if not valid_name(f):
            continue
        out[f] = _one(f, values.get(f, ""))
    return out


def run_info_attrs(values: dict) -> dict:
    """The file attributes for a run: non-empty fields only, WITHOUT comment
    (that one travels in the recipe, see the module docstring). A non-standard
    field gets the ``info_`` prefix."""
    vals = normalise(values)
    return {attr_name(f): v for f, v in vals.items() if v and f != "comment"}


def from_attrs(attrs: dict) -> dict:
    """{field: value} of the run info in a file's attributes (standard,
    ``info_*`` and the legacy plain ones), in the panel's order, comment
    included when the file has one. Values as text."""
    attrs = attrs or {}
    out = {}
    for f in STANDARD + LEGACY:
        v = attrs.get(f)
        if v not in (None, ""):
            out[f] = str(v)
    for k, v in attrs.items():
        if isinstance(k, str) and k.startswith(PREFIX) and v not in (None, ""):
            name = k[len(PREFIX):]
            if valid_name(name):
                out[name] = str(v)
    return out


def load(root=None) -> dict:
    """The remembered run info: every field of this PC's list ('' when never
    set), then the extra fields that were added in the panel."""
    try:
        from suite_common import get_setting
        stored = get_setting(SETTING, {}, root=root)
    except Exception:
        stored = {}
    return normalise(stored if isinstance(stored, dict) else {}, keys=fields(root))


def save(values: dict, root=None) -> None:
    """Remember the run info on this PC. Empty fields of this PC's list are
    not stored; an EXTRA field is stored even when empty (so it stays in the
    panel until removed)."""
    from suite_common import set_setting
    listed = set(fields(root)) | set(STANDARD)
    vals = {k: v for k, v in normalise(values, keys=()).items()
            if v or k not in listed}
    set_setting(SETTING, vals or None, root=root)
