"""导出 OpenAPI 描述，供前端生成接口类型。

不启动服务、不访问网络：直接从 FastAPI app 取 ``app.openapi()``。
用法：``uv run python -m duohelm.api.openapi_export <输出文件>``（前端用 ``npm run gen:api``）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from duohelm.api.app import create_app
from duohelm.settings import Settings


def render_openapi() -> str:
    """稳定的 OpenAPI JSON 文本：键排序、缩进 2、末尾换行、不转义非 ASCII。"""
    # 数据根目录用临时目录，不碰 ~/.duohelm；描述里不含任何本机路径。
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Settings(home=Path(tmp)))
        spec = app.openapi()
        # create_app 打开了任务库和插件配置库；Windows 上要先释放才能删临时目录。
        store = getattr(app.state, "store", None)
        close = getattr(store, "close", None)
        if callable(close):
            close()
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：python -m duohelm.api.openapi_export <输出文件>", file=sys.stderr)
        return 2
    Path(argv[1]).write_text(render_openapi(), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
