"""入库质量报告的检查逻辑（#31）：用真实形态的数据直接测 QualityCollector。"""

from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl

from duohelm.tasks.quality import MAX_EXAMPLES, QualityCollector, report_statuses
from duohelm_sdk.adjust import EX_EVENTS_SCHEMA
from duohelm_sdk.schema import ADJ_FACTORS_SCHEMA, BARS_SCHEMA, SECURITIES_SCHEMA

D = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
CALENDAR = pl.DataFrame({"market": ["CN"] * 4, "date": D})
SECURITIES = pl.DataFrame(
    [
        ("600000.SH", "浦发银行", "SH", "stock", date(1999, 11, 10), None),
        ("688001.SH", "新股", "SH", "stock", date(2024, 1, 4), None),  # 区间内上市
        ("600005.SH", "武钢股份", "SH", "stock", date(1999, 8, 3), date(2024, 1, 3)),  # 区间内退市
    ],
    schema=SECURITIES_SCHEMA,
    orient="row",
)


def bars(*rows: tuple[Any, ...]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=BARS_SCHEMA, orient="row")


def row(d: date, s: str, *ohlc: float | None) -> tuple[Any, ...]:
    return (d, s, *ohlc, 1000.0, 10000.0)


EMPTY_BARS = bars()
NO_FACTORS = pl.DataFrame(schema=ADJ_FACTORS_SCHEMA)


def collector(**kwargs: Any) -> QualityCollector:
    return QualityCollector(CALENDAR, SECURITIES, D[0], D[-1], **kwargs)


def test_ohlc_rules() -> None:
    good = row(D[0], "600000.SH", 10, 11, 9, 10.5)
    flat = row(D[1], "600000.SH", 10, 10, 10, 10)  # 一字板：high == low 合法
    high_low = row(D[2], "600000.SH", 10, 10.2, 9, 10.5)  # high < close
    low_high = row(D[3], "600000.SH", 10, 11, 10.3, 10.5)  # low > open
    negative = row(D[0], "600005.SH", 10, 11, -1, 10.5)
    zero = row(D[1], "600005.SH", 0, 0, 0, 0)
    nan = row(D[2], "600005.SH", float("nan"), 11, 9, 10.5)
    null = row(D[3], "688001.SH", None, 11, 9, 10.5)
    # 价格非正、高低价也不一致（high 低于 close，low 高于 open）：只计入 non_positive_price
    neg_and_inconsistent = row(D[0], "688001.SH", 5, -2, 8, 3)
    c = collector()
    c.add_batch(["600000.SH"], bars(good, flat, high_low, low_high), EMPTY_BARS, NO_FACTORS)
    c.add_batch(
        ["600005.SH", "688001.SH"],
        bars(negative, zero, nan, null, neg_and_inconsistent),
        EMPTY_BARS,
        NO_FACTORS,
    )
    ohlc = c.finish("p")["checks"]["ohlc"]
    assert ohlc["status"] == "issues"
    assert ohlc["rows_checked"] == 9
    assert ohlc["rows_with_issues"] == 7
    assert ohlc["issues"] == {
        "missing_price": 2,
        "non_positive_price": 3,
        "high_below_open_close": 1,
        "low_above_open_close": 1,
    }
    assert ohlc["symbols_affected"] == 3
    assert {(e["symbol"], e["date"]) for e in ohlc["examples"]} == {
        ("600000.SH", "2024-01-04"),
        ("600000.SH", "2024-01-05"),
        ("600005.SH", "2024-01-02"),
        ("600005.SH", "2024-01-03"),
        ("600005.SH", "2024-01-04"),
        ("688001.SH", "2024-01-05"),
        ("688001.SH", "2024-01-02"),
    }
    by_key = {(e["symbol"], e["date"]): e["problems"] for e in ohlc["examples"]}
    assert by_key[("688001.SH", "2024-01-02")] == ["non_positive_price"]


def test_clean_ohlc_is_ok_and_examples_are_capped() -> None:
    c = collector()
    c.add_batch(
        ["600000.SH"], bars(row(D[0], "600000.SH", 10, 11, 9, 10.5)), EMPTY_BARS, NO_FACTORS
    )
    assert c.finish("p")["checks"]["ohlc"]["status"] == "ok"

    many = pl.DataFrame(
        {
            "date": [D[0]] * 50,
            "symbol": [f"6{i:05d}.SH" for i in range(50)],
            "open": [10.0] * 50,
            "high": [9.0] * 50,
            "low": [9.0] * 50,
            "close": [10.0] * 50,
            "volume": [1.0] * 50,
            "amount": [1.0] * 50,
        },
        schema=BARS_SCHEMA,
    )
    c = collector()
    c.add_batch([], many, EMPTY_BARS, NO_FACTORS)
    ohlc = c.finish("p")["checks"]["ohlc"]
    assert ohlc["rows_with_issues"] == 50 and len(ohlc["examples"]) == MAX_EXAMPLES


def test_dates_not_in_calendar() -> None:
    saturday = date(2024, 1, 6)
    c = collector()
    c.add_batch(
        ["600000.SH"],
        bars(row(D[0], "600000.SH", 10, 11, 9, 10), row(saturday, "600000.SH", 10, 11, 9, 10)),
        EMPTY_BARS,
        NO_FACTORS,
    )
    cal = c.finish("p")["checks"]["calendar_dates"]
    assert cal["status"] == "issues"
    assert cal["rows_not_in_calendar"] == 1
    assert cal["examples"] == [
        {"symbol": "600000.SH", "date": "2024-01-06", "problems": ["date_not_in_calendar"]}
    ]


def test_missing_days_respect_listing_and_placeholders() -> None:
    """应有的交易日按上市日、退市日截取；停牌占位行单独计数，不算缺失。"""
    placeholders = bars((D[1], "600000.SH", 10.0, 10.0, 10.0, 10.0, 0.0, 0.0))
    c = collector()
    c.add_batch(
        ["600000.SH", "688001.SH", "600005.SH"],
        bars(
            # 600000.SH：01-03 停牌占位；01-04 缺；01-02、01-05 有
            row(D[0], "600000.SH", 10, 11, 9, 10),
            row(D[3], "600000.SH", 10, 11, 9, 10),
            # 688001.SH 01-04 上市：应有 01-04、01-05，只有 01-04 -> 缺 1
            row(D[2], "688001.SH", 10, 11, 9, 10),
            # 600005.SH 01-03 退市：应有 01-02、01-03，都有
            row(D[0], "600005.SH", 10, 11, 9, 10),
            row(D[1], "600005.SH", 10, 11, 9, 10),
        ),
        placeholders,
        NO_FACTORS,
    )
    missing = c.finish("p")["checks"]["missing_days"]
    assert missing["status"] == "issues"
    assert missing["suspension"] == "unknown"
    assert missing["expected_days"] == 4 + 2 + 2
    assert missing["missing_total"] == 2
    assert missing["placeholder_total"] == 1
    assert missing["symbols_with_placeholder"] == 1
    assert missing["per_symbol"] == [
        {"symbol": "600000.SH", "missing": 1, "placeholder": 1},
        {"symbol": "688001.SH", "missing": 1, "placeholder": 0},
    ]
    assert missing["symbols_with_missing"] == 2


def test_unknown_exactness_is_flagged_like_approximate() -> None:
    """有退市日、但 exact 为 null 或没有记录：不知道精不精确，也要标注，不能漏掉。"""
    null_exact = pl.DataFrame(
        {"symbol": ["600005.SH"], "exact": [None]},
        schema={"symbol": pl.String(), "exact": pl.Boolean()},
    )
    no_record = pl.DataFrame(schema={"symbol": pl.String(), "exact": pl.Boolean()})
    for quality in (null_exact, no_record, None):
        c = collector(delist_quality=quality)
        c.add_batch(
            ["600005.SH"], bars(row(D[0], "600005.SH", 10, 11, 9, 10)), EMPTY_BARS, NO_FACTORS
        )
        missing = c.finish("p")["checks"]["missing_days"]
        assert missing["symbols_with_approximate_delist_date"] == 1
        assert missing["per_symbol"][0]["delist_date_approximate"] is True


def test_report_range_is_first_and_last_trading_day() -> None:
    """报告的 start、end 是日历里实际覆盖到的第一个和最后一个交易日，不是请求的自然日。"""
    c = QualityCollector(CALENDAR, SECURITIES, date(2024, 1, 1), date(2024, 1, 7))
    report = c.finish("p")
    assert (report["start"], report["end"]) == ("2024-01-02", "2024-01-05")
    empty = QualityCollector(CALENDAR.clear(), SECURITIES, D[0], D[-1]).finish("p")
    assert empty["start"] is None and empty["end"] is None


def test_approximate_delist_date_is_flagged() -> None:
    """退市日不精确的证券：缺失天数可能少算，在报告里标注；精确的不标。"""
    quality = pl.DataFrame(
        {"symbol": ["600000.SH", "600005.SH"], "exact": [None, False]},
        schema={"symbol": pl.String(), "exact": pl.Boolean()},
    )
    c = collector(delist_quality=quality)
    # 600005.SH 退市日 01-03（近似），应有 01-02、01-03，只有 01-02 -> 缺 1
    c.add_batch(
        ["600000.SH", "600005.SH"],
        bars(row(D[0], "600005.SH", 10, 11, 9, 10)),
        EMPTY_BARS,
        NO_FACTORS,
    )
    missing = c.finish("p")["checks"]["missing_days"]
    assert missing["symbols_with_approximate_delist_date"] == 1
    flagged = {m["symbol"]: m.get("delist_date_approximate") for m in missing["per_symbol"]}
    assert flagged == {"600000.SH": None, "600005.SH": True}

    exact = pl.DataFrame(
        {"symbol": ["600005.SH"], "exact": [True]},
        schema={"symbol": pl.String(), "exact": pl.Boolean()},
    )
    c = collector(delist_quality=exact)
    c.add_batch(["600005.SH"], bars(row(D[0], "600005.SH", 10, 11, 9, 10)), EMPTY_BARS, NO_FACTORS)
    missing = c.finish("p")["checks"]["missing_days"]
    assert missing["symbols_with_approximate_delist_date"] == 0
    assert "delist_date_approximate" not in missing["per_symbol"][0]


def test_symbol_without_any_bars_misses_all_expected_days() -> None:
    c = collector()
    c.add_batch(["600000.SH"], EMPTY_BARS, EMPTY_BARS, NO_FACTORS)
    assert c.finish("p")["checks"]["missing_days"]["missing_total"] == 4


def test_adj_factor_jumps_unavailable_without_ex_events() -> None:
    """没有除权除息记录：报告“无法检查”，不是“没有问题”。"""
    factors = pl.DataFrame(
        [(date(2024, 1, 4), "600000.SH", 1.5)], schema=ADJ_FACTORS_SCHEMA, orient="row"
    )
    c = collector()
    c.add_batch(["600000.SH"], EMPTY_BARS, EMPTY_BARS, factors)
    report = c.finish("p")
    adj = report["checks"]["adj_factor_jumps"]
    assert adj["status"] == "unavailable"
    assert adj["reason"]
    assert report_statuses(report)["adj_factor_jumps"] == "unavailable"


def events(*rows: tuple[Any, ...]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=EX_EVENTS_SCHEMA, orient="row")


def test_adj_factor_jumps_match_ex_events() -> None:
    factors = pl.DataFrame(
        [
            (date(2020, 6, 1), "600000.SH", 1.02),  # 有事件
            (date(2024, 1, 4), "600000.SH", 1.2),  # 没有事件：因子跳变对不上
            (date(2024, 1, 5), "600000.SH", 1.2),  # 因子没变（比例 1）：不算跳变
        ],
        schema=ADJ_FACTORS_SCHEMA,
        orient="row",
    )
    ev = events(
        (date(2020, 6, 1), "600000.SH", 0.5, 0.0, 0.0, 0.0),
        (date(2023, 6, 1), "600000.SH", 0.4, 0.0, 0.0, 0.0),  # 有事件但没有因子
        (date(2020, 6, 1), "000001.SZ", 0.5, 0.0, 0.0, 0.0),  # 别的批次的证券：不看
    )
    c = collector(ex_events=ev)
    c.add_batch(["600000.SH"], EMPTY_BARS, EMPTY_BARS, factors)
    adj = c.finish("p")["checks"]["adj_factor_jumps"]
    assert adj["status"] == "issues"
    assert adj["factor_jumps_without_event"] == 1
    assert adj["events_without_factor"] == 1
    assert adj["examples_factor_jumps_without_event"] == [
        {"symbol": "600000.SH", "date": "2024-01-04"}
    ]
    assert adj["examples_events_without_factor"] == [{"symbol": "600000.SH", "date": "2023-06-01"}]


def test_adj_factor_jumps_ok_when_matched() -> None:
    factors = pl.DataFrame(
        [(date(2020, 6, 1), "600000.SH", 1.02)], schema=ADJ_FACTORS_SCHEMA, orient="row"
    )
    ev = events((date(2020, 6, 1), "600000.SH", 0.5, 0.0, 0.0, 0.0))
    c = collector(ex_events=ev)
    c.add_batch(["600000.SH"], EMPTY_BARS, EMPTY_BARS, factors)
    assert c.finish("p")["checks"]["adj_factor_jumps"]["status"] == "ok"


def test_delist_date_conflicts_list_both_sources_and_dates() -> None:
    """精确源说在市而覆盖了别人的退市日：逐只列出两边的数据源和日期；没有时为 ok。"""
    assert collector().finish("p")["checks"]["delist_date_conflicts"] == {
        "status": "ok",
        "count": 0,
        "examples": [],
    }
    conflicts = pl.DataFrame(
        {
            "symbol": ["600000.SH", "600005.SH"],
            "other_source": ["eltdx", "来源未知"],
            "other_delist_date": [date(2020, 5, 6), date(2024, 1, 3)],
        }
    )
    report = collector(delist_conflicts=conflicts).finish("xtdata")
    check = report["checks"]["delist_date_conflicts"]
    assert check["status"] == "issues" and check["count"] == 2
    assert check["examples"][0] == {
        "symbol": "600000.SH",
        "exact_source": "xtdata",
        "exact_delist_date": None,
        "other_source": "eltdx",
        "other_delist_date": "2020-05-06",
    }
    assert check["examples"][1]["other_source"] == "来源未知"
