"""四类插件的接口。

这是对插件作者的契约，改动需要维护者确认。

所有表格数据都是 polars DataFrame，格式见 :mod:`duohelm_sdk.schema`。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import polars as pl


class Freq(StrEnum):
    DAY = "1d"
    MINUTE = "1m"


# ---------------------------------------------------------------- 行情源


@dataclass(frozen=True)
class Capabilities:
    markets: tuple[str, ...]
    """支持的市场，例如 ("CN",)。"""
    freqs: tuple[Freq, ...]
    fields: tuple[str, ...]
    """bars() 能提供的字段，除 date、symbol 外。"""
    lists_delisted: bool = False
    """能否直接列出已退市的证券。

    为 True 时，平台调用 ``securities(include_delisted=True)`` 直接取得完整名单；
    为 False 时（数据源的代码表只有在市证券），平台用 ``lookup_securities`` 扫描代码区间。
    """
    exact_delist_date: bool = True
    """``delist_date`` 是否符合 SDK 口径（摘牌日之前的最后一个交易所交易日，SDK 0.4.1 起）。

    数据源拿不到摘牌日、只能给出近似值（例如最后一根有成交的 K 线的日期）时设为 False。
    平台把这个数据源给出的退市日记为近似值（数据集 ``delist_date_quality``）。
    """


@dataclass(frozen=True)
class ConnectionStatus:
    """:meth:`SupportsConnectionCheck.check_connection` 的结果（SDK 0.3 起）。"""

    ok: bool
    message: str = ""
    """失败时给用户看的说明，例如“连不上 MiniQMT，请打开并登录”。不要写入密码等凭据。"""


@runtime_checkable
class SupportsConnectionCheck(Protocol):
    """可选：插件能快速检查数据源是否连得上（SDK 0.3 起）。

    平台在同步任务开始时，如果插件实现了它，先调用一次；``ok`` 为 False 时任务直接失败，
    把 ``message`` 展示给用户，不进入下载。实现要快（几秒内返回），连接失败返回
    ``ConnectionStatus(ok=False, ...)``，不要抛异常。
    """

    def check_connection(self) -> ConnectionStatus: ...


@runtime_checkable
class DataSourcePlugin(Protocol):
    """行情源插件。返回的数据必须符合 schema 中的标准格式，平台入库前会校验。"""

    def capabilities(self) -> Capabilities: ...

    def securities(self, *, include_delisted: bool = False) -> pl.DataFrame:
        """证券列表，格式见 schema.SECURITIES_SCHEMA。

        默认只返回当前在市的证券。``include_delisted=True`` 时同时返回已退市的证券，
        只有 ``capabilities().lists_delisted`` 为 True 的插件才需要支持，其他插件可以
        抛出 NotImplementedError（平台不会这样调用它们）。
        """
        ...

    def lookup_securities(self, symbols: Sequence[str]) -> pl.DataFrame:
        """查询给定代码，可以包含已退市或根本不存在的代码。格式见 schema.SECURITIES_SCHEMA。

        只返回数据源有记录的代码；已退市的证券 delist_date 不为空。不能直接列出退市
        证券的插件（``lists_delisted`` 为 False），平台用它扫描代码区间发现退市股；扫描
        结果由平台保存，没找到的代码不会重复查询，找到过的退市证券每次同步都重新查询。
        """
        ...

    def trading_calendar(self, market: str, start: date, end: date) -> pl.DataFrame:
        """列：date。"""
        ...

    def adj_factors(self, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame:
        """复权因子，格式和含义见 schema.ADJ_FACTORS_SCHEMA。

        date 为除权除息日；factor 是自该日起生效的累计后复权比例，只能由该日及以前的
        数据决定。可以用 :func:`duohelm_sdk.adjust.factors_from_events` 从除权除息事件计算。
        """
        ...

    def bars(self, symbols: Sequence[str], freq: Freq, start: date, end: date) -> pl.DataFrame:
        """未复权 K 线，格式见 schema.BARS_SCHEMA。

        停牌日不返回这一行；已退市的证券也要能返回历史。
        """
        ...

    def index_constituents(self, index: str, start: date, end: date) -> pl.DataFrame:
        """列：symbol、in_date、out_date（仍在指数中则为 null）。"""
        ...


# ---------------------------------------------------------------- 算子

Operator = Callable[..., "pl.Expr"]
"""算子：接收 polars 表达式和参数，返回 polars 表达式。"""


@runtime_checkable
class OperatorPlugin(Protocol):
    def operators(self) -> Mapping[str, Operator]:
        """算子名称到实现的映射。名称在全平台唯一，与内置算子重名会被拒绝加载。"""
        ...


# ---------------------------------------------------------------- 因子


@runtime_checkable
class FactorPlugin(Protocol):
    def compute(self, factor_id: str, data: pl.DataFrame) -> pl.DataFrame:
        """计算一个因子。

        data：长表，按 (symbol, date) 排序，包含清单中该因子声明的 fields。
        返回：列为 date、symbol、value，见 schema.FACTOR_OUTPUT_SCHEMA。
        不得修改 data，不得访问网络或文件。
        """
        ...


# ---------------------------------------------------------------- 交易接口


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Order:
    client_order_id: str
    """平台生成的唯一 id，用于幂等：同一 id 重复提交不得产生第二笔委托。"""
    symbol: str
    side: OrderSide
    quantity: int
    limit_price: float | None = None
    """None 表示市价单（是否支持由券商决定）。"""


@dataclass(frozen=True)
class OrderAck:
    client_order_id: str
    broker_order_id: str | None
    status: OrderStatus
    filled_quantity: int = 0
    message: str = ""


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    available: int
    """可卖数量（A 股 T+1 下通常小于 quantity）。"""


@dataclass(frozen=True)
class AccountInfo:
    cash: float
    equity: float
    currency: str


@runtime_checkable
class BrokerPlugin(Protocol):
    """交易接口插件。

    平台在调用 submit 之前一定会先做风控检查。插件不得自行绕过平台向券商下单。
    """

    def account(self) -> AccountInfo: ...

    def positions(self) -> Sequence[Position]: ...

    def submit(self, order: Order) -> OrderAck: ...

    def cancel(self, client_order_id: str) -> OrderAck: ...

    def orders(self) -> Sequence[OrderAck]: ...
