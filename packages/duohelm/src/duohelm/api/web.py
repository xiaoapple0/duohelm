"""托管构建好的 Web 页面（``web/dist``），长期运行时只需要 ``duohelm serve`` 一个进程。

- ``/api/`` 下的路径不在这里处理：没有对应接口的仍返回 API 的 404，不返回页面
- 其他 GET 路径：``dist`` 里有这个文件就返回文件，没有就返回 ``index.html``，由前端路由
  （BrowserRouter）处理 ``/sync``、``/plugins`` 等页面地址
- 只返回 ``dist`` 目录里的文件：含 ``..``、反斜杠、盘符、绝对路径的请求在访问文件系统之前
  就被拒绝，解析（含符号链接）之后不在目录里的也不返回；这些都返回 ``index.html``
- 页面和 API 同源（``http://127.0.0.1:<API 端口>``），Host 和 Origin 校验（``security.py``）
  对页面同样生效，同源的 Origin 本来就在允许列表里
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

INDEX = "index.html"
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".wasm": "application/wasm",
}
"""构建产物的类型不按 mimetypes 猜：Windows 上 mimetypes 会读注册表，有的机器把 .js 登记成
text/plain，浏览器对 ``<script type="module">`` 严格检查类型，页面会白屏。表里没有的扩展名
仍按 mimetypes。"""


def has_web_build(web_dir: Path | None) -> bool:
    return web_dir is not None and (web_dir / INDEX).is_file()


def install_web(app: FastAPI, web_dir: Path) -> None:
    """在所有 API 路由之后注册，放在最后才不会挡住 /api、/docs 等已有路由。"""
    root = web_dir.resolve()
    index = root / INDEX

    def page(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        target = _inside(root, path)
        if target is not None and target.is_file() and target != index:
            # 构建产物的文件名带内容哈希，可以长期缓存
            cache = "public, max-age=31536000, immutable" if _is_asset(root, target) else "no-cache"
            return FileResponse(
                target,
                media_type=MEDIA_TYPES.get(target.suffix.lower()),
                headers={"Cache-Control": cache},
            )
        # index.html 不缓存：升级后刷新页面就能拿到新版本
        return FileResponse(
            index, media_type=MEDIA_TYPES[".html"], headers={"Cache-Control": "no-cache"}
        )

    app.add_api_route("/{path:path}", page, methods=["GET"], include_in_schema=False)


def _inside(root: Path, path: str) -> Path | None:
    # 先按字面检查，再碰文件系统：反斜杠、盘符、以 / 开头的路径在 Windows 上可能指向
    # 别的盘或网络路径（\\server\share），解析它们本身就会访问那个位置。
    if not path or "\\" in path or ":" in path or path.startswith("/"):
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    try:
        target = (root / path).resolve()
    except (OSError, ValueError):
        return None
    return target if target.is_relative_to(root) else None


def _is_asset(root: Path, target: Path) -> bool:
    return target.is_relative_to(root / "assets")
