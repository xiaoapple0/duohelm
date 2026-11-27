"""导出 OpenAPI 描述，供前端生成接口类型。

不启动服务、不访问网络：直接从 FastAPI app 取 ``app.openapi()``。
用法：``uv run python -m duohelm.api.openapi_export <输出文件>``（前端用 ``npm run gen:api``）。
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path

from duohelm.api.app import create_app
from duohelm.settings import Settings

logger = logging.getLogger(__name__)


def render_openapi(home: Path | None = None) -> str:
    """稳定的 OpenAPI JSON 文本：键排序、缩进 2、末尾换行、不转义非 ASCII。

    home 是 create_app 用的数据根目录，默认用临时目录，不碰 ~/.duohelm；
    描述里不应出现任何本机路径（测试会检查）。TaskStore、EndpointStore 每次操作自己开关连接，
    所以临时目录可以直接删除。
    """
    if home is not None:
        return _render(home)
    with tempfile.TemporaryDirectory() as tmp:
        return _render(Path(tmp))


def _render(home: Path) -> str:
    spec = create_app(Settings(home=home)).openapi()
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(argv) != 2:
        logger.error("用法：python -m duohelm.api.openapi_export <输出文件>")
        return 2
    Path(argv[1]).write_text(render_openapi(), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
