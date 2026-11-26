"""ST 判定和快照内容（#43）。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from duohelm.tasks.snapshots import SnapshotUnavailableError, is_st_name, st_snapshot
from duohelm_sdk.schema import SECURITIES_SCHEMA

DAY = date(2024, 1, 5)


@pytest.mark.parametrize(
    "name",
    [
        "ST平安",
        "*ST平安",
        "SST浦发",
        "S*ST浦发",
        "ＳＴ平安",
        " *ST 平安",
        "st平安",
        "*ST　平安",
        "ST",
    ],
)
def test_st_names(name: str) -> None:
    assert is_st_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "平安银行",
        "中ST股份",
        "东方STAR",
        "STAR 科技",
        "S浦发",
        "XST",
        "N平安",
        "*平安",
        "",
        "S*平安",
    ],
)
def test_non_st_names(name: str) -> None:
    assert not is_st_name(name)


def securities(rows: list[tuple[str, str | None, str, date | None, date | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        [(s, n, s[-2:], k, ld, dd) for s, n, k, ld, dd in rows],
        schema=SECURITIES_SCHEMA,
        orient="row",
    )


def test_st_snapshot_only_listed_stocks() -> None:
    df = securities(
        [
            ("600001.SH", "*ST一号", "stock", date(2000, 1, 1), None),
            ("600002.SH", "ST二号退", "stock", date(2000, 1, 1), date(2024, 1, 4)),  # 已退市
            ("600003.SH", "ST三号", "stock", date(2000, 1, 1), date(2024, 1, 5)),  # 退市日当天
            ("600004.SH", "ST四号", "stock", date(2000, 1, 1), date(2024, 1, 9)),  # 将来才退市
            ("600005.SH", "ST五号", "stock", date(2024, 1, 8), None),  # 还没上市
            ("600006.SH", "ST六号", "stock", None, None),  # 上市日未知
            ("000001.SH", "ST指数", "index", None, None),  # 不是股票
            ("600007.SH", "中ST股份", "stock", date(2000, 1, 1), None),
            ("600008.SH", None, "stock", date(2000, 1, 1), None),
            ("600009.SH", "  ", "stock", date(2000, 1, 1), None),
            ("600010.SH", "浦发银行", "stock", date(2000, 1, 1), None),
        ]
    )
    st, unknown = st_snapshot(df, DAY)
    assert st.rows() == [
        (DAY, "600001.SH", "*ST一号"),
        (DAY, "600004.SH", "ST四号"),
        (DAY, "600006.SH", "ST六号"),
    ]
    assert unknown == 2


def test_st_snapshot_all_names_unknown_raises() -> None:
    df = securities([("600001.SH", None, "stock", None, None)])
    with pytest.raises(SnapshotUnavailableError):
        st_snapshot(df, DAY)
