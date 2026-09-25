# suite-common — module discovery

The one place that answers "which modules does the AaltoFlow suite have, and where
do they listen?". Standard library only.

A module is any folder next to this one that contains a `module.toml`
(contract: `INSTRUMENT_MODULE_GUIDE.md` section 11). This PC's choices -- port
overrides, real/simulated, services on other PCs -- are in `suite_local.json` at
the repository root, written by the launcher and not committed.

```python
from suite_common import discover
found = discover()
for m in found.modules:
    print(m.id, m.name, m.host, m.cmd, "remote" if m.remote else "local")
print(found.problems)
```

Used by mission-control, scan-core and the tools in `../tools`.

```powershell
cd suite-common
uv sync
uv run pytest -q
```
