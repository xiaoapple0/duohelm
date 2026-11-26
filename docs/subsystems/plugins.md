# 插件体系

## 职责划分

| 平台负责 | 插件作者负责 |
|---|---|
| 插件接口、清单格式、数据标准格式（契约） | 插件逻辑的正确性 |
| 发现、校验、依赖解析、启用、停用、版本回退 | 插件的更新和 bug 修复 |
| 隔离运行：子进程、超时、内存、网络限制 | 插件的性能 |
| 契约检查：输出格式、未来函数、确定性 | 因子是否有效、数据是否准确 |
| 调度、缓存、凭据注入、日志、错误展示 | 插件自身的依赖 |

插件作者只依赖 `duohelm-sdk`，不依赖 `duohelm` 平台包。

## 四类插件

| 类型 (`kind`) | 提供什么 | 接口（`duohelm_sdk.protocols`） |
|---|---|---|
| `datasource` 行情源 | 证券列表（含退市）、交易日历、K 线、复权因子、指数成分股 | `DataSourcePlugin` |
| `operator` 算子 | 返回 polars 表达式的计算函数 | `OperatorPlugin` |
| `factor` 因子 | 因子定义（表达式或代码） | `FactorPlugin` |
| `broker` 交易接口 | 账户、持仓、下单、撤单、委托查询 | `BrokerPlugin` |

## 插件目录和清单

每个插件是一个目录，根目录有一个 `duohelm-plugin.toml`：

```toml
[plugin]
id = "my-factors"            # 小写字母开头，只含小写字母、数字、连字符
name = "自有因子"
version = "1.2.0"            # 语义化版本
kind = "factor"
sdk = ">=0.2,<0.3"           # 兼容的 SDK 版本范围
author = "me"
description = "量价类自有因子"
entrypoint = "my_factors:plugin"   # 代码式插件的入口：模块:对象。纯表达式因子插件可省略

[permissions]
network = []                 # 需要访问的主机名。默认没有网络

[[requires]]                 # 依赖的其他插件
id = "ops-industry"
version = ">=1.0"

[[factors]]                  # 仅 kind = "factor"
id = "vol_price_corr_10"
name = "10日量价相关性"
category = "量价"
expression = "-1 * Corr(Rank($close), Rank($volume), 10)"
fields = ["close", "volume"]
lookback = 10
description = "价格和成交量排名的10日相关系数取反"
```

清单的权威定义是 `packages/duohelm-sdk/src/duohelm_sdk/manifest.py`。选用 TOML 是因为 Python 标准库自带解析器，SDK 不需要额外依赖。

## 加载流程

`duohelm.registry.PluginRegistry` 扫描插件目录，对每个插件：

1. **解析清单**：格式错误 → 状态 `invalid`，记录原因
2. **检查 SDK 兼容性**：当前 SDK 版本不在 `sdk` 范围内 → `incompatible`
3. **检查重复**：同一个 `id` 出现多次 → 全部标为 `invalid`
4. **解析依赖**：依赖缺失、版本不满足、依赖本身不可用 → `blocked`，列出原因。依赖传递生效
5. 以上都通过 → `ready`

`ready` 只表示"可以加载"。因子插件还要通过契约检查（M3）才能启用。

## 契约检查

由 `duohelm_sdk.checks` 提供。插件作者可以在自己的 CI 里跑同一套检查。

| 检查 | 适用 | 状态 |
|---|---|---|
| 输出格式：列名、类型、无重复 (date, symbol) | 因子 | M0 已实现 |
| 未来函数扰动：打乱截止日之后的数据，截止日及之前的值必须不变 | 因子、算子 | M0 已实现 |
| 确定性：同一输入两次结果一致 | 因子、算子 | M0 已实现 |
| 行情数据格式与质量 | 行情源 | M1 |
| 交易接口模拟回放 | 交易接口 | M6 |

**未来函数扰动检查的局限**：它只能发现"截止日之后的数据改变了之前的结果"，抓不到数据本身就带未来信息的情况（比如行情源返回的复权因子是用今天的价格算的）。后一类问题由行情源的契约检查负责（M1）。

## 隔离

源码：`packages/duohelm/src/duohelm/isolation/`。用法：

```python
with PluginSession(entry, deps=[...], limits=Limits.from_env(), cancel=event, log=ctx.log) as s:
    out = s.compute("factor_id", data)  # 返回前已用 check_factor_output 校验
    check_no_lookahead(lambda d: s.compute("factor_id", d), data, ...)
```

### 威胁模型

**M0 到 M2 期间，插件来自维护者自己或可信来源。隔离用于防止有错误的代码（死循环、内存泄漏、误用网络、意外读到平台的环境变量）拖垮平台，不防恶意代码。运行第三方插件之前，必须先实现操作系统层的隔离（[#7](https://github.com/xiaoapple0/duohelm/issues/7)）。**

| 方面 | M0 做到了什么 | 防不住什么 |
|---|---|---|
| 进程 | 每次会话一个子进程：`python -I -m duohelm.isolation.child`，工作目录是会话临时目录，结束后删除 | — |
| 超时 | 每次调用 300 秒（`DUOHELM_PLUGIN_TIMEOUT`），超时后结束整棵进程树 | — |
| 结束进程树 | Windows 用 Job Object，进程以挂起状态启动、加入 Job 后再恢复，venv 启动器之后启动的解释器和插件启动的所有进程都在 Job 里；加入 Job 失败时不恢复，直接结束这个挂起的进程并报错；POSIX 用独立的进程组，外加 psutil 找到的子进程 | POSIX 上调用 `setsid` 脱离进程组、并且在结束时已经找不到的孙进程 |
| 内存 | 整棵进程树 2 GiB（`DUOHELM_PLUGIN_MEMORY_MB`）。Windows 是 Job Object 硬限制；所有平台的父进程每 0.5 秒检查一次 RSS，超限就结束 | Linux 和 macOS 上两次检查之间的瞬时峰值 |
| 环境变量 | 只继承白名单里的系统变量；`HOME`、`TEMP` 等指向会话临时目录。`DUOHELM_*`、代理设置和一切凭据都不传 | — |
| 网络 | **Python 层**拦截：替换 `socket` 模块里的连接、解析和发送入口，M0 不放行任何主机。清单中的 `permissions.network` 在 M1 连同用户确认流程一起实现 | 原生扩展、ctypes、厂商 DLL / so 直接调用系统接口 |
| 文件系统 | 不限制 | 插件可以读写当前用户能访问的任何文件 |
| 凭据 | 子进程拿不到任何凭据。存储和注入见 [#6](https://github.com/xiaoapple0/duohelm/issues/6) | — |

### 父子进程协议

- 控制消息走 stdin / stdout：4 字节长度加 JSON，单条不超过 8 MiB，两个方向都检查
- 子进程启动时先复制 fd 1 留给协议，再把 fd 1 重定向到 stderr，插件的 `print` 和原生库输出都不会破坏协议
- stderr 作为插件日志转给调用方，每次会话最多 256 KiB，超出部分丢弃，并记一条说明
- 数据表用 Arrow IPC 文件在会话临时目录里传递。读取时先把整个文件读进内存再解析，不做内存映射，读完立即删除文件（Windows 上被映射的文件删不掉）
- 子进程只接受规定的方法：因子插件只有 `compute`，行情源插件只有协议里的五个方法。交易接口插件和算子插件不能单独在隔离进程里运行：算子只在依赖它的因子插件的子进程里加载，算子返回的 polars 表达式永远不跨进程
- 平台在发出 `start` 之后，子进程才导入插件代码；在此之前已经完成 fd 重定向、网络拦截，以及（Windows 上）加入 Job

### 错误分类（`PluginRunError.reason`）

| reason | 含义 | 会话还能继续用吗 |
|---|---|---|
| `plugin_error` | 插件自己抛出异常，附 traceback | 能 |
| `contract` | 返回值类型不对，或没有通过 `check_factor_output` | 能 |
| `timeout` / `memory` / `cancelled` | 被平台结束 | 不能 |
| `crashed` | 子进程意外退出 | 不能 |
| `load` | 插件状态不是 `ready`、种类不支持，或导入失败 | 不能 |
| `protocol` | 消息超长、格式不对、调用了不允许的方法 | 不能，平台会结束子进程 |

这些错误都不应作为 `RetryableError` 自动重试：同样的输入再跑一次，结果大概率不变。

### 其他

- 插件和平台共用同一个 Python 环境（venv），插件自身的依赖需要装在这个环境里
- 不支持上传编译好的二进制（DLL / so / dylib）

## 契约变更

以下内容的任何改动都是契约变更，**需要维护者确认**：

- `duohelm_sdk` 的公开 API（`__all__` 中的名字和 `protocols.py`）
- 清单格式（`manifest.py`）
- 数据标准格式（`schema.py`）

规则：
- SDK 遵循语义化版本。0.x 阶段，不兼容改动升次版本号（0.1 → 0.2）
- 不兼容改动必须在 PR 里写迁移说明，并同步更新官方参考插件
- 新增可选字段是兼容改动；删除字段、改变含义、新增必填字段是不兼容改动
- **枚举以后可能新增取值**，新增取值是兼容改动。契约中的枚举包括 `SECURITIES_SCHEMA` 的 `kind` 和 `exchange`、`Freq`、`Capabilities.markets` 等。使用方（平台和插件）遇到不认识的取值时，**跳过这一项并记一条日志，不能报错**：
  - 证券列表用 `schema.known_securities()` 过滤，`checks.check_securities()` 对不认识的取值只做说明，不判为不合格
  - 隔离协议解码 `Capabilities` 时，跳过不认识的 `Freq`
  - 清单字段（如插件的 `kind`）不在此列：清单受 `sdk` 版本范围约束，用了新取值的插件会被判为 SDK 不兼容，而不是被当作错误

### 变更记录

**SDK 0.2.0（M1）**，不兼容：

- `DataSourcePlugin` 新增两个必需方法，都返回 `schema.SECURITIES_SCHEMA`：
  - `securities(*, include_delisted=False)`：证券列表，默认只含在市证券
  - `lookup_securities(symbols)`：查询给定代码，可以包含已退市或不存在的代码；只返回有记录的代码
- `Capabilities` 新增可选字段 `lists_delisted`（默认 False），声明能否直接列出退市证券：
  - 为 True（如 xtdata）：平台调用 `securities(include_delisted=True)` 直接取完整名单
  - 为 False（如 eltdx，代码表只有在市证券）：平台用 `lookup_securities` 扫描代码区间，扫描结果保存在平台，不重复扫描
- `adj_factors()` 的 `factor` 明确定义为**累计后复权比例**，自除权除息日起生效，只能由该日及以前的数据决定（`schema.ADJ_FACTORS_SCHEMA`）
- 新增 `duohelm_sdk.adjust`：`factors_from_events()` 由除权除息事件计算因子；`adjust_bars()` 计算后复权或以指定日期为基准的前复权
- 新增检查：`checks.check_securities()`、`checks.check_adj_factors()`；新增 `schema.known_securities()`

迁移：

- 行情源插件实现 `securities()` 和 `lookup_securities()`；能直接列出退市证券的，在 `capabilities()` 里设置 `lists_delisted=True`，并支持 `include_delisted=True`
- 按新定义返回 `adj_factors()`，推荐直接使用 `factors_from_events()`
- 清单的 `sdk` 改为 `">=0.2,<0.3"`
- 目前还没有官方参考插件需要同步更新

**SDK 0.2 口径说明（M1，不改格式和版本号）**：

- `SECURITIES_SCHEMA` 的 `delist_date` 明确为**摘牌日之前的最后一个交易所交易日**（按交易日历），不是这只证券最后一根 K 线的日期。原文“退市日（最后一个交易日）”对退市前停牌的证券有两种读法，这里定为前一种。数据源只给出最后一根 K 线、取不到摘牌日的插件，要在 README 里写明自己的口径，平台在质量报告里标出两个数据源的差异
- **不升 SDK 版本号**（维护者 2026-11-26 确认）：这是在原文的两种读法里选定一种，格式和字段都没变，不算“改变含义”的不兼容改动。按旧读法实现的插件（如 eltdx，取最后一根 K 线的日期）当作待修的 bug 处理，见 xiaoapple0/duohelm-plugins#4
