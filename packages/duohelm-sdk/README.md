# duohelm-sdk

DuoHelm 的插件 SDK。插件作者只依赖这个包。

- `duohelm_sdk.manifest`：插件清单（`duohelm-plugin.toml`）的格式与校验
- `duohelm_sdk.protocols`：四类插件的接口
- `duohelm_sdk.schema`：数据标准格式
- `duohelm_sdk.checks`：契约检查（未来函数扰动、确定性、输出格式），插件作者可以在自己的 CI 里运行

设计说明见仓库的 `docs/subsystems/plugins.md`。
