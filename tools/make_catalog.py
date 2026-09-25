"""make_catalog.py -- write <root>/catalog.json from the modules' module.toml.

The catalog is what a person SHOPPING for a module reads: what each module is
for (category), search words (tags), version, default ports. It is generated,
never edited: run this after changing any module.toml (a suite-common test
fails while it is stale).

    python tools/make_catalog.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "suite-common" / "src"))

from suite_common.catalog import CATALOG_FILE, catalog_text  # noqa: E402
from suite_common.modules import discover_local               # noqa: E402


def main() -> int:
    mods, problems = discover_local(ROOT)
    for p in problems:
        print("problem:", p)
    out = ROOT / CATALOG_FILE
    out.write_text(catalog_text(mods), encoding="utf-8", newline="\n")
    print(f"wrote {out.name}: {len(mods)} modules")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
