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
sdk = ">=0.4,<0.5"           # 兼容的 SDK 版本范围
author = "me"
description = "量价类自有因子"
entrypoint = "my_factors:plugin"   # 代码式插件的入口：模块:对象。纯表达式因子插件可省略

[permissions]
network = []                 # 需要访问的公网主机名或公网 IP（SDK 0.4 起只允许公网）。默认没有网络
# 本机或局域网的服务（如 MiniQMT）用 [[permissions.endpoints]]，见下文

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

### 联网地址 `permissions.network`（SDK 0.4 起只允许公网）

`network` 列出插件要连接的**公网**主机名或公网 IP，可以带端口（`api.example.com`、`1.2.3.4:443`）。

- 解析清单时拒绝本机、私有网段、链路本地、保留地址（`127.0.0.1`、`10.x`、`172.16–31.x`、`192.168.x`、`169.254.x`、`100.64/10`、文档保留地址等），以及 `localhost`、`*.localhost`、`*.local`、`*.lan`、`*.internal`、`*.home.arpa`。本机或局域网的服务用下面的 endpoints，由用户填写并确认地址
- **整个列表由用户确认一次**才放行：Web 插件页显示地址数量和完整列表，用户点“确认放行”（API：`PUT /api/plugins/{id}/network`，`confirm` 必须为 true，`digest` 必须是页面上显示的 `NetworkOut.digest`）。**确认之前插件不能联网**
- `digest` 防止确认一个没看过的列表：用户看到列表之后、点确认之前清单变了，哈希对不上，返回 409，不写入、不记审计，页面提示刷新后重新确认。endpoint 的确认同理（`EndpointOut.digest`）
- 列表的哈希（`network_digest`，排序后计算，顺序不同不算变化）变了需要重新确认，页面列出新增和去掉的地址，重新确认之前插件不能联网。插件升版本、改 endpoints 不影响 network 的确认
- 用户可以撤销确认（`DELETE /api/plugins/{id}/network`），撤销后插件不能联网
- 运行时，netguard 按**实际解析出的 IP** 检查：`network` 里的主机名解析到本机或私有网段（哪怕只有一个地址）就拦截，防止公网域名指向内网

### 用户可配置的连接地址（SDK 0.3 起）

需要连接用户自己的服务（例如局域网里另一台机器上的 MiniQMT）时，插件不把地址写进清单或代码，而是声明一个 endpoint：

```toml
[[permissions.endpoints]]
id = "miniqmt"                    # 小写字母开头，只含小写字母、数字、下划线
description = "MiniQMT 行情服务"   # 在平台的确认弹窗里给用户看
default = "127.0.0.1:58610"       # 默认地址，必须符合 scope；用户确认之前不放行
scope = "lan"                     # 用户能填的地址范围
```

插件代码用 `duohelm_sdk.endpoint("miniqmt", default="127.0.0.1:58610")` 取得地址（`Address(host, port)`）。在平台里运行时，返回用户确认过的地址，代码里的 `default` 被忽略；**用户还没确认时（包括需要重新确认时），平台不把任何地址交给插件，`endpoint()` 抛 `EndpointNotConfiguredError`**，消息提示用户去插件页确认，插件把它原样报告出来即可（实现了 `check_connection` 的，在这里返回 `ConnectionStatus(ok=False, message=str(exc))`）。不在平台里运行时（测试、独立脚本），返回代码里的 `default`。

scope 规则（`duohelm_sdk.endpoints.parse_address`）：

| scope | 接受 | 不接受 |
|---|---|---|
| `loopback` | 只有 `127.0.0.1`、`::1` | 主机名（包括 `localhost`）、其他地址 |
| `lan` | **只接受 IP 地址**：本机（`127.0.0.1`、`::1`）、10/8、172.16/12、192.168/16、fc00::/7 | 主机名（防止 DNS 重绑定）、公网地址、169.254/16 等链路本地地址 |
| `internet` | IP 地址或主机名；用户保存时需要额外确认 | — |

所有 scope 都要求写端口，不接受 `0.0.0.0`、组播地址、带网卡后缀的 IPv6 地址、协议和路径。

平台的流程（`duohelm.endpoints`、`duohelm.api.plugins`、`duohelm.isolation`）：

- **没确认过的 endpoint 一律不放行，包括清单的 `default`。** 插件页把 `default` 预先填好：scope 为 loopback、lan 时可以点“确认默认地址”一键确认；internet 范围要在弹窗里额外确认
- 用户在 Web 的插件页展开插件，点“修改地址”填写地址，勾选“我确认：插件 X 将可以连接 Y”后才保存（API：`PUT /api/plugins/{id}/endpoints/{endpoint_id}`，`confirm` 必须为 true；scope 为 internet 时还要 `confirm_internet`；`digest` 必须是页面上显示的 `EndpointOut.digest`，对不上返回 409，不写入、不记审计）。“清除”删除用户的地址（`DELETE`），之后回到未确认，插件不能连接
- 地址保存在 `DUOHELM_HOME/plugin-config.db`（表 `plugin_endpoints`），不进任何仓库
- 地址是否符合 scope 由平台校验两次，用的都是 `duohelm_sdk.parse_address`：
  - **保存时**：`EndpointStore.confirm`，不合格返回 422
  - **启动插件子进程、传地址之前**：`PluginSession.start` 调用 `checked_addresses`，不合格时不启动子进程，报 `load` 错误。错误信息会进任务日志，只写 endpoint 和 scope，不写地址
- 平台代码运行插件时用 `duohelm.isolation.session_for(entry, store)`，它从 `EndpointStore` 取出确认过的地址和 network 传给 `PluginSession`。`PluginSession` 的 `endpoints` 和 `network` 是必填的关键字参数，没有默认值，调用方不能漏传（#24）。目前运行插件的平台代码是同步任务 `data.sync`（见 [data.md](data.md) 的“同步任务”），用的是 `session_for`
- 需要重新确认的情况：清单里 `[[permissions.endpoints]]` 这一节变了。按这一节规范化 JSON 的 sha256 判断（`endpoints_digest`），插件升版本、改插件名称或说明、改 `network` 等清单其他部分的改动都不算；endpoint 自己的 `description`、`default`、`scope` 变了算。需要重新确认时，页面显示“需要重新确认，不能连接”；用户重新确认之前，插件拿不到地址（已保存的地址和 `default` 都不交给插件），也不放行
- endpoint 的确认、清除和 network 的确认、撤销，都记进审计表 `permission_audit`（对象、旧值、新值、当时的哈希、时间），只在本机数据库里。普通日志、质量报告和任务日志里不写具体地址
- 子进程收到 `start` 后、导入插件代码之前，按“**用户确认过的** `network` + 用户确认过的 endpoint 地址”设置网络白名单（`session_for` 从 `EndpointStore.network_for` 取 network，没确认时为空；`network` 传空时也不放行；传入的 network 必须是清单的子集），并设置 `duohelm_sdk.endpoint()` 能取到的地址（只有确认过的；没确认的只传 id，`endpoint()` 据此报“还没有确认”）。白名单逐个精确匹配主机和端口；白名单里的主机名必须解析到公网 IP，解析出来的公网 IP 按主机名那一项的端口规则放行。原生扩展（如 xtquant）和插件启动的子进程不经过这层拦截：平台不交出没确认的地址，所以按 `endpoint()` 取地址的插件（包括用原生扩展连接的）在用户确认之前连不上；但插件代码自己写死地址、交给原生扩展去连，这层拦不住（威胁模型见下，操作系统层的隔离见 #7）
- 实现了 `check_connection` 的插件，平台在隔离进程里调用它：`data.sync` 开始下载之前先调用，`ok=False` 时任务直接失败；没实现的插件跳过这一步（子进程报 `unsupported`）

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
with session_for(
    entry, EndpointStore(path), deps=[...], limits=Limits.from_env(), cancel=event, log=ctx.log
) as s:
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
| 网络 | **Python 层**拦截：替换 `socket` 模块里的连接、解析和发送入口，以及 Windows 上 asyncio Proactor 的连接和 UDP 发送；只放行用户确认过的 `permissions.network` 和 endpoint 地址（逐个匹配主机和端口），`network` 里的主机名必须解析到公网 IP；没确认的 endpoint 地址不交给插件；反向解析、Unix 套接字、`sendmsg` 一律拒绝 | 原生扩展、ctypes、厂商 DLL / so 直接调用系统接口（如 xtquant 连接 MiniQMT），插件代码写死地址交给它们时照样能连上；插件启动的子进程（如调用 curl）不继承拦截（#7） |
| 文件系统 | 不限制 | 插件可以读写当前用户能访问的任何文件 |
| 凭据 | 子进程拿不到任何凭据。存储和注入见 [#6](https://github.com/xiaoapple0/duohelm/issues/6) | — |

### 父子进程协议

- 控制消息走 stdin / stdout：4 字节长度加 JSON，单条不超过 8 MiB，两个方向都检查
- 子进程启动时先复制 fd 1 留给协议，再把 fd 1 重定向到 stderr，插件的 `print` 和原生库输出都不会破坏协议
- stderr 作为插件日志转给调用方，每次会话最多 256 KiB，超出部分丢弃，并记一条说明
- 数据表用 Arrow IPC 文件在会话临时目录里传递。读取时先把整个文件读进内存再解析，不做内存映射，读完立即删除文件（Windows 上被映射的文件删不掉）
- 子进程只接受规定的方法：因子插件只有 `compute`，行情源插件只有 `DataSourcePlugin` 协议里的方法，加上可选的 `check_connection`（SDK 0.3）。交易接口插件和算子插件不能单独在隔离进程里运行：算子只在依赖它的因子插件的子进程里加载，算子返回的 polars 表达式永远不跨进程
- 平台在发出 `start` 之后，子进程才导入插件代码；在此之前已经完成 fd 重定向、网络拦截，以及（Windows 上）加入 Job。`start` 消息带上网络白名单（`network`）、确认过的 endpoint 地址（`endpoints`）和没确认的 endpoint id（`unconfirmed_endpoints`），子进程先按它们设置好，再导入插件

### 错误分类（`PluginRunError.reason`）

| reason | 含义 | 会话还能继续用吗 |
|---|---|---|
| `plugin_error` | 插件自己抛出异常，附 traceback | 能 |
| `contract` | 返回值类型不对，或没有通过 `check_factor_output` | 能 |
| `timeout` / `memory` / `cancelled` | 被平台结束 | 不能 |
| `crashed` | 子进程意外退出 | 不能 |
| `load` | 插件状态不是 `ready`、种类不支持，或导入失败 | 不能 |
| `protocol` | 消息超长、格式不对、调用了不允许的方法 | 不能，平台会结束子进程 |
| `unsupported` | 调用了允许的可选方法（如 `check_connection`），但插件根本没有这个属性。属性存在但不可调用、或方法内部抛出 `NotImplementedError`，都算 `plugin_error`。可选方法要定义在类上：子进程用 `inspect.getattr_static` 判断，`__getattr__` 动态提供的方法认不出来 | 能 |
| `lookup_failed` | 插件抛了 `LookupFailedError`（SDK 0.4.2，`lookup_securities` 查询失败，结果未知）。子进程单独报这个类型，不退化成 `plugin_error`。`data.sync` 的退市股扫描专门处理它，见 `data.md` | 能 |

除 `timeout`、`crashed` 外，这些错误都不应作为 `RetryableError` 自动重试：同样的输入再跑一次，结果大概率不变。`data.sync` 把 `timeout`、`crashed` 当作可能是一时的问题（数据源暂时不可达、子进程被系统结束），转成 `RetryableError`，由任务系统按 `max_attempts` 重试；其他都直接失败。

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

**SDK 0.4.2（M1）**，兼容，只新增一个异常类（#56，维护者 2026-11-27 批准）：

- `LookupFailedError`（`duohelm_sdk.__all__`，继承 `Exception`）：`DataSourcePlugin.lookup_securities` 查询失败时抛它。**返回空结果仍表示“确定没有这个代码”**，抛这个异常表示“不知道”。此前接口分不清两者，数据源一时返回空会被记成“没找到”，要等重扫间隔才被重查
- 方法签名、返回格式没有变。不抛这个异常的旧插件行为不变：抛别的异常，平台照旧按 `plugin_error` 处理，整次同步失败
- 平台行为：隔离协议把它作为单独的错误类型（`lookup_failed`）传回，不会在子进程边界上退化成普通的 `plugin_error`。退市股扫描收到它，这批代码不写进扫描记录（不记“没找到”，也不更新查询时间），其余批次照常扫描，最后任务失败；下次同步照常重查。见 `data.md` 的“退市股扫描”
- 用到它的插件，清单的 `sdk` 改为 `">=0.4.2,<0.5"`：0.4.1 及以前的平台没有这个类，`import` 会失败，被判为 SDK 不兼容；不改清单则要在 0.4.2 之前的平台上自己兜底导入
- 迁移（插件仓库单独提 PR）：eltdx、xtdata 的 `lookup_securities` 里，把“连接失败、接口报错、返回结构异常”从“返回空”（或吞掉异常）改为抛 `LookupFailedError`；确认数据源没有这个代码才不返回它。不改也能运行，只是缺口依旧

**SDK 0.4.1（M1）**，兼容，只新增一个可选字段：

- `Capabilities.exact_delist_date`（默认 True）：`delist_date` 是否符合 SDK 口径（摘牌日之前的最后一个交易所交易日）。拿不到摘牌日、只能给近似值的数据源设为 False，平台把它给出的退市日记为近似值（数据集 `delist_date_quality`，见 `data.md`）
- 0.4.0 的插件不设这个字段，在 0.4.1 平台上取 dataclass 的默认值 True（插件和平台共用一个 Python 环境，子进程用的是平台的 SDK）。隔离协议解码时缺少这个字段也按 True，只作防御
- 默认值 True 只表示“插件没有声明是近似值”，不是平台核实过。拿不到摘牌日的插件必须声明 False
- 用到这个字段的插件，清单的 `sdk` 改为 `">=0.4.1,<0.5"`：0.4.0 的平台会把它判为 SDK 不兼容，而不是在构造 `Capabilities` 时出错

**SDK 0.4.0（M1）**，不兼容，有两项：`permissions.network` 的含义收紧，0.3 下有效的清单（`network` 里写了本机或局域网地址）在 0.4 下无效；`endpoint()` 遇到没确认的 endpoint 从返回 `default` 改为报错。按“改变含义是不兼容改动”升次版本号：

- `network` 只允许公网地址：解析清单时拒绝本机、私有网段、链路本地、保留地址（包括 `127.1`、`0x7f.0.0.1` 这类数字或十六进制写法），以及 `localhost`、`*.localhost`、`*.local`、`*.lan`、`*.internal`、`*.home.arpa`，报错信息提示改用 `[[permissions.endpoints]]`
- `endpoint()` 在平台里遇到用户还没确认的 endpoint（包括需要重新确认的），抛 `EndpointNotConfiguredError`，0.3 返回清单的 `default`。这样原生扩展（不经过 Python 层的网络拦截）在用户确认之前也拿不到地址（#27）
- `__all__`、协议、数据格式没有变化

同一版本里平台的行为（不是 SDK 接口，但插件作者需要知道）：

- `network` 的整个列表要用户在插件页确认后才放行，列表变了要重新确认
- 运行时 netguard 按实际解析出的 IP 检查：`network` 里的主机名解析到本机或私有网段时拦截
- 没确认过的 endpoint 一律不放行，包括清单的 `default`；插件也拿不到这个地址（见上面 `endpoint()` 那一条）

迁移：

- `network` 里写了本机或局域网地址的插件，改用 `[[permissions.endpoints]]`（例如 xtdata 的 `127.0.0.1:58610` 改为 `miniqmt` endpoint，scope 为 `lan`），清单的 `sdk` 写 `">=0.4,<0.5"`
- `network` 只写了公网地址（或为空）、并且没有用 `[[permissions.endpoints]]` 的插件不需要改代码，把清单的 `sdk` 放宽为 `">=0.3,<0.5"` 即可
- 用 endpoints 的插件：用户确认之前 `endpoint()` 会抛 `EndpointNotConfiguredError`，捕获后把消息报告给用户（实现了 `check_connection` 的，在这里返回 `ok=False` 和这条消息）；不要退回到代码里写死的地址
- 官方插件：xtdata 改用 `endpoint("miniqmt")`、实现 `check_connection`，`sdk` 写 `">=0.4,<0.5"`；eltdx 按上面的规则调整 `sdk` 范围。两者在插件仓库单独提 PR

**SDK 0.3.0（M1）**，只有新增，但清单多了字段，旧版平台无法解析（清单不接受未知字段），所以升次版本号：

- 清单新增可选的 `[[permissions.endpoints]]`（`manifest.Endpoint`：`id`、`description`、`default`、`scope`），见上文“用户可配置的连接地址”
- 新增 `duohelm_sdk.endpoints`：`EndpointScope`（`loopback` / `lan` / `internet`）、`Address`、`parse_address()`、`AddressError`
- 新增 `duohelm_sdk.context`：`endpoint()`、`in_platform()`、`EndpointNotConfiguredError`
- 新增可选协议 `SupportsConnectionCheck`：`check_connection() -> ConnectionStatus(ok, message)`。平台在同步任务开始时，如果插件实现了它就先调用一次，`ok` 为 False 时任务直接失败。它不是 `DataSourcePlugin` 的必需方法
- 以上名字都加入了 `duohelm_sdk.__all__`

迁移：

- 不用 endpoints 的插件不需要改代码，把清单的 `sdk` 放宽为 `">=0.2,<0.4"` 即可（0.3 只有新增，0.2 的插件在 0.3 的平台上照常运行）
- 用 endpoints 的插件，清单的 `sdk` 必须写 `">=0.3,<0.4"`
- 官方插件：xtdata 改用 `endpoint("miniqmt")`、实现 `check_connection`；eltdx 只放宽 `sdk` 范围。两者在插件仓库单独提 PR

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

- `SECURITIES_SCHEMA` 的 `delist_date` 明确为**摘牌日之前的最后一个交易所交易日**（按交易日历），不是这只证券最后一根 K 线的日期。原文“退市日（最后一个交易日）”对退市前停牌的证券有两种读法，这里定为前一种。数据源只给出最后一根 K 线、取不到摘牌日的插件，要在 README 里写明自己的口径，平台在质量报告里标出两个数据源的差异。**SDK 0.4.1 起**，这类插件还要声明 `Capabilities.exact_delist_date=False`，平台把它的退市日记为近似值（见上面 0.4.1 的变更记录）
- **不升 SDK 版本号**（维护者 2026-11-26 确认）：这是在原文的两种读法里选定一种，格式和字段都没变，不算“改变含义”的不兼容改动。按旧读法实现的插件（如 eltdx，取最后一根 K 线的日期）当作待修的 bug 处理，见 xiaoapple0/duohelm-plugins#4。eltdx 取不到摘牌日，最后改为声明近似值（xiaoapple0/duohelm-plugins#7）
