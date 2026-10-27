import os
import pytest

# CI and local tests must never use a real model key.
@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for k in list(os.environ):
        if k.startswith("REIMBURSE_AGENT_") or k.endswith("_API_KEY"):
            monkeypatch.delenv(k, raising=False)
