# 子系统设计

修改某个子系统之前，先读它的设计文档，并核对文档中链接的源码。文档和源码不一致时，以源码为准，并在同一个 PR 里修正文档。

| 子系统 | 文档 | 源码 | 状态 |
|---|---|---|---|
| 插件体系 | [plugins.md](plugins.md) | `packages/duohelm-sdk/`、`packages/duohelm/src/duohelm/registry.py` | M0 进行中 |
| 因子引擎与算子 | [factor-engine.md](factor-engine.md) | `packages/duohelm-sdk/src/duohelm_sdk/checks.py` | M0 只有契约检查 |
| 行情与数据 | [data.md](data.md) | `packages/duohelm-sdk/src/duohelm_sdk/schema.py` | M0 只有数据格式 |
| 任务系统 | [tasks.md](tasks.md) | `packages/duohelm/src/duohelm/tasks/`、`packages/duohelm/src/duohelm/api/tasks.py` | M0 后端已实现，页面进行中 |
| 交易执行 | [execution.md](execution.md) | 未开始 | M6 |
| API 与 Web | 本页下方 | `packages/duohelm/src/duohelm/api/`、`web/` | M0 进行中 |

## API 与 Web

- 所有功能先在核心库实现，再由 FastAPI 暴露，Web 只调 API。
- API 路径统一以 `/api/` 开头。
- 默认只监听 `127.0.0.1`。现阶段没有登录，**不得**默认监听 `0.0.0.0`。
- 前端开发时由 Vite 代理 `/api` 到后端（见 `web/vite.config.ts`）。
- 前端的接口类型将从 OpenAPI 生成（M1 引入），之前手写的类型只放在 `web/src/api/` 一处。

## 写一份新的子系统文档时

包含：职责、不负责什么、关键数据结构、对外契约、正确性要求、已知缺口（每个缺口都要有对应的 issue）。
