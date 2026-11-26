"""插件清单 ``duohelm-plugin.toml`` 的格式与校验。

这是对插件作者的契约。新增可选字段是兼容改动；删除字段、改变含义、
新增必填字段是不兼容改动，需要维护者确认并升 SDK 版本。
"""

from __future__ import annotations

import ipaddress
import re
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from duohelm_sdk.endpoints import AddressError, EndpointScope, parse_address

MANIFEST_FILENAME = "duohelm-plugin.toml"

_PLUGIN_ID = r"^[a-z][a-z0-9-]{0,63}$"
_FACTOR_ID = r"^[a-z][a-z0-9_]{0,63}$"
_ENTRYPOINT = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d{1,5})?$")
_LOCAL_NAMES = ("localhost",)
_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home.arpa")
"""明显指向本机或局域网的主机名。network 只允许公网地址，这些名字直接拒绝；其他主机名在
运行时由平台按实际解析出的 IP 检查（解析到本机或私有网段时拦截）。"""


def _network_host_problem(entry: str) -> str | None:
    """network 里的一项不是公网地址时，返回原因。"""
    host = entry.rsplit(":", 1)[0] if ":" in entry else entry
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES):
            return f"{entry!r} 指向本机或局域网"
        labels = host.split(".")
        if labels[-1].isdigit() or any(label.startswith("0x") for label in labels):
            # 127.1、2130706433、0x7f.0.0.1 这类写法会被系统解析成 IP，绕过上面的检查
            return f"{entry!r} 是不规范的 IP 写法（只接受标准的点分十进制 IP 或主机名）"
        return None
    if not ip.is_global or ip.is_multicast:
        return f"{entry!r} 不是公网地址（本机、私有网段、链路本地、保留地址都不行）"
    return None


class PluginKind(StrEnum):
    DATASOURCE = "datasource"
    OPERATOR = "operator"
    FACTOR = "factor"
    BROKER = "broker"


class ManifestError(Exception):
    """清单无法解析或不合法。``source`` 指明出错的文件。"""

    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"{source}: {message}")
        self.source = source
        self.message = message


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_version(value: str) -> str:
    try:
        Version(value)
    except InvalidVersion as exc:
        raise ValueError(f"不是合法的版本号: {value!r}") from exc
    return value


def _validate_spec(value: str) -> str:
    try:
        SpecifierSet(value)
    except InvalidSpecifier as exc:
        raise ValueError(f"不是合法的版本范围: {value!r}") from exc
    return value


class PluginInfo(_Model):
    id: Annotated[str, Field(pattern=_PLUGIN_ID)]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    version: str
    kind: PluginKind
    sdk: str
    author: str = ""
    description: str = ""
    entrypoint: str | None = None

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        return _validate_version(value)

    @field_validator("sdk")
    @classmethod
    def _check_sdk(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("必须声明兼容的 SDK 版本范围，例如 '>=0.4,<0.5'")
        return _validate_spec(value)

    @field_validator("entrypoint")
    @classmethod
    def _check_entrypoint(cls, value: str | None) -> str | None:
        if value is not None and not _ENTRYPOINT.fullmatch(value):
            raise ValueError(f"入口应为 '模块:对象' 格式: {value!r}")
        return value


class Endpoint(_Model):
    """用户可配置的连接地址（SDK 0.3 起），例如 MiniQMT 的地址。见 :mod:`duohelm_sdk.endpoints`。

    实际地址由用户在平台里配置并确认，插件用 :func:`duohelm_sdk.context.endpoint` 取得。
    """

    id: Annotated[str, Field(pattern=_FACTOR_ID)]
    description: Annotated[str, Field(min_length=1, max_length=200)]
    default: str
    """默认地址 ``主机:端口``，必须符合 scope。"""
    scope: EndpointScope
    """用户能填的地址范围：loopback / lan / internet。"""

    @model_validator(mode="after")
    def _check_default(self) -> Self:
        try:
            parse_address(self.default, self.scope)
        except AddressError as exc:
            raise ValueError(f"endpoint {self.id!r} 的 default 不符合 scope：{exc}") from None
        return self


class Permissions(_Model):
    network: tuple[str, ...] = ()
    """插件需要访问的公网主机名或公网 IP（可带端口）。不允许协议、路径和通配符。

    SDK 0.4 起只允许公网地址：本机、私有网段等地址在解析清单时拒绝，需要连接本机或
    局域网服务的插件改用 ``endpoints``（由用户填写并确认地址）。整个列表由用户在平台里确认
    一次后才放行，列表变了需要重新确认。"""
    endpoints: tuple[Endpoint, ...] = ()
    """用户可配置的连接地址（SDK 0.3 起）。这一节变了（不含清单的其他部分），用户需要重新确认。"""

    @field_validator("endpoints")
    @classmethod
    def _check_endpoint_ids(cls, value: tuple[Endpoint, ...]) -> tuple[Endpoint, ...]:
        ids = [e.id for e in value]
        if len(set(ids)) != len(ids):
            raise ValueError(f"endpoint id 重复: {_duplicates(ids)}")
        return value

    @field_validator("network")
    @classmethod
    def _check_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for host in value:
            if not _HOST.fullmatch(host):
                raise ValueError(f"主机名格式不对（只写主机名，不带协议和路径）: {host!r}")
            problem = _network_host_problem(host)
            if problem is not None:
                raise ValueError(
                    f"network 只允许公网地址：{problem}。本机或局域网的服务请改用 "
                    "[[permissions.endpoints]]"
                )
        if len(set(value)) != len(value):
            raise ValueError("network 中有重复的主机名")
        return value


class Requirement(_Model):
    id: Annotated[str, Field(pattern=_PLUGIN_ID)]
    version: str = ""
    """版本范围，例如 '>=1.0'。空字符串表示任意版本。"""

    @field_validator("version")
    @classmethod
    def _check_spec(cls, value: str) -> str:
        return _validate_spec(value)

    @property
    def spec(self) -> SpecifierSet:
        return SpecifierSet(self.version)


class FactorSpec(_Model):
    id: Annotated[str, Field(pattern=_FACTOR_ID)]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    category: str = ""
    expression: str | None = None
    fields: tuple[str, ...] = ()
    lookback: Annotated[int, Field(ge=0)] = 0
    """需要的历史窗口（K 线根数）。"""
    description: str = ""


class Manifest(_Model):
    plugin: PluginInfo
    permissions: Permissions = Permissions()
    requires: tuple[Requirement, ...] = ()
    factors: tuple[FactorSpec, ...] = ()

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        info = self.plugin
        if info.kind is PluginKind.FACTOR:
            if not self.factors:
                raise ValueError("因子插件至少要定义一个 [[factors]]")
            if info.entrypoint is None:
                missing = [f.id for f in self.factors if not f.expression]
                if missing:
                    raise ValueError(
                        f"没有 entrypoint 的因子插件，每个因子都必须有 expression，缺少: {missing}"
                    )
        else:
            if self.factors:
                raise ValueError(f"只有因子插件可以定义 [[factors]]，当前类型是 {info.kind}")
            if info.entrypoint is None:
                raise ValueError(f"{info.kind} 插件必须声明 entrypoint")

        factor_ids = [f.id for f in self.factors]
        if len(set(factor_ids)) != len(factor_ids):
            raise ValueError(f"因子 id 重复: {_duplicates(factor_ids)}")

        req_ids = [r.id for r in self.requires]
        if info.id in req_ids:
            raise ValueError("插件不能依赖自己")
        if len(set(req_ids)) != len(req_ids):
            raise ValueError(f"依赖重复: {_duplicates(req_ids)}")
        return self

    @property
    def id(self) -> str:
        return self.plugin.id

    @property
    def version(self) -> Version:
        return Version(self.plugin.version)

    def supports_sdk(self, sdk_version: str) -> bool:
        return SpecifierSet(self.plugin.sdk).contains(Version(sdk_version), prereleases=True)


def _duplicates(items: list[str]) -> list[str]:
    seen: set[str] = set()
    dup: list[str] = []
    for item in items:
        if item in seen and item not in dup:
            dup.append(item)
        seen.add(item)
    return dup


def parse_manifest(text: str, source: str = "<string>") -> Manifest:
    """解析清单文本。出错时抛出 :class:`ManifestError`，消息可直接展示给用户。"""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(source, f"TOML 语法错误: {exc}") from exc
    try:
        return Manifest.model_validate(data)
    except ValidationError as exc:
        lines: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"]) or "(清单)"
            lines.append(f"{loc}: {err['msg']}")
        raise ManifestError(source, "; ".join(lines)) from exc


def load_manifest(path: Path) -> Manifest:
    """从插件目录或清单文件加载清单。"""
    file = path / MANIFEST_FILENAME if path.is_dir() else path
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(str(file), f"无法读取: {exc}") from exc
    return parse_manifest(text, source=str(file))
