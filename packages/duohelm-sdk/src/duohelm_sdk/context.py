"""插件运行时从平台取得的信息（SDK 0.3 起）。

目前只有用户配置的连接地址：插件调用 :func:`endpoint` 取得清单里
``[[permissions.endpoints]]`` 声明的服务的地址。

- 在平台里运行时：平台在导入插件代码之前传入用户确认过的地址（已按 scope 校验）。
  用户还没确认的 endpoint（包括需要重新确认的），平台不传地址，:func:`endpoint` 抛
  :class:`EndpointNotConfiguredError`，插件据此提示用户去插件页确认（SDK 0.4 起；0.3 返回
  清单的 ``default``）。代码里给的 ``default`` 在平台里一律忽略
- 不在平台里运行时（测试、独立脚本）：返回调用方给的 ``default``；没有给就报错
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from duohelm_sdk.endpoints import Address, EndpointScope, parse_address

_endpoints: dict[str, Address] | None = None
_unconfirmed: frozenset[str] = frozenset()


class EndpointNotConfiguredError(LookupError):
    """取不到 endpoint 的地址：用户还没在插件页确认，清单里没有声明，或不在平台里运行且
    没有给 default。"""


def endpoint(endpoint_id: str, *, default: str | None = None) -> Address:
    """取得 endpoint 的地址。``default`` 只在不由平台运行时使用（格式同清单的 default）。"""
    if _endpoints is not None:
        if endpoint_id in _endpoints:
            return _endpoints[endpoint_id]
        if endpoint_id in _unconfirmed:
            raise EndpointNotConfiguredError(
                f"endpoint {endpoint_id!r} 的地址还没有确认，请在 DuoHelm 插件页确认后再试"
            )
        raise EndpointNotConfiguredError(
            f"清单的 [[permissions.endpoints]] 里没有声明 {endpoint_id!r}"
        )
    if default is None:
        raise EndpointNotConfiguredError(
            f"不在 DuoHelm 平台里运行，endpoint {endpoint_id!r} 需要给出 default"
        )
    return parse_address(default, EndpointScope.INTERNET)


def in_platform() -> bool:
    """是否由 DuoHelm 平台运行（平台已设置 endpoint）。"""
    return _endpoints is not None


def _install(  # pyright: ignore[reportUnusedFunction]
    endpoints: Mapping[str, Address], unconfirmed: Iterable[str] = ()
) -> None:
    """平台在子进程里、导入插件代码之前调用。插件不要调用它。

    ``endpoints`` 是用户确认过的地址；``unconfirmed`` 是清单声明了、但用户还没确认的 id。"""
    global _endpoints, _unconfirmed  # noqa: PLW0603  进程级的运行时信息，只设置一次
    _endpoints = dict(endpoints)
    _unconfirmed = frozenset(unconfirmed) - set(_endpoints)


def _reset() -> None:  # pyright: ignore[reportUnusedFunction]
    """测试用：恢复为“不在平台里运行”。"""
    global _endpoints, _unconfirmed  # noqa: PLW0603
    _endpoints = None
    _unconfirmed = frozenset()
