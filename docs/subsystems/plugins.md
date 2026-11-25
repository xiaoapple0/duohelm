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
| `datasource` 行情源 | 交易日历、K 线、复权因子、指数成分股 | `DataSourcePlugin` |
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
sdk = ">=0.1,<0.2"           # 兼容的 SDK 版本范围
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

## 隔离（M0 未实现）

- 每个插件在独立子进程中运行，限制超时和内存
- 默认没有网络；`permissions.network` 里的主机需用户在页面上确认后放行
- 凭据由平台注入，插件代码中不得出现凭据
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
