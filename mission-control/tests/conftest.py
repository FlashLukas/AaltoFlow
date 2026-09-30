"""Shared test set-up for the launcher."""

# The security setup of the PC running the tests (suite_common/secure.py: its
# keys, the lab keyring and policy) must never change what the tests see:
# point them at an empty folder, i.e. security "off". Tests of the security
# itself set their own folder.
import os
import tempfile

os.environ["AALTOFLOW_SECURITY_DIR"] = tempfile.mkdtemp(prefix="aaltoflow-nosec-")
