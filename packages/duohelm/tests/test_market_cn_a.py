"""A 股交易规则（#45）。

期望值都是按交易所规则手工算出的字面量，不用被测代码生成。四舍五入一律是 ROUND_HALF_UP。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from duohelm.market import (
    Board,
    LimitStatus,
    PriceLimit,
    Side,
    Verdict,
    check_buy,
    check_quantity,
    check_sell,
    classify_board,
    listing_day_index,
    lot_rule,
    price_limit,
)

D = Decimal
LIMITED = LimitStatus.LIMITED
UNLIMITED = LimitStatus.UNLIMITED
UNKNOWN = LimitStatus.UNKNOWN
OLD = 1000  # 上市很久，不在初期


def pl(
    code: str,
    day: date,
    prev: str,
    *,
    st: bool | None = False,
    listing: date | None = None,
    idx: int | None = OLD,
) -> PriceLimit:
    return price_limit(code, day, D(prev), is_st=st, listing_date=listing, listing_day_index=idx)


def limited(res: PriceLimit) -> tuple[str, str]:
    assert res.status is LIMITED, res
    assert res.upper is not None and res.lower is not None
    return (str(res.upper), str(res.lower))


# ---------- 板块 ----------


@pytest.mark.parametrize(
    ("code", "board"),
    [
        ("600000.SH", Board.MAIN),
        ("601318.SH", Board.MAIN),
        ("603259.SH", Board.MAIN),
        ("605499.SH", Board.MAIN),
        ("000001.SZ", Board.MAIN),
        ("001979.SZ", Board.MAIN),
        ("002594.SZ", Board.MAIN),
        ("003816.SZ", Board.MAIN),
        ("300750.SZ", Board.GEM),
        ("301269.SZ", Board.GEM),
        ("688981.SH", Board.STAR),
        ("689009.SH", Board.STAR),
        ("830799.BJ", Board.BSE),
        ("430047.BJ", Board.BSE),
        ("872925.BJ", Board.BSE),
        ("889999.BJ", Board.BSE),
        ("920002.BJ", Board.BSE),
        (" 600000.sh ", Board.MAIN),
        # 判断不了的
        ("600000.SZ", Board.UNKNOWN),  # 后缀和前缀对不上
        ("000001.SH", Board.UNKNOWN),  # 上证综指不是股票
        ("399001.SZ", Board.UNKNOWN),
        ("900901.SH", Board.UNKNOWN),  # B 股
        ("200002.SZ", Board.UNKNOWN),
        ("159915.SZ", Board.UNKNOWN),  # 基金
        ("510300.SH", Board.UNKNOWN),
        ("400001.BJ", Board.UNKNOWN),  # 老三板
        ("820001.BJ", Board.UNKNOWN),  # 优先股
        ("600000", Board.UNKNOWN),
        ("60000.SH", Board.UNKNOWN),
        ("ABCDEF.SH", Board.UNKNOWN),
        ("600000.HK", Board.UNKNOWN),
        ("", Board.UNKNOWN),
    ],
)
def test_classify_board(code: str, board: Board) -> None:
    assert classify_board(code) is board


# ---------- 普通涨跌停价与四舍五入 ----------


@pytest.mark.parametrize(
    ("prev", "up", "down"),
    [
        ("10.00", "11.00", "9.00"),
        ("9.95", "10.95", "8.96"),  # 10.945 进位、8.955 进位
        ("9.99", "10.99", "8.99"),  # 10.989、8.991
        ("1.25", "1.38", "1.13"),  # 1.375、1.125 都恰好在 .xx5
        ("10.05", "11.06", "9.05"),  # 11.055、9.045
        ("9.995", "10.99", "9.00"),  # 10.9945 舍、8.9955 进
        ("0.01", "0.01", "0.01"),  # 0.011、0.009
    ],
)
def test_main_board_rounding(prev: str, up: str, down: str) -> None:
    assert limited(pl("600000.SH", date(2024, 3, 5), prev)) == (up, down)


def test_float_is_rejected_not_rounded() -> None:
    with pytest.raises(TypeError):
        price_limit(
            "600000.SH",
            date(2024, 3, 5),
            9.95,  # type: ignore[arg-type]
            is_st=False,
            listing_date=None,
            listing_day_index=OLD,
        )
    with pytest.raises(ValueError, match="大于 0"):
        pl("600000.SH", date(2024, 3, 5), "0")


def test_shenzhen_main_and_sme_use_main_rules() -> None:
    assert limited(pl("002594.SZ", date(2019, 5, 6), "10.00")) == ("11.00", "9.00")
    assert limited(pl("000001.SZ", date(2024, 3, 5), "10.00")) == ("11.00", "9.00")


# ---------- ST ----------


@pytest.mark.parametrize(
    ("prev", "up", "down"),
    [
        ("3.15", "3.31", "2.99"),  # 3.3075、2.9925
        ("10.10", "10.61", "9.60"),  # 10.605、9.595
        ("10.00", "10.50", "9.50"),
    ],
)
def test_main_board_st_is_five_percent(prev: str, up: str, down: str) -> None:
    assert limited(pl("600000.SH", date(2024, 3, 5), prev, st=True)) == (up, down)


def test_st_unknown_is_not_treated_as_normal() -> None:
    res = pl("600000.SH", date(2024, 3, 5), "10.00", st=None)
    assert res.status is UNKNOWN
    assert res.upper is None and res.lower is None
    assert "ST" in res.reason


def test_st_unknown_is_harmless_where_ratios_are_equal() -> None:
    # 科创板、北交所、2020-08-24 起的创业板，ST 和非 ST 比例相同
    assert limited(pl("688981.SH", date(2024, 3, 5), "50.00", st=None)) == ("60.00", "40.00")
    assert limited(pl("830799.BJ", date(2024, 3, 5), "10.00", st=None)) == ("13.00", "7.00")
    assert limited(pl("300750.SZ", date(2020, 8, 24), "100.00", st=None)) == ("120.00", "80.00")
    # 2020-08-24 之前的创业板 ST 是 5%，ST 未知就不知道
    assert pl("300750.SZ", date(2020, 8, 21), "100.00", st=None).status is UNKNOWN


def test_st_date_bounds_on_main_board() -> None:
    # 1998-04-22 之前没有 ST
    assert pl("600000.SH", date(1998, 4, 21), "10.00", st=True).status is UNKNOWN
    assert limited(pl("600000.SH", date(1998, 4, 22), "10.00", st=True)) == ("10.50", "9.50")
    # 1996-12-16 之前没有涨跌停规则的记录
    assert pl("600000.SH", date(1996, 12, 13), "10.00").status is UNKNOWN
    assert limited(pl("600000.SH", date(1996, 12, 16), "10.00")) == ("11.00", "9.00")
    # 2026-07-06 起主板 ST 的比例未核对，返回未知；非 ST 不受影响
    assert limited(pl("600000.SH", date(2026, 7, 3), "10.00", st=True)) == ("10.50", "9.50")
    assert pl("600000.SH", date(2026, 7, 6), "10.00", st=True).status is UNKNOWN
    assert pl("600000.SH", date(2026, 7, 6), "10.00", st=None).status is UNKNOWN
    assert limited(pl("600000.SH", date(2026, 7, 6), "10.00", st=False)) == ("11.00", "9.00")


# ---------- 创业板 2020-08-24 ----------


def test_gem_ratio_changes_on_2020_08_24() -> None:
    code = "300750.SZ"
    assert limited(pl(code, date(2020, 8, 21), "100.00")) == ("110.00", "90.00")  # 周五
    assert limited(pl(code, date(2020, 8, 23), "100.00")) == ("110.00", "90.00")
    assert limited(pl(code, date(2020, 8, 24), "100.00")) == ("120.00", "80.00")  # 周一
    assert limited(pl(code, date(2020, 8, 25), "87.65")) == ("105.18", "70.12")  # 105.18、70.12


def test_gem_st_before_and_after_reform() -> None:
    assert limited(pl("300750.SZ", date(2020, 8, 21), "20.00", st=True)) == ("21.00", "19.00")
    assert limited(pl("300750.SZ", date(2020, 8, 24), "20.00", st=True)) == ("24.00", "16.00")


def test_gem_before_launch_is_unknown() -> None:
    assert pl("300001.SZ", date(2009, 10, 29), "10.00").status is UNKNOWN
    assert limited(pl("300001.SZ", date(2009, 10, 30), "10.00")) == ("11.00", "9.00")


def test_star_and_bse() -> None:
    assert limited(pl("688981.SH", date(2023, 6, 1), "50.00")) == ("60.00", "40.00")
    assert limited(pl("688981.SH", date(2023, 6, 1), "50.00", st=True)) == ("60.00", "40.00")
    assert pl("688001.SH", date(2019, 7, 19), "10.00").status is UNKNOWN
    assert limited(pl("830799.BJ", date(2023, 6, 1), "8.88")) == ("11.54", "6.22")  # 11.544、6.216
    assert limited(pl("830799.BJ", date(2023, 6, 1), "8.88", st=True)) == ("11.54", "6.22")
    assert pl("830799.BJ", date(2021, 11, 12), "8.88").status is UNKNOWN


def test_unknown_board_is_unknown() -> None:
    res = pl("159915.SZ", date(2024, 3, 5), "2.00")
    assert res.status is UNKNOWN and res.upper is None


# ---------- 新股上市初期 ----------


def test_main_board_registration_new_listing_first_five_days_unlimited() -> None:
    listing = date(2023, 4, 10)
    for idx in range(1, 6):
        res = pl("603000.SH", date(2023, 4, 10), "10.00", listing=listing, idx=idx)
        assert res.status is UNLIMITED
        assert res.upper is None and res.lower is None
    assert limited(pl("603000.SH", date(2023, 4, 18), "30.00", listing=listing, idx=6)) == (
        "33.00",
        "27.00",
    )


def test_main_board_before_registration_first_day_band() -> None:
    listing = date(2022, 5, 10)
    # 发行价 12.34：+44% = 17.7696 → 17.77，-36% = 7.8976 → 7.90
    assert limited(pl("601000.SH", listing, "12.34", listing=listing, idx=1)) == ("17.77", "7.90")
    assert limited(pl("601000.SH", listing, "6.55", listing=listing, idx=1)) == ("9.43", "4.19")
    # 第二天起 10%
    assert limited(pl("601000.SH", date(2022, 5, 11), "17.77", listing=listing, idx=2)) == (
        "19.55",
        "15.99",
    )  # 19.547、15.993


def test_main_board_listed_before_2014_initial_days_unknown() -> None:
    listing = date(2013, 12, 30)
    assert pl("601000.SH", listing, "10.00", listing=listing, idx=1).status is UNKNOWN
    assert pl("601000.SH", date(2014, 1, 3), "10.00", listing=listing, idx=3).status is UNKNOWN
    # 过了初期就不需要上市日期的规则
    assert limited(pl("601000.SH", date(2014, 1, 8), "10.00", listing=listing, idx=6)) == (
        "11.00",
        "9.00",
    )


def test_star_new_listing() -> None:
    listing = date(2019, 7, 22)
    assert pl("688001.SH", date(2019, 7, 22), "10.00", listing=listing, idx=1).status is UNLIMITED
    assert pl("688001.SH", date(2019, 7, 26), "10.00", listing=listing, idx=5).status is UNLIMITED
    assert limited(pl("688001.SH", date(2019, 7, 29), "20.00", listing=listing, idx=6)) == (
        "24.00",
        "16.00",
    )


def test_gem_new_listing_across_reform_date() -> None:
    # 2020-08-24 起上市：前 5 日不设涨跌幅
    new = date(2020, 8, 24)
    assert pl("300999.SZ", new, "10.00", listing=new, idx=1).status is UNLIMITED
    assert pl("300999.SZ", date(2020, 8, 28), "10.00", listing=new, idx=5).status is UNLIMITED
    # 2020-08-20 上市（旧制度）：首日 +44%/-36%
    old = date(2020, 8, 20)
    assert limited(pl("300998.SZ", old, "10.00", listing=old, idx=1)) == ("14.40", "6.40")
    # 旧制度上市的，第 2 日（2020-08-21）仍是 10%，第 3 日（2020-08-24）起全市场 20%
    assert limited(pl("300998.SZ", date(2020, 8, 21), "14.40", listing=old, idx=2)) == (
        "15.84",
        "12.96",
    )
    assert limited(pl("300998.SZ", date(2020, 8, 24), "14.00", listing=old, idx=3)) == (
        "16.80",
        "11.20",
    )


def test_bse_new_listing() -> None:
    listing = date(2023, 3, 1)
    assert pl("830799.BJ", listing, "10.00", listing=listing, idx=1).status is UNLIMITED
    assert limited(pl("830799.BJ", date(2023, 3, 2), "20.00", listing=listing, idx=2)) == (
        "26.00",
        "14.00",
    )
    # 2021-11-15 上市的混有精选层转入的公司，首日规则没有记录
    first = date(2021, 11, 15)
    assert pl("830799.BJ", first, "10.00", listing=first, idx=1).status is UNKNOWN
    assert pl("830799.BJ", date(2021, 11, 16), "10.00", listing=first, idx=2).status is UNKNOWN
    assert limited(pl("830799.BJ", date(2021, 11, 22), "10.00", listing=first, idx=6)) == (
        "13.00",
        "7.00",
    )


def test_listing_info_insufficient() -> None:
    day = date(2024, 3, 5)
    # 什么都不知道：不能排除是新股
    assert pl("600000.SH", day, "10.00", listing=None, idx=None).status is UNKNOWN
    # 上市第几个交易日不知道，上市日期很近
    assert pl("600000.SH", day, "10.00", listing=date(2024, 3, 1), idx=None).status is UNKNOWN
    # 上市日期很久以前，不需要序号
    assert limited(pl("600000.SH", day, "10.00", listing=date(2015, 1, 5), idx=None)) == (
        "11.00",
        "9.00",
    )
    # 只知道序号 >= 6
    assert limited(pl("600000.SH", day, "10.00", listing=None, idx=6)) == ("11.00", "9.00")
    # 只知道序号 <= 5，上市日期不知道：落在同一个制度里就能判断
    assert pl("688001.SH", day, "10.00", listing=None, idx=3).status is UNLIMITED
    # 可能的上市日期跨过 2020-08-24，不能判断
    assert pl("300999.SZ", date(2020, 8, 26), "10.00", listing=None, idx=3).status is UNKNOWN


def test_bad_listing_inputs_raise() -> None:
    with pytest.raises(ValueError, match="从 1 开始"):
        pl("600000.SH", date(2024, 3, 5), "10.00", idx=0)
    with pytest.raises(ValueError, match="晚于交易日"):
        pl("600000.SH", date(2024, 3, 5), "10.00", listing=date(2024, 3, 6), idx=1)


def test_listing_day_index() -> None:
    cal = [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 7)]
    assert listing_day_index(cal, date(2024, 3, 4), date(2024, 3, 4)) == 1
    assert listing_day_index(cal, date(2024, 3, 4), date(2024, 3, 7)) == 4
    assert listing_day_index(cal, date(2024, 3, 5), date(2024, 3, 4)) is None
    assert listing_day_index(cal, date(2024, 3, 2), date(2024, 3, 5)) is None  # 不在日历里
    assert listing_day_index(cal, date(2024, 3, 4), date(2024, 3, 8)) is None
    assert listing_day_index([], date(2024, 3, 4), date(2024, 3, 4)) is None


# ---------- 能否买入、卖出 ----------


def test_check_buy_and_sell_at_limits() -> None:
    lim = pl("600000.SH", date(2024, 3, 5), "10.00")  # 11.00 / 9.00
    assert check_buy(lim, D("11.00")) is Verdict.BLOCKED  # 涨停价不能买
    assert check_buy(lim, D("10.99")) is Verdict.ALLOWED
    assert check_buy(lim, D("9.00")) is Verdict.ALLOWED  # 跌停价可以买
    assert check_buy(lim, D("8.99")) is Verdict.BLOCKED  # 超出范围
    assert check_buy(lim, D("11.01")) is Verdict.BLOCKED
    assert check_sell(lim, D("9.00")) is Verdict.BLOCKED  # 跌停价不能卖
    assert check_sell(lim, D("9.01")) is Verdict.ALLOWED
    assert check_sell(lim, D("11.00")) is Verdict.ALLOWED  # 涨停价可以卖
    assert check_sell(lim, D("11.01")) is Verdict.BLOCKED
    assert check_sell(lim, D("8.99")) is Verdict.BLOCKED


def test_check_unknown_and_unlimited() -> None:
    unknown = pl("600000.SH", date(2024, 3, 5), "10.00", st=None)
    assert check_buy(unknown, D("10.00")) is Verdict.UNKNOWN
    assert check_sell(unknown, D("10.00")) is Verdict.UNKNOWN
    listing = date(2023, 4, 10)
    free = pl("603000.SH", listing, "10.00", listing=listing, idx=1)
    assert check_buy(free, D("30.00")) is Verdict.ALLOWED
    assert check_sell(free, D("3.00")) is Verdict.ALLOWED


def test_check_price_must_be_decimal() -> None:
    lim = pl("600000.SH", date(2024, 3, 5), "10.00")
    with pytest.raises(TypeError):
        check_buy(lim, 10.5)  # type: ignore[arg-type]


# ---------- 整手 ----------

TODAY = date(2024, 3, 5)


@pytest.mark.parametrize(
    ("code", "qty", "verdict"),
    [
        ("600000.SH", 100, Verdict.ALLOWED),
        ("600000.SH", 300, Verdict.ALLOWED),
        ("600000.SH", 150, Verdict.BLOCKED),
        ("600000.SH", 50, Verdict.BLOCKED),
        ("300750.SZ", 100, Verdict.ALLOWED),
        ("300750.SZ", 101, Verdict.BLOCKED),
        ("688981.SH", 200, Verdict.ALLOWED),
        ("688981.SH", 201, Verdict.ALLOWED),
        ("688981.SH", 199, Verdict.BLOCKED),
        ("688981.SH", 100, Verdict.BLOCKED),
        ("830799.BJ", 100, Verdict.ALLOWED),
        ("830799.BJ", 101, Verdict.ALLOWED),
        ("830799.BJ", 99, Verdict.BLOCKED),
    ],
)
def test_buy_quantity(code: str, qty: int, verdict: Verdict) -> None:
    assert check_quantity(code, TODAY, Side.BUY, qty) is verdict


@pytest.mark.parametrize(
    ("code", "holding", "qty", "verdict"),
    [
        # 主板：零股一次性卖出
        ("600000.SH", 250, 250, Verdict.ALLOWED),
        ("600000.SH", 250, 200, Verdict.ALLOWED),
        ("600000.SH", 250, 150, Verdict.ALLOWED),  # 100 加零股 50
        ("600000.SH", 250, 50, Verdict.ALLOWED),  # 只卖零股
        ("600000.SH", 250, 100, Verdict.ALLOWED),
        ("600000.SH", 250, 30, Verdict.BLOCKED),
        ("600000.SH", 250, 300, Verdict.BLOCKED),  # 超过持仓
        ("600000.SH", 80, 80, Verdict.ALLOWED),
        ("600000.SH", 80, 50, Verdict.BLOCKED),
        ("600000.SH", 200, 150, Verdict.BLOCKED),  # 没有零股，不能卖半手
        # 科创板：不足 200 股的余额全部卖出
        ("688981.SH", 150, 150, Verdict.ALLOWED),
        ("688981.SH", 150, 100, Verdict.BLOCKED),
        ("688981.SH", 250, 250, Verdict.ALLOWED),
        ("688981.SH", 250, 201, Verdict.ALLOWED),
        ("688981.SH", 250, 100, Verdict.BLOCKED),
        # 北交所：不足 100 股的余额全部卖出
        ("830799.BJ", 60, 60, Verdict.ALLOWED),
        ("830799.BJ", 60, 50, Verdict.BLOCKED),
        ("830799.BJ", 160, 150, Verdict.ALLOWED),
    ],
)
def test_sell_quantity_with_holding(code: str, holding: int, qty: int, verdict: Verdict) -> None:
    assert check_quantity(code, TODAY, Side.SELL, qty, holding) is verdict


def test_sell_quantity_without_holding() -> None:
    assert check_quantity("600000.SH", TODAY, Side.SELL, 200) is Verdict.ALLOWED
    assert check_quantity("600000.SH", TODAY, Side.SELL, 50) is Verdict.UNKNOWN


def test_quantity_rule_dates() -> None:
    assert check_quantity("688001.SH", date(2019, 7, 19), Side.BUY, 200) is Verdict.UNKNOWN
    assert check_quantity("688001.SH", date(2019, 7, 22), Side.BUY, 200) is Verdict.ALLOWED
    assert check_quantity("830799.BJ", date(2021, 11, 12), Side.BUY, 100) is Verdict.UNKNOWN
    assert check_quantity("830799.BJ", date(2021, 11, 15), Side.BUY, 100) is Verdict.ALLOWED
    assert check_quantity("300001.SZ", date(2009, 10, 29), Side.BUY, 100) is Verdict.UNKNOWN
    assert check_quantity("159915.SZ", TODAY, Side.BUY, 100) is Verdict.UNKNOWN
    assert lot_rule("159915.SZ", TODAY) is None


def test_quantity_bad_inputs() -> None:
    with pytest.raises(ValueError, match="正整数"):
        check_quantity("600000.SH", TODAY, Side.BUY, 0)
    with pytest.raises(ValueError, match="非负"):
        check_quantity("600000.SH", TODAY, Side.SELL, 100, -1)
