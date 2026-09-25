"""scan_queue.py -- several scan definitions run one after another.

A QUEUE is an ordered list of (name, recipe). It comes from loading several
definitions at once (.yaml recipes and/or measured .nc files), or from a queue
file that was saved earlier:

    queue:
      - name: map 40 mT
        recipe: {name: ..., axes: [...], detectors: [...], ...}
      - name: map 80 mT
        recipe: maps/map_80.yaml          # a path, relative to the queue file

Saving always writes the recipes INLINE. A queue is a snapshot of what was
loaded: editing a recipe file afterwards must not change a queue saved
yesterday, just as it does not change one that is already running.

What the queue means when it runs (decided with Lukas 2026-09-24): each scan is
its own measurement file; Abort skips the CURRENT scan (its after-scan routine
still runs) and the next one starts; an ERROR stops the queue, because the
usual cause is a module that died, and the next scan would fail the same way.

No Qt here, so it is tested without a screen. The GUI is in scan_builder.py.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .recipe import Recipe

#: A name the builder gives an unnamed scan; a file name says more than that.
_DEFAULT_NAMES = {"", "scan"}


@dataclass
class QueueEntry:
    name: str
    recipe: Recipe
    source: str = ""          # the file it came from, for the dialog's tooltip

    def named_recipe(self) -> Recipe:
        """A COPY of the recipe under the entry's name -- the name goes into
        the file name and into the definition the .nc carries."""
        r = Recipe.from_dict(copy.deepcopy(self.recipe.to_dict()))
        r.name = self.name
        return r


def is_queue_file(path) -> bool:
    """True for a .yaml whose top level is {queue: [...]}."""
    p = Path(path)
    if p.suffix.lower() not in (".yaml", ".yml"):
        return False
    try:
        with open(p, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("queue"), list)


def recipe_from_file(path) -> Recipe:
    """A scan definition from a .yaml recipe OR a measured .nc (its recipe_json)."""
    if str(path).lower().endswith(".nc"):
        import xarray as xr
        with xr.open_dataset(path) as ds:
            blob = ds.attrs.get("recipe_json")
        if not blob:
            raise ValueError(f"{Path(path).name} has no scan definition in it "
                             f"(no 'recipe_json' attribute)")
        return Recipe.from_dict(json.loads(blob))
    return Recipe.load(path)


def _default_name(recipe: Recipe, path: Path) -> str:
    name = (recipe.name or "").strip()
    if name in _DEFAULT_NAMES:
        # An .nc from the autosave is "<HHMMSS>_<name>.nc": drop the time.
        stem = path.stem
        head, _, tail = stem.partition("_")
        return tail if head.isdigit() and len(head) == 6 and tail else stem
    return name


def load_queue_file(path) -> list[QueueEntry]:
    path = Path(path)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    entries = []
    for k, item in enumerate(data.get("queue") or []):
        if not isinstance(item, dict) or "recipe" not in item:
            raise ValueError(f"{path.name}: entry {k + 1} has no 'recipe'")
        spec = item["recipe"]
        if isinstance(spec, dict):
            recipe, src = Recipe.from_dict(spec), str(path)
        else:
            ref = Path(spec)
            ref = ref if ref.is_absolute() else path.parent / ref
            recipe, src = recipe_from_file(ref), str(ref)
        name = str(item.get("name") or _default_name(recipe, Path(src)))
        entries.append(QueueEntry(name, recipe, src))
    return entries


def save_queue_file(path, entries) -> None:
    data = {"queue": [{"name": e.name, "recipe": e.named_recipe().to_dict()}
                      for e in entries]}
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)


def load_definitions(paths) -> list[QueueEntry]:
    """Every scan in `paths`, in order: a recipe or .nc is one entry, a queue
    file is as many as it holds. Raises on the first file that cannot be read,
    naming it -- a queue that silently lost a scan is worse than no queue."""
    out: list[QueueEntry] = []
    for p in map(Path, paths):
        try:
            if is_queue_file(p):
                out.extend(load_queue_file(p))
            else:
                recipe = recipe_from_file(p)
                out.append(QueueEntry(_default_name(recipe, p), recipe, str(p)))
        except Exception as exc:
            raise ValueError(f"could not read {p.name}: {exc}") from exc
    return out


def validate_queue(entries, registry) -> list[list[str]]:
    """Problems per entry ([] = fine), checked ALL before anything runs: the
    third scan must not turn out to be invalid at two in the morning."""
    out = []
    for e in entries:
        errs = list(e.recipe.validate(registry))
        if not e.recipe.axes:
            errs.append("no axes")
        if not e.name.strip():
            errs.append("no name")
        out.append(errs)
    return out


def n_points(recipe: Recipe, registry=None) -> int:
    try:
        return int(recipe.compile(registry).n_points)
    except Exception:
        return 0
