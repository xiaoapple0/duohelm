# 在自己的 Windows 机器上长期运行

这份手册讲怎么从零开始，在维护者自己的 Windows 机器上长期运行 DuoHelm：装好 xtdata、eltdx 两个行情源插件，第一次全量同步，然后每个交易日 16:00 自动增量同步。

为什么要尽早长期运行：xtdata 普通版和 eltdx 都拿不到历史 ST 状态和历史指数成分，平台只能从第一次同步起**每天保存一份快照**（见 [data.md](subsystems/data.md) 的“ST 名单和指数成分快照”）。晚一天开始，就永远少一天这类 point-in-time 数据。

下文的路径是示例，按自己的机器替换：

| 示例路径 | 是什么 |
|---|---|
| `D:\AiCode\duohelm` | DuoHelm 的开发检出（agent 在这里工作） |
| `D:\AiCode\duohelm-plugins` | 插件仓库 duohelm-plugins 的检出 |
| `D:\duohelm-run` | 专门运行的检出（下面新建） |
| `D:\AiCode\xtquant_250807` | 迅投发布的 xtquant 所在目录（里面有 `xtquant\` 子目录） |
| `%USERPROFILE%\.duohelm` | 数据目录 |

需要：[uv](https://docs.astral.sh/uv/)、Git、Node.js 22 以上（只用来构建页面）。

## 1. 数据目录

平台的所有数据都放在 `DUOHELM_HOME` 下，默认是 `%USERPROFILE%\.duohelm`（`C:\Users\<用户名>\.duohelm`）。源码：`packages/duohelm/src/duohelm/settings.py`。

| 路径 | 用途 |
|---|---|
| `plugins\` | 插件目录。平台只扫描它的**直接子目录**，子目录里有 `duohelm-plugin.toml` 才算一个插件 |
| `data\` | 行情数据：Parquet 文件、清单和数据目录库（快照） |
| `duohelm.db` | 元数据库（SQLite）：任务、任务日志、定时规则 |
| `plugin-config.db` | 插件页里确认过的连接地址、联网放行和审计记录。**MiniQMT 的实际地址只存在这里**，不进任何仓库 |
| `serve.log` | 下面的启动脚本写的运行日志（不是平台自己建的） |

- 这是真实数据，不要放在 `%APPDATA%` 下：Claude 桌面应用会把写入 `%APPDATA%` 的内容重定向到它的私有目录（见 LESSONS.md 的“开发环境”）
- 同一个数据目录只能有一个 `duohelm serve` 在运行。两个 serve 共用一个数据目录时，两边的定时线程、任务 worker 都会工作。开发时要另外启动 serve，用别的端口**和**别的数据目录（`$env:DUOHELM_HOME = "D:\tmp\duohelm-dev"`）

## 2. 运行用的检出：和开发检出分开

不要在开发检出（`D:\AiCode\duohelm`）里长期运行，另建一个只用来运行的检出：

- agent 会在开发检出里切换分支、拉取代码，正在运行的平台会被换成别的代码
- Windows 上，运行中的进程会锁住 `.venv` 里的文件（`duohelm.exe`、`.pyd`），这时在同一个检出里 `uv sync` 会失败

用 `git worktree` 从开发检出建一个运行目录，固定在某个版本上（detached，不跟分支）：

```powershell
cd D:\AiCode\duohelm
git fetch origin --tags
git worktree add --detach D:\duohelm-run <tag>
```

> 目前仓库还没有打过 tag。在有 tag 之前，用 main 上一个确定的提交代替 `<tag>`，例如 `origin/main`（建好后它就停在那个提交上，不会跟着 main 变）。以后有了 tag，升级时改用 tag。

然后安装依赖、构建页面：

```powershell
cd D:\duohelm-run
uv sync --no-dev
uv pip install "eltdx==3.3.5" tzdata pandas numpy
cd web
npm ci
npm run build
cd ..
```

- `uv sync --no-dev`：只装运行需要的包，不装 pytest、ruff、pyright
- 第二行装的是**插件的依赖**，不是平台的依赖（见第 3 节）。**每次 `uv sync` 之后都要重新运行这一行**：`uv sync` 会删掉锁文件里没有的包（已实测），插件的依赖会被删掉，插件运行时报找不到 eltdx、pandas
- `npm run build` 生成 `web\dist`，`duohelm serve` 启动时自动提供这些页面（`DUOHELM_WEB_DIR` 可以改成别的目录）。页面和 API 都在 `http://127.0.0.1:8765`，只需要这一个进程

检查一下：

```powershell
.\.venv\Scripts\duohelm.exe plugins
```

现在还没有装插件，会输出“没有找到插件（插件目录: ...）”，并显示实际使用的插件目录。

### 升级

1. 停止 serve（见第 5 节）
2. 切到新版本、重新安装依赖、重新构建页面：

   ```powershell
   cd D:\duohelm-run
   git fetch origin --tags
   git checkout --detach <新 tag>
   uv sync --no-dev
   uv pip install "eltdx==3.3.5" tzdata pandas numpy
   cd web; npm ci; npm run build; cd ..
   ```

3. 插件有更新时，按第 3 节重新复制插件
4. 重新启动 serve

数据目录不动。元数据库的表结构由平台在启动时自动迁移。

## 3. 安装插件

### 复制插件目录

平台扫描 `%USERPROFILE%\.duohelm\plugins\` 的直接子目录（`packages/duohelm/src/duohelm/registry.py`）。把插件仓库里的 `plugins\xtdata`、`plugins\eltdx` **复制**过去：

```powershell
$plugins = "$env:USERPROFILE\.duohelm\plugins"
foreach ($p in "xtdata", "eltdx") {
    robocopy "D:\AiCode\duohelm-plugins\plugins\$p" "$plugins\$p" /MIR /XF miniqmt.toml /XD __pycache__ tests .pytest_cache
}
```

- 用复制，不用目录链接（junction / 符号链接）。平台扫描时会跟随链接，链接也能用，但插件仓库的检出同样会被 agent 切换分支，正在运行的插件代码会跟着变；而且 xtdata 按**解析链接之后**的位置找 `miniqmt.toml`（`duohelm_xtdata/config.py` 用 `Path(__file__).resolve()`），配置文件会落进插件仓库的检出里
- 复制之前确认插件仓库的检出在 main 上、没有未提交的改动
- `/MIR` 会让目标目录和源目录一致，删掉多出来的旧文件；`/XF miniqmt.toml` 保证重新复制时不会删掉本机的配置。robocopy 的退出码 0–7 都表示成功（例如 1 是复制了文件，2 是删掉了多余的文件），8 以上才是失败
- `tests` 不需要复制

复制后检查：

```powershell
cd D:\duohelm-run
.\.venv\Scripts\duohelm.exe plugins
```

两个插件的状态都应是 `ready`：

```
eltdx                            0.1.1      datasource  ready
xtdata                           0.2.0      datasource  ready
```

### 插件的依赖装在哪里

插件在子进程里运行，用的是**平台自己的 Python**（`D:\duohelm-run\.venv`），所以插件的依赖要装进这个环境（`docs/subsystems/plugins.md`：“插件和平台共用同一个 Python 环境”）。平台本身不依赖 eltdx、xtquant，它们不进平台的 `pyproject.toml`：

| 插件 | 依赖 | 装法 |
|---|---|---|
| eltdx | `eltdx==3.3.5`（版本固定）、`tzdata` | 第 2 节的 `uv pip install` |
| xtdata | `tzdata`；连接 MiniQMT 时 xtquant 还需要 `pandas`、`numpy` | 同上 |
| xtdata | `xtquant` | **不用 pip 安装**，也不用 PyPI 上的同名包。用 MiniQMT 自带的或迅投官方发布的版本，在 `miniqmt.toml` 里写它所在的目录，插件运行时自己加进 `sys.path` |

xtquant 是编译好的扩展，要有和平台 Python 版本对应的文件。平台用 Python 3.12，xtquant 目录里要有 `*.cp312-win_amd64.pyd`。

### miniqmt.toml

放在**复制后的插件目录**里：`%USERPROFILE%\.duohelm\plugins\xtdata\miniqmt.toml`。

```powershell
$x = "$env:USERPROFILE\.duohelm\plugins\xtdata"
Copy-Item "$x\miniqmt.example.toml" "$x\miniqmt.toml"
notepad "$x\miniqmt.toml"
```

在平台里运行时只需要改 `xtquant_path`：

```toml
[miniqmt]
xtquant_path = "D:/AiCode/xtquant_250807"
download_timeout_s = 60
```

- `xtquant_path` 是 xtquant 所在目录的**上一级**（目录里有 `xtquant\` 子目录）。路径里用 `/`，或者用 TOML 的单引号字符串 `'D:\AiCode\xtquant_250807'`
- 在平台里，`host`、`port` 不用，MiniQMT 的地址在插件页确认（下一节），**不要**写进这个文件。`host`、`port` 只给录制脚本、独立调试用
- 文件要存成**不带 BOM 的 UTF-8**。Windows 11 的记事本默认就是；不要用 Windows PowerShell 5.1 的 `Out-File -Encoding utf8` 或 `Set-Content -Encoding utf8` 生成它，它们会加 BOM，插件读不了（已实测：报“miniqmt.toml 格式不对：Invalid statement (at line 1, column 1)”）

## 4. 第一次启动、确认地址、全量同步

先在 PowerShell 里直接启动一次，确认能用（第 5 节再改成开机自动运行）：

```powershell
cd D:\duohelm-run
.\.venv\Scripts\duohelm.exe serve
```

输出里有 `Web 页面: http://127.0.0.1:8765/` 就是页面也已经由 serve 提供；如果提示“没有找到构建好的 Web 页面”，回到第 2 节运行 `npm run build`。

不要用 `uv run duohelm serve` 长期运行：`uv run` 每次启动前会先检查、同步环境。直接运行 `.venv\Scripts\duohelm.exe`。

用浏览器打开 <http://127.0.0.1:8765>：

1. **插件页**（菜单“插件”）
   - xtdata：展开这一行，连接地址显示“未确认，不能连接”。点“修改地址”，填 MiniQMT 那台机器的局域网 IP 和端口（默认端口 58610），确认。MiniQMT 就在本机时，点“确认默认地址”（`127.0.0.1:58610`）。确认之前插件拿不到地址，不会去连 MiniQMT
   - eltdx：展开这一行，联网显示“待确认，不能联网”，核对清单里的 44 个地址后点“确认放行”
2. **数据同步页**（菜单“数据同步”）的“手动同步”：行情源选 xtdata，开始日期保持 1990-12-19，结束日期保持默认（最近一个已收盘的日子），点“开始同步”。页面显示进度和日志，结束后显示快照 id 和各数据集写入的行数
   - 同步开始前插件会先检查连接。连接检查没通过时任务直接失败，日志里有原因，例如“连不上 MiniQMT……请确认 MiniQMT 已打开并登录”“找不到 xtquant……”“miniqmt.toml 格式不对”
3. **用 eltdx 补一次退市股**（可以等 xtdata 同步完再做）：xtdata 缺少早年退市的证券（如 000003.SZ PT金田A），平台用 eltdx 扫描代码区间补全（见 [data.md](subsystems/data.md) 的“退市股扫描记录”和“多个数据源的退市日”）。行情源选 eltdx 再同步一次
   - **第一次很慢**：要逐个查询 11415 个代码，本机实测 22 分钟查了 2000 个，全部查完约 2 小时（与网络有关）。期间 serve 不能停。之后每次同步 eltdx，距上次查询超过 7 天的“没找到”的代码都会重查，同样很慢（#38）
   - 所以定时同步用 xtdata，eltdx 只在需要时手动运行
4. **打开定时同步**：数据同步页下方的“定时同步”，打开开关，行情源选 xtdata，时间保持 16:00（北京时间，必须晚于 15:30），保存。页面显示下次触发时间
   - 没有做过全量同步时（还没有日线或交易日历），页面显示需要先全量同步，定时同步不会触发
   - 定时同步提交的是增量同步：从上次同步的最后一个交易日往前 7 天开始，停机几天也不会漏掉日线；但停机那几天的 ST 和指数成分快照补不回来（#52）

在任务中心（菜单“任务中心”）能看到所有同步任务、状态和日志。

## 5. 长期运行

### 条件

定时同步**跑在 `duohelm serve` 进程里**：serve 每 30 秒检查一次是否到了触发时间，把同步任务放进队列，再由 serve 里的 worker 执行（见 [tasks.md](subsystems/tasks.md) 的“定时触发”）。所以：

- **16:00 时 serve 要在运行**，电脑不能在睡眠或关机。如果错过了，serve 在**同一天**晚些时候启动或电脑唤醒后会补跑一次；更早的日子不补跑，那几天的 ST 和指数成分快照就永远缺了。设置 → 系统 → 电源 → “接通电源时，使设备进入睡眠状态”改为“从不”，或至少保证交易日 16:00 前后电脑醒着
- **MiniQMT 那台机器要开着、MiniQMT 要打开并登录**。连不上时这一天的同步任务失败（任务中心可见，失败原因在日志里），可以手动在数据同步页重新同步当天，但 ST 和成分快照只能按同步当时的状态保存
- 同步需要的时间里 serve 都要在运行。关闭 serve 会中断正在运行的同步，下次启动时任务会被恢复重试
- **不要从 Claude 桌面应用的终端（或 agent）启动长期运行的 serve**：桌面应用是 MSIX 包，它启动的进程写 `%APPDATA%` 会被重定向；而且进程是应用的子进程，应用退出或更新时 serve 跟着退出

### 用任务计划程序在登录时自动启动

1. 新建启动脚本 `D:\duohelm-run-start.cmd`（放在运行检出外面，升级时不受 `git checkout` 影响）：

   ```bat
   @echo off
   rem DuoHelm 长期运行。输出追加到数据目录下的 serve.log
   set DUOHELM_HOME=%USERPROFILE%\.duohelm
   cd /d D:\duohelm-run
   ".venv\Scripts\duohelm.exe" serve >> "%DUOHELM_HOME%\serve.log" 2>&1
   ```

   输出重定向到文件时，平台会把输出改成 UTF-8，日志里的中文不会乱码，也不会因为系统代码页不支持中文而启动失败。用记事本查看 `serve.log` 即可

2. 在 PowerShell 里注册任务（只对当前用户，登录时运行）：

   ```powershell
   $me = "$env:USERDOMAIN\$env:USERNAME"
   $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument '/c start "DuoHelm" /min "D:\duohelm-run-start.cmd"'
   $trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
   $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
   $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
   Register-ScheduledTask -TaskName "DuoHelm" -Action $action -Trigger $trigger -Settings $settings -Principal $principal
   ```

   - `start ... /min`：serve 在一个最小化的命令行窗口里运行，任务栏上能看到它，窗口标题是“DuoHelm”
   - `-ExecutionTimeLimit 0`：任务计划程序默认 3 天后结束任务，这里改成不限时
   - 提示“拒绝访问”时，用“以管理员身份运行”的 PowerShell 再注册一次

3. 不用等下次登录，立即启动一次：`Start-ScheduledTask -TaskName "DuoHelm"`，然后打开 <http://127.0.0.1:8765> 确认

### 日常操作

| 要做的事 | 怎么做 |
|---|---|
| 看是否在运行 | 任务栏上有“DuoHelm”窗口；或打开 <http://127.0.0.1:8765/api/health> |
| 看日志 | `%USERPROFILE%\.duohelm\serve.log`；同步任务的日志在任务中心 |
| 停止 | 在“DuoHelm”窗口里按 Ctrl+C（会等当前任务最多 30 秒再退出）；或直接关闭窗口。由于脚本是用 `start` 启动的，在任务计划程序里“结束”这个任务停不了 serve |
| 重新启动 | 停止后 `Start-ScheduledTask -TaskName "DuoHelm"` |
| 不再自动启动 | `Unregister-ScheduledTask -TaskName "DuoHelm"` |

## 验证情况

这份手册的步骤于 2026-11-28 在一台 Windows 11 机器上实测，用临时的 `DUOHELM_HOME`，没有使用真实的 `%USERPROFILE%\.duohelm`：

| 步骤 | 结果 |
|---|---|
| `git worktree add --detach`、`uv sync --no-dev`、`uv pip install` 插件依赖 | 通过。另外确认了 `uv sync` 会删掉插件的依赖 |
| robocopy 复制插件、重新复制时保留 `miniqmt.toml`、`duohelm plugins` 显示两个插件 `ready` | 通过 |
| `npm ci`、`npm run build`，serve 提供页面，直接打开 `/plugins`、`/sync` | 通过（需要 serve 托管页面的改动，见 PR #94） |
| 插件页放行 eltdx 联网、确认 xtdata 地址 | 通过 |
| xtdata 同步 | **只验证到连接这一步**：本机没有 MiniQMT，也拿不到真实的 MiniQMT 地址。确认了插件按 `miniqmt.toml` 加载了 xtquant 和 pandas、numpy，并真的去连接（报错来自 xtquant：“无法连接xtquant服务”）。连上之后的全量同步、耗时没有实测 |
| eltdx 同步 | 证券列表、退市股扫描在正常进行（22 分钟查了 2000 个代码）；没有等它跑完，入库那一步没有用真实数据实测 |
| 带 BOM 的 `miniqmt.toml` | 插件读不了（已实测），所以上文要求不带 BOM |
| `.cmd` 启动脚本，输出重定向到日志 | 通过：页面和 API 可访问，日志里中文正常（需要 PR #94 的输出编码修复；之前在英文 Windows 上 serve 一启动就崩溃） |
| 任务计划程序注册 | **没有实际注册**（会改动这台机器的系统配置）。只确认了上面的 `New-ScheduledTask*` 命令能正常构造出任务定义 |
| 16:00 定时触发、睡眠后补跑 | 没有实测，行为依据 [tasks.md](subsystems/tasks.md) 和对应的测试 |
