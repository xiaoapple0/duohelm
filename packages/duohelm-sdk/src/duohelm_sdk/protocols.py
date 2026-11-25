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


@runtime_checkable
class DataSourcePlugin(Protocol):
    """行情源插件。返回的数据必须符合 schema 中的标准格式，平台入库前会校验。"""

    def capabilities(self) -> Capabilities: ...

    def securities(self) -> pl.DataFrame:
        """当前在市的证券，格式见 schema.SECURITIES_SCHEMA。"""
        ...

    def lookup_securities(self, symbols: Sequence[str]) -> pl.DataFrame:
        """查询给定代码，可以包含已退市或根本不存在的代码。格式见 schema.SECURITIES_SCHEMA。

        只返回数据源有记录的代码；已退市的证券 delist_date 不为空。平台用它扫描代码区间、
        发现退市股（数据源的代码表通常只有在市证券），扫描结果由平台保存，不会重复扫描。
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
