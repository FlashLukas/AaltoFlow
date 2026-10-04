"""Make the src-layout package importable in tests without installing it.

`uv run pytest` uses the installed package; this shim also lets a bare `pytest`
find `cs260` straight from src/, which is handy while iterating.
"""

# The security setup of the PC running the tests (secure.py: its keys, the lab
# keyring and policy) must never change what the tests see: point them at an
# empty folder, i.e. security "off". Tests of the security itself set their
# own folder.
import os as _os
import tempfile as _tempfile
_os.environ["AALTOFLOW_SECURITY_DIR"] = _tempfile.mkdtemp(prefix="aaltoflow-nosec-")


import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, os.path.abspath(_SRC))
