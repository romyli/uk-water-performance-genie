"""Shared test setup. Tests run in the validate_project job on Databricks serverless compute.

Unit tests are deterministic and make no network calls. Fixtures are generated at run time under
the test artifact directory (a run-specific folder in the validation Volume when run by the job).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "bundles" / "data" / "src", ROOT / "bundles" / "genie", ROOT / "tools", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

sys.dont_write_bytecode = True


@pytest.fixture
def artifact_dir(tmp_path) -> Path:
    """A fresh directory for generated fixtures: inside the validation Volume when available."""
    base = os.environ.get("WP_TEST_ARTIFACT_ROOT")
    if not base:
        return tmp_path
    d = Path(base) / "fixtures" / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT
