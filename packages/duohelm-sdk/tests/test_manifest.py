from __future__ import annotations

from pathlib import Path

import pytest

from duohelm_sdk import MANIFEST_FILENAME, ManifestError, PluginKind, load_manifest, parse_manifest

FACTOR_PLUGIN = """
[plugin]
id = "my-factors"
name = "自有因子"
version = "1.2.0"
kind = "factor"
sdk = ">=0.1,<0.2"

[permissions]
network = []

[[requires]]
id = "ops-industry"
version = ">=1.0"

[[factors]]
id = "vol_price_corr_10"
name = "10日量价相关性"
expression = "-1 * Corr(Rank($close), Rank($volume), 10)"
fields = ["close", "volume"]
lookback = 10
"""

DATASOURCE_PLUGIN = """
[plugin]
id = "demo-source"
name = "示例行情源"
version = "0.1.0"
kind = "datasource"
sdk = ">=0.1"
entrypoint = "demo_source:plugin"

[permissions]
network = ["api.example.com", "data.example.com:8443"]
"""


def _error(text: str) -> str:
    with pytest.raises(ManifestError) as info:
        parse_manifest(text)
    return info.value.message


def test_parses_factor_plugin() -> None:
    m = parse_manifest(FACTOR_PLUGIN)
    assert m.id == "my-factors"
    assert m.plugin.kind is PluginKind.FACTOR
    assert m.factors[0].fields == ("close", "volume")
    assert m.factors[0].lookback == 10
    assert m.requires[0].id == "ops-industry"
    assert str(m.version) == "1.2.0"


def test_parses_datasource_plugin_with_network() -> None:
    m = parse_manifest(DATASOURCE_PLUGIN)
    assert m.permissions.network == ("api.example.com", "data.example.com:8443")


def test_sdk_compatibility() -> None:
    m = parse_manifest(FACTOR_PLUGIN)
    assert m.supports_sdk("0.1.0")
    assert m.supports_sdk("0.1.5")
    assert not m.supports_sdk("0.2.0")


@pytest.mark.parametrize("bad_id", ["My-Plugin", "1plugin", "my_plugin", "", "a" * 65])
def test_rejects_bad_plugin_id(bad_id: str) -> None:
    assert "plugin.id" in _error(FACTOR_PLUGIN.replace('"my-factors"', f'"{bad_id}"'))


def test_rejects_bad_version() -> None:
    assert "版本号" in _error(FACTOR_PLUGIN.replace('"1.2.0"', '"one"'))


def test_rejects_empty_or_bad_sdk_spec() -> None:
    assert "SDK" in _error(FACTOR_PLUGIN.replace('">=0.1,<0.2"', '""'))
    assert "版本范围" in _error(FACTOR_PLUGIN.replace('">=0.1,<0.2"', '"latest"'))


def test_rejects_unknown_keys() -> None:
    assert "extra" in _error(FACTOR_PLUGIN + '\n[extra]\ncolour = "red"\n')
    typo = FACTOR_PLUGIN.replace('kind = "factor"', 'kind = "factor"\nauthr = "me"')
    assert "plugin.authr" in _error(typo)


def test_factor_plugin_needs_factors() -> None:
    text = FACTOR_PLUGIN.split("[[factors]]", maxsplit=1)[0]
    assert "至少要定义一个" in _error(text)


def test_expression_required_without_entrypoint() -> None:
    text = FACTOR_PLUGIN.replace('expression = "-1 * Corr(Rank($close), Rank($volume), 10)"\n', "")
    assert "expression" in _error(text)


def test_code_factor_plugin_may_omit_expression() -> None:
    text = FACTOR_PLUGIN.replace(
        'expression = "-1 * Corr(Rank($close), Rank($volume), 10)"\n', ""
    ).replace('sdk = ">=0.1,<0.2"', 'sdk = ">=0.1,<0.2"\nentrypoint = "my_factors:plugin"')
    assert parse_manifest(text).factors[0].expression is None


def test_non_factor_plugin_needs_entrypoint() -> None:
    text = DATASOURCE_PLUGIN.replace('entrypoint = "demo_source:plugin"\n', "")
    assert "entrypoint" in _error(text)


def test_non_factor_plugin_cannot_define_factors() -> None:
    text = DATASOURCE_PLUGIN + '\n[[factors]]\nid = "x"\nname = "x"\nexpression = "$close"\n'
    assert "只有因子插件" in _error(text)


def test_rejects_duplicate_factor_ids() -> None:
    extra = '\n[[factors]]\nid = "vol_price_corr_10"\nname = "again"\nexpression = "$close"\n'
    assert "因子 id 重复" in _error(FACTOR_PLUGIN + extra)


def test_rejects_self_dependency() -> None:
    assert "依赖自己" in _error(FACTOR_PLUGIN.replace('id = "ops-industry"', 'id = "my-factors"'))


@pytest.mark.parametrize(
    "host", ["https://api.example.com", "api.example.com/path", "*.example.com", "API.example.com"]
)
def test_rejects_bad_network_hosts(host: str) -> None:
    assert "主机名" in _error(DATASOURCE_PLUGIN.replace('"api.example.com"', f'"{host}"'))


@pytest.mark.parametrize("entrypoint", ["demo_source", "demo-source:plugin", ":plugin"])
def test_rejects_bad_entrypoint(entrypoint: str) -> None:
    text = DATASOURCE_PLUGIN.replace('"demo_source:plugin"', f'"{entrypoint}"')
    assert "入口" in _error(text)


def test_reports_toml_syntax_errors() -> None:
    assert "TOML" in _error("[plugin\n")


def test_load_manifest_from_directory(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_FILENAME).write_text(FACTOR_PLUGIN, encoding="utf-8")
    assert load_manifest(tmp_path).id == "my-factors"


def test_load_manifest_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestError) as info:
        load_manifest(tmp_path)
    assert "无法读取" in info.value.message


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.1:58610",
        "10.0.0.2",
        "172.16.5.4:80",
        "192.168.31.5:58610",
        "169.254.1.1",
        "100.64.0.1",
        "0.0.0.0:80",
        "224.0.0.1",
        "203.0.113.7",  # 文档保留地址
        "localhost",
        "localhost:8080",
        "nas.local",
        "router.lan",
        "db.internal",
        "app.localhost",
        "printer.home.arpa",
        "127.1",  # 不规范的 IP 写法，系统会解析成 127.0.0.1
        "2130706433",
        "0x7f.0.0.1",
        "0x7f000001:80",
        "10.1",
    ],
)
def test_network_rejects_local_and_private(host: str) -> None:
    """SDK 0.3.1：network 只允许公网地址，本机和局域网的服务走 endpoints。"""
    message = _error(
        DATASOURCE_PLUGIN.replace(
            'network = ["api.example.com", "data.example.com:8443"]', f'network = ["{host}"]'
        )
    )
    assert "公网" in message and "endpoints" in message
    assert host.split(":", maxsplit=1)[0] in message


@pytest.mark.parametrize(
    "host", ["8.8.8.8", "1.1.1.1:443", "api.example.com", "tdx.example.net:7709"]
)
def test_network_accepts_public(host: str) -> None:
    text = DATASOURCE_PLUGIN.replace(
        'network = ["api.example.com", "data.example.com:8443"]', f'network = ["{host}"]'
    )
    assert parse_manifest(text).permissions.network == (host,)
