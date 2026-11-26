"""A 股交易规则：板块判断、涨跌停价、整手。设计见 docs/subsystems/market.md。

规则都在下面的表里，每一行写明生效区间（左闭右开）和来源。所有来源在写下时都**没有核对原文**。
拿不准、输入不够判断的一律返回未知，不猜。只提供规则，不涉及下单、仓位、风控。
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from duohelm.market.base import LimitStatus, PriceLimit, Side, Verdict

_TICK = Decimal("0.01")
_ONE = Decimal(1)

# 上市初期最长的不设涨跌幅天数；上市第几个交易日不小于这个数加 1 就一定不在初期
_MAX_INITIAL_DAYS = 5
# 5 个交易日跨不过 30 个自然日；可能上市日期的回看范围（调用方没给上市日期时用）
_INITIAL_SPAN_DAYS = 21
_NOT_INITIAL_AFTER_DAYS = 30


class Board(StrEnum):
    MAIN = "main"  # 沪深主板（含原中小板）
    GEM = "gem"  # 创业板
    STAR = "star"  # 科创板
    BSE = "bse"  # 北交所
    UNKNOWN = "unknown"


_MAIN_SH = ("600", "601", "603", "605")
_MAIN_SZ = ("000", "001", "002", "003")
_GEM_SZ = ("300", "301")
_STAR_SH = ("688", "689")
_BSE_PREFIX2 = ("43", "83", "87", "88")


def classify_board(code: str) -> Board:  # noqa: PLR0911  按交易所分派
    """按代码前缀和交易所后缀判断板块。格式 ``600000.SH``；对不上、不认识的返回 UNKNOWN。"""
    num, dot, suffix = code.strip().upper().partition(".")
    if not dot or len(num) != 6 or not (num.isascii() and num.isdigit()):
        return Board.UNKNOWN
    if suffix == "SH":
        if num.startswith(_MAIN_SH):
            return Board.MAIN
        if num.startswith(_STAR_SH):
            return Board.STAR
    elif suffix == "SZ":
        if num.startswith(_MAIN_SZ):
            return Board.MAIN
        if num.startswith(_GEM_SZ):
            return Board.GEM
    elif suffix == "BJ" and (num.startswith(_BSE_PREFIX2) or num.startswith("920")):
        return Board.BSE
    return Board.UNKNOWN


@dataclass(frozen=True)
class RatioRule:
    """涨跌幅比例。``st`` 为 None 表示 ST 和非 ST 都适用。"""

    board: Board
    start: date
    end: date | None
    st: bool | None
    up: Decimal
    down: Decimal
    source: str

    def covers(self, day: date, st: bool) -> bool:
        return (
            self.start <= day
            and (self.end is None or day < self.end)
            and (self.st is None or self.st == st)
        )


@dataclass(frozen=True)
class NewListingRule:
    """新股上市初期规则，按上市日期（不是交易日）选择。

    ``no_limit_days``：前几个交易日不设涨跌幅；``band``：首日的（涨幅, 跌幅）上下限，基准是发行价。
    """

    board: Board
    start: date
    end: date | None
    no_limit_days: int
    band: tuple[Decimal, Decimal] | None
    source: str

    def covers(self, listing: date) -> bool:
        return self.start <= listing and (self.end is None or listing < self.end)


@dataclass(frozen=True)
class LotRule:
    """买入数量：不小于 ``min_qty``，超出部分按 ``step`` 递增。"""

    board: Board
    start: date
    end: date | None
    min_qty: int
    step: int
    source: str

    def covers(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day < self.end)

    def conforms(self, qty: int) -> bool:
        return qty >= self.min_qty and (qty - self.min_qty) % self.step == 0


def _d(text: str) -> Decimal:
    return Decimal(text)


_S_1996 = "沪深交易所涨跌停板制度通知（1996-12-13 发布，12-16 起实施）；未核对原文"
_S_ST = "沪深交易所对“特别处理”股票实行 5% 涨跌幅的通知（1998-04-22）；未核对原文"
_S_GEM_OLD = "深交所创业板股票上市规则及交易规则（2009-10-30 开板）；未核对原文"
_S_GEM_REG = "《深圳证券交易所创业板交易特别规定》（2020-08-24 起）；未核对原文"
_S_STAR = "《上海证券交易所科创板股票交易特别规定》（2019-07-22 开板）；未核对原文"
_S_BSE = "《北京证券交易所交易规则（试行）》（2021-11-15 起施行）；未核对原文"
_S_IPO_44 = "沪深交易所 2013 年底发布的新股上市首日涨跌幅规则（2014 年起实施）；未核对原文"
_S_MAIN_REG = "沪深交易所交易规则（2023 年修订，主板全面注册制，2023-04-10 起）；未核对原文"

D_1996 = date(1996, 12, 16)
D_ST = date(1998, 4, 22)
D_STAR = date(2019, 7, 22)
D_GEM = date(2009, 10, 30)
D_GEM_REG = date(2020, 8, 24)
D_BSE = date(2021, 11, 15)
D_MAIN_REG = date(2023, 4, 10)
D_IPO_44 = date(2014, 1, 1)
# 主板风险警示股票涨跌幅比例调整的消息：生效日和新比例没有核对原文，保守地从这一天起 ST 返回未知
D_MAIN_ST_UNVERIFIED = date(2026, 7, 6)

RATIO_RULES: tuple[RatioRule, ...] = (
    RatioRule(Board.MAIN, D_1996, None, False, _d("0.10"), _d("0.10"), _S_1996),
    RatioRule(Board.MAIN, D_ST, D_MAIN_ST_UNVERIFIED, True, _d("0.05"), _d("0.05"), _S_ST),
    RatioRule(Board.GEM, D_GEM, D_GEM_REG, False, _d("0.10"), _d("0.10"), _S_GEM_OLD),
    RatioRule(Board.GEM, D_GEM, D_GEM_REG, True, _d("0.05"), _d("0.05"), _S_GEM_OLD),
    RatioRule(Board.GEM, D_GEM_REG, None, None, _d("0.20"), _d("0.20"), _S_GEM_REG),
    RatioRule(Board.STAR, D_STAR, None, None, _d("0.20"), _d("0.20"), _S_STAR),
    RatioRule(Board.BSE, D_BSE, None, None, _d("0.30"), _d("0.30"), _S_BSE),
)

NEW_LISTING_RULES: tuple[NewListingRule, ...] = (
    NewListingRule(Board.MAIN, D_IPO_44, D_MAIN_REG, 0, (_d("0.44"), _d("0.36")), _S_IPO_44),
    NewListingRule(Board.MAIN, D_MAIN_REG, None, 5, None, _S_MAIN_REG),
    NewListingRule(Board.GEM, D_IPO_44, D_GEM_REG, 0, (_d("0.44"), _d("0.36")), _S_IPO_44),
    NewListingRule(Board.GEM, D_GEM_REG, None, 5, None, _S_GEM_REG),
    NewListingRule(Board.STAR, D_STAR, None, 5, None, _S_STAR),
    # 2021-11-15 上市的有精选层转入的公司，首日规则不同，没有记录；之后新上市首日不设涨跌幅
    NewListingRule(Board.BSE, date(2021, 11, 16), None, 1, None, _S_BSE),
)

LOT_RULES: tuple[LotRule, ...] = (
    LotRule(
        Board.MAIN,
        D_1996,
        None,
        100,
        100,
        "沪深交易所交易规则（买入 100 股或其整数倍）；未核对原文",
    ),
    LotRule(
        Board.GEM, D_GEM, None, 100, 100, "深交所交易规则（买入 100 股或其整数倍）；未核对原文"
    ),
    LotRule(Board.STAR, D_STAR, None, 200, 1, _S_STAR),
    LotRule(Board.BSE, D_BSE, None, 100, 1, _S_BSE),
)


def _unknown(reason: str) -> PriceLimit:
    return PriceLimit(LimitStatus.UNKNOWN, None, None, reason)


def _round_tick(value: Decimal) -> Decimal:
    return value.quantize(_TICK, rounding=ROUND_HALF_UP)


def _limits(prev_close: Decimal, up: Decimal, down: Decimal, reason: str) -> PriceLimit:
    upper = _round_tick(prev_close * (_ONE + up))
    lower = _round_tick(prev_close * (_ONE - down))
    return PriceLimit(LimitStatus.LIMITED, upper, lower, reason)


def _check_decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} 必须是 Decimal（不接受 float），收到 {type(value).__name__}")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} 必须是大于 0 的有限数，收到 {value}")
    return value


def _find_ratio(board: Board, day: date, is_st: bool | None) -> RatioRule | str:
    """ST 未知时，ST 和非 ST 的比例必须相同才返回规则，否则返回拿不准的原因。"""
    candidates = (True, False) if is_st is None else (is_st,)
    found: list[RatioRule] = []
    for st in candidates:
        rule = next((r for r in RATIO_RULES if r.board is board and r.covers(day, st)), None)
        if rule is None:
            label = "ST" if st else "非 ST"
            note = "ST 状态未知，" if is_st is None else ""
            return f"{note}{board.value} {day} 没有适用于 {label} 的涨跌幅规则"
        found.append(rule)
    first = found[0]
    if any((r.up, r.down) != (first.up, first.down) for r in found):
        return f"{board.value} {day} 的 ST 状态未知，且 ST 与非 ST 的比例不同"
    return first


def _inconsistent(  # noqa: PLR0911  逐项检查，每项一个原因
    day: date, listing: date | None, index: int | None
) -> str | None:
    """上市日期、上市第几个交易日和交易日互相矛盾时返回原因。"""
    if listing is None:
        return None
    if listing > day:
        return f"上市日期 {listing} 晚于交易日 {day}"
    if index is None:
        return None
    if (index == 1) != (listing == day):
        return f"上市第 {index} 个交易日与上市日期 {listing}、交易日 {day} 矛盾"
    if (day - listing).days < index - 1:
        return f"上市日期 {listing} 到交易日 {day} 放不下 {index} 个交易日"
    if index <= _MAX_INITIAL_DAYS and (day - listing).days >= _NOT_INITIAL_AFTER_DAYS:
        return (
            f"上市第 {index} 个交易日与上市日期 {listing} 矛盾：距交易日 {day} 已有 30 个自然日以上"
        )
    return None


def _listing_rule(board: Board, listing: date) -> NewListingRule | None:
    return next((r for r in NEW_LISTING_RULES if r.board is board and r.covers(listing)), None)


def listing_day_index(calendar: Sequence[date], listing_date: date, trade_date: date) -> int | None:
    """上市首日为 1，按交易日历计。``calendar`` 升序；上市日或交易日不在日历里返回 None。"""
    i = bisect_left(calendar, listing_date)
    j = bisect_left(calendar, trade_date)
    if i >= len(calendar) or j >= len(calendar):
        return None
    if calendar[i] != listing_date or calendar[j] != trade_date or j < i:
        return None
    return j - i + 1


def price_limit(
    code: str,
    trade_date: date,
    prev_close: Decimal,
    *,
    is_st: bool | None,
    listing_date: date | None,
    listing_day_index: int | None,
) -> PriceLimit:
    """某只股票某个交易日的涨跌停价。

    ``prev_close`` 是未复权的前收盘价（上市首日是发行价）；``is_st`` 由调用方给，None 表示未知；
    ``listing_date`` 和 ``listing_day_index``（上市首日为 1）用来判断是否在上市初期，规则见
    docs/subsystems/market.md。拿不准返回 UNKNOWN，调用方应当按“不能成交”处理。
    """
    prev = _check_decimal("prev_close", prev_close)
    if listing_day_index is not None and listing_day_index < 1:
        raise ValueError(f"listing_day_index 从 1 开始，收到 {listing_day_index}")
    conflict = _inconsistent(trade_date, listing_date, listing_day_index)
    if conflict is not None:
        return _unknown(f"输入自相矛盾：{conflict}")

    board = classify_board(code)
    if board is Board.UNKNOWN:
        return _unknown(f"板块判断不了：{code}")

    initial = _initial_period(board, trade_date, prev, listing_date, listing_day_index)
    if initial is not None:
        return initial

    rule = _find_ratio(board, trade_date, is_st)
    if isinstance(rule, str):
        return _unknown(rule)
    return _limits(prev, rule.up, rule.down, rule.source)


def _initial_period(  # noqa: PLR0911  逐个排除，每步一个结果
    board: Board,
    day: date,
    prev: Decimal,
    listing_date: date | None,
    index: int | None,
) -> PriceLimit | None:
    """上市初期的判断。返回 None 表示不在初期，按普通比例；其余情况都有结果。"""
    if index is not None and index > _MAX_INITIAL_DAYS:
        return None
    if listing_date is not None and (day - listing_date).days >= _NOT_INITIAL_AFTER_DAYS:
        return None
    if index is None:
        return _unknown("可能在上市初期，但不知道上市第几个交易日")

    if listing_date is not None:
        rule = _listing_rule(board, listing_date)
    else:
        # 不知道上市日期：可能的上市日期都落在同一个制度里才能判断
        probes = {day - timedelta(days=_INITIAL_SPAN_DAYS), day}
        probes |= {
            r.start
            for r in NEW_LISTING_RULES
            if r.board is board and day - timedelta(days=_INITIAL_SPAN_DAYS) < r.start <= day
        }
        rules = {_listing_rule(board, p) for p in probes}
        if len(rules) != 1:
            return _unknown("可能在上市初期，不知道上市日期，且可能跨越新股规则的变更日")
        rule = rules.pop()
    if rule is None:
        return _unknown("可能在上市初期，但这个上市日期的新股规则没有记录")

    if index <= rule.no_limit_days:
        return PriceLimit(LimitStatus.UNLIMITED, None, None, rule.source)
    if rule.band is not None and index == 1:
        return _limits(prev, rule.band[0], rule.band[1], rule.source)
    return None


def check_buy(limit: PriceLimit, price: Decimal) -> Verdict:
    """涨停价（及以上）不能买；低于跌停价的价格不在当天的价格范围内，也不能买。"""
    px = _check_decimal("price", price)
    if limit.status is LimitStatus.UNKNOWN:
        return Verdict.UNKNOWN
    if limit.status is LimitStatus.UNLIMITED:
        return Verdict.ALLOWED
    assert limit.upper is not None and limit.lower is not None
    return Verdict.BLOCKED if px >= limit.upper or px < limit.lower else Verdict.ALLOWED


def check_sell(limit: PriceLimit, price: Decimal) -> Verdict:
    """跌停价（及以下）不能卖；高于涨停价的价格不在当天的价格范围内，也不能卖。"""
    px = _check_decimal("price", price)
    if limit.status is LimitStatus.UNKNOWN:
        return Verdict.UNKNOWN
    if limit.status is LimitStatus.UNLIMITED:
        return Verdict.ALLOWED
    assert limit.upper is not None and limit.lower is not None
    return Verdict.BLOCKED if px <= limit.lower or px > limit.upper else Verdict.ALLOWED


def lot_rule(code: str, trade_date: date) -> LotRule | None:
    """申报数量规则；板块或日期没有记录返回 None（未知）。"""
    board = classify_board(code)
    return next((r for r in LOT_RULES if r.board is board and r.covers(trade_date)), None)


def check_quantity(  # noqa: PLR0911  按买卖和各种数量情形分派
    code: str,
    trade_date: date,
    side: Side,
    qty: int,
    holding: int | None = None,
) -> Verdict:
    """申报数量是否符合整手规则。卖出时余额不足一手的零股可以一次性卖出，需要 ``holding``。"""
    if isinstance(qty, bool) or qty <= 0:
        raise ValueError(f"qty 必须是正整数，收到 {qty!r}")
    if holding is not None and (isinstance(holding, bool) or holding < 0):
        raise ValueError(f"holding 必须是非负整数，收到 {holding!r}")
    rule = lot_rule(code, trade_date)
    if rule is None:
        return Verdict.UNKNOWN
    if side is Side.BUY:
        return Verdict.ALLOWED if rule.conforms(qty) else Verdict.BLOCKED
    if holding is not None and qty > holding:
        return Verdict.BLOCKED
    if rule.conforms(qty):
        return Verdict.ALLOWED
    if holding is None:
        return Verdict.UNKNOWN
    if qty == holding:
        return Verdict.ALLOWED
    odd = holding % rule.step
    if rule.step > 1 and odd > 0 and qty % rule.step == odd:
        return Verdict.ALLOWED
    return Verdict.BLOCKED
