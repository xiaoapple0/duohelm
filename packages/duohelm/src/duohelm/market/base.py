"""市场规则的通用类型。设计见 docs/subsystems/market.md。

所有判断都是三态：未知不是“可以”。调用方遇到 ``UNKNOWN`` 一律按“不能成交”处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class LimitStatus(StrEnum):
    LIMITED = "limited"  # 有涨跌停价
    UNLIMITED = "unlimited"  # 当天不设涨跌幅
    UNKNOWN = "unknown"  # 规则拿不准或输入不够判断


class Verdict(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class PriceLimit:
    """某只股票某个交易日的涨跌停价。``upper`` 和 ``lower`` 只在 ``LIMITED`` 时有值。"""

    status: LimitStatus
    upper: Decimal | None
    lower: Decimal | None
    reason: str
