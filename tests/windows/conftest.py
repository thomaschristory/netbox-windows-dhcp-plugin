from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

TESTDATA = Path(__file__).parent / "testdata"


def fixture_bytes(name: str) -> bytes:
    return (TESTDATA / name).read_bytes()


def find_pwsh() -> str | None:
    """pwsh from $PWSH or PATH, or None."""
    env = os.environ.get("PWSH")
    if env and Path(env).is_file():
        return env
    return shutil.which("pwsh")


@pytest.fixture
def pwsh_path() -> str:
    path = find_pwsh()
    if not path:
        pytest.skip("pwsh not installed; skipping PowerShell script tests")
    return path


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "pwsh: runs the real scripts in a local pwsh against stub cmdlets")
