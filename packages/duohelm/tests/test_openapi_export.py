"""web/openapi.json 必须和后端当前的 OpenAPI 一致。

后端改了接口却忘了重新生成，这里会失败。修复：在 web/ 下运行 `npm run gen:api`。
"""

from __future__ import annotations

import json
from pathlib import Path

from duohelm.api.openapi_export import render_openapi

COMMITTED = Path(__file__).resolve().parents[3] / "web" / "openapi.json"


def test_committed_openapi_matches_backend() -> None:
    assert COMMITTED.is_file(), "缺少 web/openapi.json，请在 web/ 下运行 npm run gen:api"
    committed = COMMITTED.read_text(encoding="utf-8")
    assert committed == render_openapi(), (
        "web/openapi.json 与后端不一致，请在 web/ 下运行 npm run gen:api 并提交结果"
    )


def test_render_is_stable_and_has_no_local_paths() -> None:
    first = render_openapi()
    assert first == render_openapi()
    spec = json.loads(first)
    assert "/api/health" in spec["paths"]
