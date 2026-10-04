"""run_info.py -- WHAT was measured and by whom, written into every data file.

Lukas (2026-10-04): sample, structure, operator, comment, project, tags and
series, typed once in the measurement suite, kept between scans and between
restarts, and written into every .nc as file attributes with EXACTLY these
names. A run catalogue indexes them by name, so the names are a contract:
do not rename them.

* Remembered on this PC in suite_local.json (suite_common settings, key
  ``run_info``) -- the next scan, and the next launch, start from the last
  values.
* ``tags`` is a comma-separated list of keywords, stored normalised:
  "a,b ,  c" -> "a, b, c" (empty entries dropped, duplicates kept once).
* An EMPTY field is OMITTED from the file (no attribute), so "not given" and
  "given as nothing" are the same thing for a reader: ``ds.attrs.get("sample",
  "")``.
* ``comment`` is the SAME field as the scan definition's comment (one comment,
  not two): the builder puts it into the recipe, and the engine has always
  written the recipe's comment as the ``comment`` attribute -- always, also
  when empty (files before 2026-10-04 have it too).

Pure Python, no Qt.
"""

from __future__ import annotations

#: The fields, in the order the panel shows them. Attribute name = field name.
FIELDS = ("sample", "structure", "operator", "project", "series", "tags",
          "comment")
#: The suite setting they are remembered under.
SETTING = "run_info"


def normalise_tags(text) -> str:
    """'a,b ,  c,,a' -> 'a, b, c'."""
    out = []
    for part in str(text or "").split(","):
        t = " ".join(part.split())
        if t and t not in out:
            out.append(t)
    return ", ".join(out)


def normalise(values: dict) -> dict:
    """Every field as a stripped string ('' when missing), tags normalised."""
    out = {}
    for f in FIELDS:
        v = values.get(f, "") if isinstance(values, dict) else ""
        v = "" if v is None else str(v)
        if f == "tags":
            v = normalise_tags(v)
        elif f == "comment":
            v = v.strip()                  # a comment may have line breaks
        else:
            v = " ".join(v.split())        # one line, single spaces
        out[f] = v
    return out


def run_info_attrs(values: dict) -> dict:
    """The file attributes for a run: non-empty fields only, WITHOUT comment
    (that one travels in the recipe, see the module docstring)."""
    vals = normalise(values)
    return {f: v for f, v in vals.items() if v and f != "comment"}


def load(root=None) -> dict:
    """The remembered run info (every field present, '' when never set)."""
    try:
        from suite_common import get_setting
        stored = get_setting(SETTING, {}, root=root)
    except Exception:
        stored = {}
    return normalise(stored if isinstance(stored, dict) else {})


def save(values: dict, root=None) -> None:
    """Remember the run info on this PC (empty fields are not stored)."""
    from suite_common import set_setting
    vals = {k: v for k, v in normalise(values).items() if v}
    set_setting(SETTING, vals or None, root=root)
