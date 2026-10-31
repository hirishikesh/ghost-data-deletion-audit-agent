import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    """Tests never reach the real API, whatever the developer's environment holds."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.fixture
def private_dataset():
    """The private challenge dataset, only where a developer has it locally."""
    path = os.environ.get("GHOST_AUDIT_DATASET")
    if not path or not Path(path, "sample_output.csv").exists():
        pytest.skip("set GHOST_AUDIT_DATASET to the challenge dataset folder to run this test")
    return Path(path)
