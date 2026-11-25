from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from duohelm_sdk import MANIFEST_FILENAME

MakePlugin = Callable[..., Path]


class FakeClock:
    """可手动推进的毫秒时钟，让租约、退避等时间逻辑不依赖真实等待。"""

    def __init__(self, start: int = 1_700_000_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def plugin_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugins"
    root.mkdir()
    return root


@pytest.fixture
def make_plugin(plugin_root: Path) -> MakePlugin:
    """在 plugin_root 下创建一个算子插件目录，返回目录路径。"""

    def make(
        plugin_id: str,
        *,
        version: str = "1.0.0",
        sdk: str = ">=0.2,<0.3",
        requires: dict[str, str] | None = None,
        dirname: str | None = None,
        raw: str | None = None,
    ) -> Path:
        path = plugin_root / (dirname or plugin_id)
        path.mkdir()
        if raw is None:
            lines = [
                "[plugin]",
                f'id = "{plugin_id}"',
                f'name = "{plugin_id}"',
                f'version = "{version}"',
                'kind = "operator"',
                f'sdk = "{sdk}"',
                f'entrypoint = "{plugin_id.replace("-", "_")}:plugin"',
            ]
            for dep, spec in (requires or {}).items():
                lines += ["", "[[requires]]", f'id = "{dep}"', f'version = "{spec}"']
            raw = "\n".join(lines) + "\n"
        (path / MANIFEST_FILENAME).write_text(raw, encoding="utf-8")
        return path

    return make
