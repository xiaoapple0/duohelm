from __future__ import annotations

from pathlib import Path

import pytest

from duohelm.settings import DEFAULT_HOST, Settings, is_loopback


def test_home_from_env(tmp_path: Path) -> None:
    settings = Settings.from_env({"DUOHELM_HOME": str(tmp_path)})
    assert settings.home == tmp_path
    assert settings.plugin_dir == tmp_path / "plugins"


def test_default_home() -> None:
    assert Settings.from_env({}).home == Path.home() / ".duohelm"


def test_default_host_is_loopback() -> None:
    assert is_loopback(DEFAULT_HOST)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("192.168.1.5", False),
        ("example.com", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected
