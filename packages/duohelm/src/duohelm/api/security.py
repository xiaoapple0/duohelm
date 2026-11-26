"""整个 API 的来源校验：Host 头和 Origin 头。

现阶段 API 没有登录，只监听本机。两道检查防的是浏览器里的恶意网页：

- **Host**：只接受 ``127.0.0.1:<端口>`` 和 ``localhost:<端口>``（端口是 API 的端口或 Web
  开发服务器的端口）。DNS 重绑定的网页把自己的域名解析到 127.0.0.1 时，浏览器发来的 Host 是
  那个域名，会被拒绝（400）
- **Origin**：POST、PUT、PATCH、DELETE 必须带 Origin 头，且是平台自己的来源
  （``http://127.0.0.1:<端口>`` 或 ``http://localhost:<端口>``），否则拒绝（403）。浏览器发出的
  写请求都会带 Origin；不带 Origin 的写请求（命令行脚本等）也拒绝

监听非本机地址（``duohelm serve --host 0.0.0.0``）时，局域网里的访问因为 Host 不对会被拒绝。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from duohelm.settings import Settings

LOCAL_NAMES = ("127.0.0.1", "localhost")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def allowed_hosts(settings: Settings) -> frozenset[str]:
    ports = {settings.port, settings.web_port}
    return frozenset(f"{name}:{port}" for name in LOCAL_NAMES for port in ports)


def allowed_origins(settings: Settings) -> frozenset[str]:
    return frozenset(f"http://{host}" for host in allowed_hosts(settings))


def install_origin_guard(app: FastAPI, settings: Settings) -> None:
    hosts = allowed_hosts(settings)
    origins = allowed_origins(settings)

    @app.middleware("http")
    async def guard(  # pyright: ignore[reportUnusedFunction]  由 FastAPI 注册
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.headers.get("host", "").strip().lower()
        if host not in hosts:
            return JSONResponse(
                {"detail": "请求的 Host 不被接受：只接受 127.0.0.1 或 localhost 加端口"},
                status_code=400,
            )
        if request.method in WRITE_METHODS:
            origin = request.headers.get("origin", "").strip().lower()
            if origin not in origins:
                return JSONResponse(
                    {"detail": "写请求必须来自平台自己的页面（Origin 不被接受）"},
                    status_code=403,
                )
        return await call_next(request)
