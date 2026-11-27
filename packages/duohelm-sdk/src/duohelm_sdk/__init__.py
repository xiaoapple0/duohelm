"""DuoHelm 插件 SDK。

本模块的 ``__all__`` 是对插件作者的公开契约，改动需要维护者确认
（见 docs/subsystems/plugins.md 的"契约变更"）。

``schema`` 和 ``checks`` 依赖 polars，作为子模块按需导入::

    from duohelm_sdk.checks import check_no_lookahead
"""

from duohelm_sdk.context import EndpointNotConfiguredError, endpoint
from duohelm_sdk.endpoints import Address, AddressError, EndpointScope, parse_address
from duohelm_sdk.manifest import (
    MANIFEST_FILENAME,
    Endpoint,
    FactorSpec,
    Manifest,
    ManifestError,
    Permissions,
    PluginInfo,
    PluginKind,
    Requirement,
    load_manifest,
    parse_manifest,
)
from duohelm_sdk.protocols import (
    AccountInfo,
    BrokerPlugin,
    Capabilities,
    ConnectionStatus,
    DataSourcePlugin,
    FactorPlugin,
    Freq,
    LookupFailedError,
    Operator,
    OperatorPlugin,
    Order,
    OrderAck,
    OrderSide,
    OrderStatus,
    Position,
    SupportsConnectionCheck,
)
from duohelm_sdk.version import SDK_VERSION

__all__ = [
    "MANIFEST_FILENAME",
    "SDK_VERSION",
    "AccountInfo",
    "Address",
    "AddressError",
    "BrokerPlugin",
    "Capabilities",
    "ConnectionStatus",
    "DataSourcePlugin",
    "Endpoint",
    "EndpointNotConfiguredError",
    "EndpointScope",
    "FactorPlugin",
    "FactorSpec",
    "Freq",
    "LookupFailedError",
    "Manifest",
    "ManifestError",
    "Operator",
    "OperatorPlugin",
    "Order",
    "OrderAck",
    "OrderSide",
    "OrderStatus",
    "Permissions",
    "PluginInfo",
    "PluginKind",
    "Position",
    "Requirement",
    "SupportsConnectionCheck",
    "endpoint",
    "load_manifest",
    "parse_address",
    "parse_manifest",
]
