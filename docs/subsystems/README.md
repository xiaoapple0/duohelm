# 子系统设计

修改某个子系统之前，先读它的设计文档，并核对文档中链接的源码。文档和源码不一致时，以源码为准，并在同一个 PR 里修正文档。

| 子系统 | 文档 | 源码 | 状态 |
|---|---|---|---|
| 插件体系 | [plugins.md](plugins.md) | `packages/duohelm-sdk/`、`packages/duohelm/src/duohelm/registry.py`、`packages/duohelm/src/duohelm/isolation/` | M0 已实现；操作系统层隔离见 #7 |
| 因子引擎与算子 | [factor-engine.md](factor-engine.md) | `packages/duohelm-sdk/src/duohelm_sdk/checks.py` | M0 只有契约检查 |
| 行情与数据 | [data.md](data.md) | `packages/duohelm-sdk/src/duohelm_sdk/schema.py`、`packages/duohelm/src/duohelm/storage/` | M1 进行中：存储与快照已实现 |
| 任务系统 | [tasks.md](tasks.md) | `packages/duohelm/src/duohelm/tasks/`、`packages/duohelm/src/duohelm/api/tasks.py` | M0 已实现 |
| 交易执行 | [execution.md](execution.md) | 未开始 | M6 |
| API 与 Web | 本页下方 | `packages/duohelm/src/duohelm/api/`、`web/` | M0 进行中 |

## API 与 Web

- 所有功能先在核心库实现，再由 FastAPI 暴露，Web 只调 API。
- API 路径统一以 `/api/` 开头。
- 默认只监听 `127.0.0.1`。现阶段没有登录，**不得**默认监听 `0.0.0.0`。
- 来源校验（`duohelm.api.security`，对整个 API 生效）：
  - Host 头只接受 `127.0.0.1:<端口>`、`localhost:<端口>` 和 `[::1]:<端口>`，端口是 API 的端口或 Web 开发服务器的端口（`DUOHELM_WEB_PORT`，默认 5173），否则返回 400。防的是 DNS 重绑定：恶意网页把自己的域名解析到 127.0.0.1 时，浏览器发来的 Host 仍是那个域名
  - POST、PUT、PATCH、DELETE 带了 Origin 头时，必须是平台自己的来源（`http://` 加上面接受的 Host），否则返回 403。浏览器跨站发出的写请求都会带 Origin，所以恶意网页过不了这一关；`Origin: null` 和空的 Origin 也拒绝。不带 Origin 的写请求（命令行脚本、curl）放行，Host 校验已经限定只能从本机访问
  - 监听非本机地址时，局域网里的访问会因为 Host 不对被拒绝
- 前端开发时由 Vite 代理 `/api` 到后端（见 `web/vite.config.ts`）。
- 前端的接口类型将从 OpenAPI 生成（M1 引入），之前手写的类型只放在 `web/src/api/` 一处。

## 写一份新的子系统文档时

包含：职责、不负责什么、关键数据结构、对外契约、正确性要求、已知缺口（每个缺口都要有对应的 issue）。
