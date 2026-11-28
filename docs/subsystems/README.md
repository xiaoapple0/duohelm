# 子系统设计

修改某个子系统之前，先读它的设计文档，并核对文档中链接的源码。文档和源码不一致时，以源码为准，并在同一个 PR 里修正文档。

| 子系统 | 文档 | 源码 | 状态 |
|---|---|---|---|
| 插件体系 | [plugins.md](plugins.md) | `packages/duohelm-sdk/`、`packages/duohelm/src/duohelm/registry.py`、`packages/duohelm/src/duohelm/isolation/` | M0 已实现；操作系统层隔离见 #7 |
| 因子引擎与算子 | [factor-engine.md](factor-engine.md) | `packages/duohelm-sdk/src/duohelm_sdk/checks.py` | M0 只有契约检查 |
| 行情与数据 | [data.md](data.md) | `packages/duohelm-sdk/src/duohelm_sdk/schema.py`、`packages/duohelm/src/duohelm/storage/` | M1 进行中：存储与快照已实现 |
| 数据源按类别配置 | [data-sources.md](data-sources.md) | 设计（#99），未开始 | 设计中，未实现 |
| 任务系统 | [tasks.md](tasks.md) | `packages/duohelm/src/duohelm/tasks/`、`packages/duohelm/src/duohelm/api/tasks.py` | M0 已实现 |
| 市场与交易规则 | [market.md](market.md) | `packages/duohelm/src/duohelm/market/` | A 股涨跌停价、整手已实现（#45），规则来源未核对原文；交易日历、T+1、停牌未开始 |
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
- 长期运行时由 `duohelm serve` 直接提供构建好的页面（`duohelm.api.web`），页面和 API 同在 `http://127.0.0.1:<API 端口>`，只需要一个进程：
  - 页面目录：`DUOHELM_WEB_DIR`，默认仓库检出里的 `web/dist`（`cd web && npm ci && npm run build` 的输出）。目录里没有 `index.html` 时只提供 API，serve 启动时提示怎么构建
  - `/api/` 下的路径不返回页面：没有对应接口的仍是 404。其他 GET 路径，`dist` 里有这个文件就返回文件，没有就返回 `index.html`（前端用 BrowserRouter，`/sync` 等地址由前端路由处理）
  - 文件访问边界：只返回 `dist` 里的文件。含 `..`、`.`、空段、反斜杠、冒号（盘符）或以 `/` 开头的路径在访问文件系统之前就拒绝（在 Windows 上解析 `\\server\share` 会去连那台机器），解析（含符号链接）之后不在 `dist` 里的也不返回；这些请求都返回 `index.html`
  - 文件类型按扩展名用固定的表（`.js` 为 `text/javascript` 等），不按 `mimetypes` 猜：Windows 上它读注册表，`.js` 可能被登记成 `text/plain`，模块脚本会被浏览器拒绝
  - `index.html` 不缓存；`assets/` 下的文件名带内容哈希，长期缓存
  - 来源校验对页面同样生效：Host 不对返回 400。页面发出的写请求，Origin 是 API 端口本身，本来就在允许的来源里
- serve 的输出重定向到文件或管道时（例如任务计划程序写日志），命令行把 stdout、stderr 改成 UTF-8，避免按系统代码页编码时打印中文崩溃。
- 前端的接口类型由后端的 OpenAPI 生成，不手写：
  - `uv run python -m duohelm.api.openapi_export web/openapi.json`（`duohelm.api.openapi_export`）直接从 FastAPI app 取 `app.openapi()`，不启动服务、不访问网络，键排序、缩进固定，输出稳定
  - 在 `web/` 下运行 `npm run gen:api`：先导出 `web/openapi.json`，再用 `openapi-typescript`（开发依赖）生成 `web/src/api/schema.d.ts`。两个生成文件都提交，不要手改
  - 改了接口（路径、参数、响应模型）后必须重新生成并一起提交。`test_openapi_export.py` 检查 `web/openapi.json` 与后端一致，忘了就会失败；`schema.d.ts` 是否最新，用 `npm run gen:api` 后 `git diff --exit-code web/` 检查
  - `web/src/api/client.ts`、`data.ts` 只给生成的类型起前端用的名字。查询参数类型也取自生成的 `operations[...]["parameters"]["query"]`。手写的只剩 OpenAPI 里没有字段的内容：任务 `result`、`params` 是自由格式（生成类型里就是 `Record<string, unknown>`），`data.sync` 的结果类型 `DataSyncResult`，并在注释里说明
- 前端测试（在 `web/` 下运行，CI 的 job 名称是 `Web tests`）：
  - `npm test`：vitest 单元测试，测 `src/` 下的纯函数，文件命名为 `*.test.ts`，与被测文件放在一起（例如 `src/api/client.test.ts` 测 `query()`）。不需要浏览器
  - `npm run test:e2e`：Playwright 冒烟测试（`web/e2e/`），K 线查看、覆盖情况、任务中心、插件各一个。测试自己先 `npm run build`，再用 `vite preview` 在 127.0.0.1:4183 提供页面；`/api/**` 由 `page.route` 拦截并返回 `e2e/fixtures.ts` 里的固定数据，所以**不需要启动后端**，也不访问外网（127.0.0.1 以外的请求会被拦下并让测试失败）。断言页面的关键元素出现、没有预期之外的接口请求、控制台没有 error（antd 与 React 19 的兼容提示除外）
  - 第一次运行前安装浏览器：`npx playwright install chromium`（只用 Chromium）
  - 新增页面或接口时，在 `e2e/fixtures.ts` 里补上对应的固定数据，形状照 `src/api/` 里的类型

## 写一份新的子系统文档时

包含：职责、不负责什么、关键数据结构、对外契约、正确性要求、已知缺口（每个缺口都要有对应的 issue）。
