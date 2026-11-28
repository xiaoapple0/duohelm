# 舵手 DuoHelm

> 人与 AI 共同掌舵的因子研究与交易平台。

DuoHelm 是一个面向个人交易者的因子研究平台：接入行情、管理股票池、研究因子、回测，最终接入模拟盘与实盘。平台本身只负责**管理、调度和适配**，行情源、算子、因子、交易接口都以插件形式装配，由使用者自己维护。

> ⚠️ 项目处于早期阶段（M0：骨架）。功能尚不可用。

## 设计要点

- **插件化**：四类插件——行情源、算子、因子、交易接口——加进去就能用。见 [docs/subsystems/plugins.md](docs/subsystems/plugins.md)。
- **正确性第一**：未来函数、幸存者偏差、成交假设是量化平台最隐蔽的 bug。平台对每个插件做契约检查，不合格的插件进不了研究和回测。
- **Web 管理界面**：行情、股票池、因子、研究、回测、交易、插件、任务，全部可在页面上操作。
- **先 A 股、先日频**：市场差异和数据频率都已抽象，将来扩展美股（IBKR）和分钟级数据不需要推倒重来。
- **人与 agent 共同维护**：代码由 agent 编写、审查和维护，人定方向、握实盘开关。规则见 [AGENTS.md](AGENTS.md)。

## 仓库结构

```
packages/
  duohelm-sdk/     插件 SDK：插件接口、清单格式、契约检查工具（插件作者只依赖它）
  duohelm/         平台核心与 API
web/               Web 前端（React + TypeScript + Ant Design）
docs/              设计文档、代码规范、贡献规则、定时任务说明
```

## 开发

需要 [uv](https://docs.astral.sh/uv/) 和 Node.js 22+。

```bash
# 后端
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest

# 启动 API（默认只监听 127.0.0.1）
uv run duohelm serve

# 前端（开发：Vite 开发服务器，http://127.0.0.1:5173）
cd web && npm install && npm run dev

# 前端（构建后由 duohelm serve 直接提供，http://127.0.0.1:8765）
cd web && npm ci && npm run build
```

## 文档

| 文档 | 内容 |
|---|---|
| [PRINCIPLES.md](PRINCIPLES.md) | 项目定位、核心原则、明确不做的事 |
| [ROADMAP.md](ROADMAP.md) | 里程碑 |
| [AGENTS.md](AGENTS.md) | 给 coding agent 的入口说明 |
| [LESSONS.md](LESSONS.md) | 从已合并工作中总结的教训 |
| [docs/running.md](docs/running.md) | 在自己的 Windows 机器上长期运行：安装插件、同步、定时同步、开机启动 |
| [docs/code-standards.md](docs/code-standards.md) | 验收标准、合并与发版条件 |
| [docs/subsystems/](docs/subsystems/README.md) | 各子系统设计 |

## 免责声明

本项目是研究工具，不构成任何投资建议。回测结果不代表未来收益。使用实盘交易功能的一切后果由使用者自行承担。
