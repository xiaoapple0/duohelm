from __future__ import annotations

import tomllib
from pathlib import Path

from duohelm_sdk import SDK_VERSION


def test_sdk_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["version"] == SDK_VERSION
