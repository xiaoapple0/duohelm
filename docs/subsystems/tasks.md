# 任务系统

## 职责

- 持久化后台任务：类型、参数、状态、进度、结果、错误
- 调度执行：后台 worker 领取任务、运行 handler、写回结果
- 进度与日志：handler 汇报进度、写日志，页面按需拉取
- 失败处理：自动重试（只针对暂时性错误）、手动重试、取消、worker 中断后的恢复

## 不负责什么

- **隔离**：handler 在 serve 进程的线程里运行，只运行平台自己的代码。插件代码一律经由隔离子进程运行（`session_for`，见 [plugins.md](plugins.md) 的“隔离”），例如 `data.sync`
- **通用 cron**：任务系统只执行已经提交的任务。定时触发只支持“每个交易日 HH:MM”这一种规则，见下面的“定时触发”
- **交易委托**：见下面的“正确性要求”

## 源码

| 文件 | 内容 |
|---|---|
| `packages/duohelm/src/duohelm/tasks/store.py` | 表结构、迁移、状态流转、租约、写回保护 |
| `packages/duohelm/src/duohelm/tasks/types.py` | 任务类型、参数模型、注册表、`TaskContext` |
| `packages/duohelm/src/duohelm/tasks/worker.py` | worker 循环、心跳线程、重试判断、退避 |
| `packages/duohelm/src/duohelm/tasks/builtin.py` | 内置任务类型 |
| `packages/duohelm/src/duohelm/tasks/schedule.py` | 定时触发：规则存储、触发判断、后台线程 |
| `packages/duohelm/src/duohelm/api/tasks.py` | HTTP 接口 |
| `packages/duohelm/src/duohelm/api/schedules.py` | 定时规则的 HTTP 接口 |
| `web/src/pages/tasks/` | Web 任务中心页面；界面文字在 `text.ts` |

## 数据表

数据库文件为 `$DUOHELM_HOME/duohelm.db`，用 `PRAGMA user_version` 记录表结构版本。数据库版本高于程序支持的版本时拒绝启动。时间一律存 UTC 毫秒整数，对外转为带时区的 `datetime`。

- `tasks`：每个任务一行。关键字段：
  - `status`：任务状态
  - `attempt` / `max_attempts`：已执行次数和次数上限
  - `run_after`：不早于这个时间执行（退避用）
  - `cancel_requested`：是否已请求取消
  - `worker_id` / `lease_expires_at`：当前持有者和租约到期时间
  - `dedupe_key`：同一个 key 同时只允许一个未结束的任务（唯一部分索引）
- `task_logs`：只追加。每条带 `attempt`，按自增 `id` 增量拉取
- `schedules`（版本 2 新增）：定时规则，目前只有 `data.sync` 一行，见“定时触发”。迁移只新建这张表，旧数据不动

## 状态流转

```
             ┌──────── 取消 ────────┐
             │                      ▼
 创建 ──► queued ──领取──► running ──► succeeded
             ▲                │ │
             │   可重试的失败  │ └──取消请求被处理──► cancelled
             └──(attempt<max)─┤
                              └──不可重试，或次数用完──► failed
 failed / cancelled ──手动重试──► queued
```

- 允许的流转集中在 `ALLOWED_TRANSITIONS`。`succeeded` 是终态
- **取消**：`queued` 直接变为 `cancelled`。`running` 只设置 `cancel_requested`，由 handler 在检查点（`ctx.progress()`、`ctx.check_cancelled()`）抛出 `TaskCancelled` 退出。已请求取消的任务，失败或中断后一律变为 `cancelled`，不再重试
- **手动重试**：只接受 `failed` 和 `cancelled`。`attempt` 不清零，`max_attempts` 改为“当前 attempt + 该类型的默认次数”，所以第几次执行始终是连续的。若同一个 `dedupe_key` 已有未结束的任务，返回 409

## Worker

- `duohelm serve` 在应用生命周期内启动 `DUOHELM_TASK_WORKERS` 个 worker 线程，默认 1 个。关闭时等当前任务结束，最多等 30 秒；超时的任务留给下次启动时的租约恢复处理
- **领取**：一条 `UPDATE … WHERE id = (SELECT … WHERE status='queued' AND run_after<=now ORDER BY id LIMIT 1) AND status='queued' RETURNING *`，在 `BEGIN IMMEDIATE` 事务里执行，并发领取不会重复
- **租约**：默认 30 秒，由独立的心跳线程续期，续期间隔为租约时长的 1/3，不依赖 handler 汇报进度。续期返回 0 行，说明任务已被接管，handler 在下一个检查点收到 `TaskLost`
- **写回保护**：进度、结果、失败、取消的写回都要求 `id`、`worker_id`、`attempt` 全部匹配，并且状态仍是 `running`。只匹配 `worker_id` 不够：同一个 worker 卡住后可能重新领到自己的任务，`attempt` 用来区分新旧两次执行。写回 0 行时丢弃结果，记一条警告日志
- **恢复**：每次领取前，把租约已过期的 `running` 任务按“worker 中断”处理，视为一次可重试的失败
- **重试**：只有 handler 抛出的 `RetryableError` 和 worker 中断会自动重试。其他异常（包括参数校验失败、未注册的任务类型）直接变为 `failed`，并把 traceback 写入任务日志。退避依次为 10 秒、60 秒、300 秒，之后都是 300 秒；默认最多执行 3 次，可按任务类型设置

## SQLite 要求

- 版本不低于 3.35（`UPDATE … RETURNING`），启动时检查，不满足时拒绝启动
- WAL 模式；所有写事务用 `BEGIN IMMEDIATE`；`busy_timeout` 为 5 秒
- 每次操作打开一个连接，线程之间不共享连接

## 任务类型

任务类型在代码里注册，包含名称、参数模型（`TaskParams` 的子类，不接受未声明的字段）、handler 和 `max_attempts`。参数在提交时校验并规范化，执行前再校验一次。

| 类型 | 说明 | 何时注册 |
|---|---|---|
| `plugins.scan` | 重新扫描插件目录，结果为各状态的插件数 | 总是 |
| `data.sync` | 从行情源插件同步证券列表、交易日历、日线和复权因子，提交为一个快照（见 [data.md](data.md) 的“同步任务”） | 总是 |
| `demo.sleep` | 按步骤睡眠并汇报进度，可用 `fail_attempts` 模拟失败重试，用于开发和演示 | 仅 `DUOHELM_DEV=1` |

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/task-types` | 已注册的类型及参数的 JSON Schema |
| GET | `/api/tasks?status=&type=&limit=50&before_id=` | 列表，按 id 倒序，`before_id` 为游标 |
| POST | `/api/tasks` | `{type, params, dedupe_key?}`，返回 201。未知类型或参数不合法返回 422，`dedupe_key` 冲突返回 409 |
| GET | `/api/tasks/{id}` | 详情 |
| GET | `/api/tasks/{id}/logs?after_id=` | 增量日志 |
| POST | `/api/tasks/{id}/cancel` | 取消。已结束的任务返回 409 |
| POST | `/api/tasks/{id}/retry` | 手动重试。状态不对或 `dedupe_key` 冲突返回 409 |

## 定时触发

源码：`tasks/schedule.py`。作用：每个交易日收盘后自动提交一次增量 `data.sync`。它只是“按时提交任务”，任务的执行、重试、日志仍由任务系统负责。

**规则**：只支持“每个交易日 HH:MM（Asia/Shanghai）”，不做通用 cron。

- **时间必须晚于 15:30**，早于或等于 15:30 的配置被拒绝（422）。原因：`data.sync` 盘中的日线不能入库，当天的日线和 ST、成分快照只在北京时间 15:30 之后才写。默认 16:00
- **默认关闭**。用户启用并指定行情源插件 id（`plugin_id`）之后才触发。修改规则只改传入的字段，没有传的字段保持用户已设的值；平台的默认值只在从未配置过时使用
- **时间换算**：触发时刻、“今天”的日期都由当前时间按 Asia/Shanghai（固定 UTC+8）换算，不依赖本机时区
- **哪些日子触发**：周末不触发。当前快照的 `CN` 日历覆盖到今天（日历里有不早于今天的日期）时，按日历判断，今天不在日历里（节假日）就不触发。日历没有覆盖到今天时（例如日历只同步到昨天，或还没有任何快照）**照常触发**，由 `data.sync` 自己判断：非交易日没有日线，也不写快照，任务正常结束。读日历失败同样按“没覆盖”处理，不当成“不是交易日”
- **提交的任务**：`data.sync`，参数 `plugin_id` 加 `start`（触发当天往前 30 个自然日，复用现有参数，不新增参数），`end` 用默认值（最近一个已收盘的日子）。复权因子 `data.sync` 总是从 1990-12-19 请求，不受 `start` 影响；只写有变化的行，窗口内没有变化时不产生新快照。窗口让几天内没运行、或数据源修正过的日线也被补上；停机超过 30 天的缺口需要手动做一次全量同步
- **去重**：`dedupe_key` 为 `schedule:data.sync`。已有未结束（排队或运行中）的同类任务时，跳过这次触发并记日志，不重复提交。这一天仍记为“已触发”，不会每个检查周期重试
- **补跑**：serve 没运行时错过的触发，启动后只在“同一天、已过触发时间、当天还没触发过”时补跑一次，并记日志。更早的日子不补跑：过去日子的 ST、成分快照补不回来，日线会在下一次同步的窗口里补上。判断靠 `last_trigger_date`（上次处理的北京日期，触发、被去重跳过、判定为非交易日都会记下）：当天已处理过就不再触发，所以重启 serve 不会重复提交
- **启用或改时间的当天**：如果当时已经过了触发时间，不立即补跑，视为当天已处理，从下一个交易日开始触发；需要当天同步请手动提交
- **后台线程**：serve 在应用生命周期内启动一个调度线程，启动时立即检查一次（用于补跑），之后每 30 秒检查一次。线程不执行同步，只把任务放进队列，由 worker 执行。时钟可注入，测试用假时钟直接调用 `tick()`

**存储**：表 `schedules`（`name` 主键，`enabled`、`plugin_id`、`run_time`、`last_trigger_date`、`updated_at`），在 `$DUOHELM_HOME/duohelm.db`。

**下次触发时间**：从今天起逐日找第一个会触发的日子，按同样的周末、日历规则判断，最多往后看 14 天；今天已触发过的跳过，今天未触发且已过触发时间的仍返回今天（下一次检查就会触发）；未启用时为空。日历没覆盖的未来日子只能按周末判断，遇到节假日时实际不会触发，所以这是预估。

## HTTP 接口（定时规则）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/schedules/data-sync` | `{enabled, plugin_id, time, timezone, last_trigger_date, next_run_at}` |
| PATCH | `/api/schedules/data-sync` | `{enabled?, plugin_id?, time?}`，只改传入的字段，返回同上。`time` 格式 `HH:MM` 且晚于 15:30，否则 422；`enabled=true` 但没有 `plugin_id` 返回 422 |

页面（查看和修改规则、显示下次触发时间）不在这里做，归 [#33](https://github.com/xiaoapple0/duohelm/issues/33)（同步任务页面）。

## 正确性要求

- **handler 必须可以安全地重复执行。** 同一个任务可能因为自动重试、手动重试或 worker 中断而被执行多次，也可能在旧的一次执行还没退出时，新的一次已经开始。handler 的副作用必须是幂等的：例如先写到临时位置再原子替换，或按确定的键覆盖写入，不能用追加的方式写结果
- **交易委托永远不走这个任务系统的自动重试。** 下单、撤单等委托类操作重复执行会造成重复成交。交易执行子系统（`execution.md`）自己负责委托的幂等（客户端唯一 id）和断线后的对账。任务系统可以调度只读的查询类工作（如同步持仓用于对账），但任何会产生委托的 handler 都不得注册为可重试，也不得抛出 `RetryableError`
- 测试只用 `tmp_path` 下的数据库，时间用注入的假时钟推进，不依赖真实等待；只有心跳测试必须使用真实时间

## 已知缺口

- 历史任务和日志永久保留，没有清理：[#3](https://github.com/xiaoapple0/duohelm/issues/3)
- handler 在线程里运行，取消是协作式的。handler 不经过检查点时，无法中途停止
- 定时规则的页面还没有，只有 API，归 #33
- 定时触发不检查 `plugin_id` 是否存在、是否 `ready`：配错时每个交易日提交一个会失败的 `data.sync`，失败在任务中心可见
- 任务中心页面只显示最近 100 个任务，还没有翻页（接口已支持 `before_id` 游标）
