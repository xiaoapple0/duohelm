"""插件运行时从平台取得的信息（SDK 0.3 起）。

目前只有用户配置的连接地址：插件调用 :func:`endpoint` 取得清单里
``[[permissions.endpoints]]`` 声明的服务的地址。

- 在平台里运行时：平台在导入插件代码之前设置好所有声明的 endpoint，值是用户确认过的地址，
  用户没有配置的用清单里的 ``default``。地址在传入之前已由平台按 scope 校验
- 不在平台里运行时（测试、独立脚本）：返回调用方给的 ``default``；没有给就报错
"""

from __future__ import annotations

from collections.abc import Mapping

from duohelm_sdk.endpoints import Address, EndpointScope, parse_address

_endpoints: dict[str, Address] | None = None


class EndpointNotConfiguredError(LookupError):
    """平台没有传入这个 endpoint（清单里没有声明），或不在平台里运行且没有给 default。"""


def endpoint(endpoint_id: str, *, default: str | None = None) -> Address:
    """取得 endpoint 的地址。``default`` 只在不由平台运行时使用（格式同清单的 default）。"""
    if _endpoints is not None:
        try:
            return _endpoints[endpoint_id]
        except KeyError:
            raise EndpointNotConfiguredError(
                f"清单的 [[permissions.endpoints]] 里没有声明 {endpoint_id!r}"
            ) from None
    if default is None:
        raise EndpointNotConfiguredError(
            f"不在 DuoHelm 平台里运行，endpoint {endpoint_id!r} 需要给出 default"
        )
    return parse_address(default, EndpointScope.INTERNET)


def in_platform() -> bool:
    """是否由 DuoHelm 平台运行（平台已设置 endpoint）。"""
    return _endpoints is not None


def _install(endpoints: Mapping[str, Address]) -> None:  # pyright: ignore[reportUnusedFunction]
    """平台在子进程里、导入插件代码之前调用。插件不要调用它。"""
    global _endpoints  # noqa: PLW0603  进程级的运行时信息，只设置一次
    _endpoints = dict(endpoints)


def _reset() -> None:  # pyright: ignore[reportUnusedFunction]
    """测试用：恢复为“不在平台里运行”。"""
    global _endpoints  # noqa: PLW0603
    _endpoints = None
