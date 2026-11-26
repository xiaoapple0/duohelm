"""ST 判定和快照内容（#43）。"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import polars as pl
import pytest

from duohelm.isolation.runner import FailureReason, PluginRunError, PluginSession
from duohelm.tasks.snapshots import (
    SnapshotUnavailableError,
    fetch_index_members,
    is_st_name,
    merge_st_rows,
    st_snapshot,
)
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
        (DAY, "600001.SH", "*ST一号", "st"),
        (DAY, "600004.SH", "ST四号", "st"),
        (DAY, "600006.SH", "ST六号", "st"),
        (DAY, "600008.SH", None, "unknown"),  # 名称为空：状态未知，不是非 ST
        (DAY, "600009.SH", None, "unknown"),
    ]
    assert unknown == 2


def test_st_snapshot_all_names_unknown_raises() -> None:
    df = securities([("600001.SH", None, "stock", None, None)])
    with pytest.raises(SnapshotUnavailableError):
        st_snapshot(df, DAY)


def st_frame(rows: list[tuple[str, str | None, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        [(DAY, s, n, st) for s, n, st in rows],
        schema={"date": pl.Date, "symbol": pl.String, "name": pl.String, "status": pl.String},
        orient="row",
    )


def test_merge_st_rows_keeps_known_rows_when_now_unknown() -> None:
    existing = st_frame([("A", "ST甲", "st"), ("B", "ST乙", "st"), ("C", None, "unknown")])
    new = st_frame([("A", None, "unknown"), ("C", "ST丙", "st"), ("D", "ST丁", "st")])
    write, gone = merge_st_rows(existing, new)
    # A 这次拿不到名称：保持原来的 ST 行，不写也不删；B 不在名单里：作废；C 现在知道了：覆盖
    assert write.get_column("symbol").to_list() == ["C", "D"]
    assert gone.get_column("symbol").to_list() == ["B"]


class FakeSession:
    def __init__(self, result: object = None, error: PluginRunError | None = None) -> None:
        self.result, self.error = result, error

    def call(self, method: str, *args: object, **kwargs: object) -> object:
        assert method == "index_constituents" and args == ("000300.SH", DAY, DAY)
        if self.error is not None:
            raise self.error
        return self.result


def members(session: FakeSession) -> pl.DataFrame:
    return fetch_index_members(cast(PluginSession, session), "000300.SH", DAY)


def index_frame(rows: list[tuple[Any, ...]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={"symbol": pl.String, "in_date": pl.Date, "out_date": pl.Date},
        orient="row",
    )


@pytest.mark.parametrize("reason", ["unsupported", "plugin_error"])
def test_index_plugin_errors_are_unknown(reason: FailureReason) -> None:
    with pytest.raises(SnapshotUnavailableError):
        members(FakeSession(error=PluginRunError(reason, "x")))


@pytest.mark.parametrize("reason", ["timeout", "crashed", "cancelled", "memory", "protocol"])
def test_index_infrastructure_errors_propagate(reason: FailureReason) -> None:
    """超时、崩溃、取消不是“这个指数没有成分”：交给任务按重试或失败处理。"""
    with pytest.raises(PluginRunError) as info:
        members(FakeSession(error=PluginRunError(reason, "x")))
    assert info.value.reason == reason


def test_index_null_symbol_is_unknown() -> None:
    frame = index_frame([("600000.SH", None, None), (None, None, None)])
    with pytest.raises(SnapshotUnavailableError, match="空的证券代码"):
        members(FakeSession(frame))


def test_index_members_filter_in_and_out_dates() -> None:
    """in_date 当天已是成分；out_date 是调出生效日，当天已不是成分。"""
    frame = index_frame(
        [
            ("A", date(2024, 1, 5), None),  # 当天调入：是
            ("B", date(2024, 1, 8), None),  # 明天才调入：不是
            ("C", None, date(2024, 1, 5)),  # 当天调出生效：不是
            ("D", None, date(2024, 1, 8)),  # 明天才调出：是
            ("E", date(2020, 1, 1), date(2023, 1, 1)),  # 早已调出：不是
            ("A", date(2024, 1, 5), None),  # 重复：只留一行
        ]
    )
    assert members(FakeSession(frame)).get_column("symbol").to_list() == ["A", "D"]


def test_index_members_all_filtered_out_is_unknown() -> None:
    with pytest.raises(SnapshotUnavailableError, match="为空"):
        members(FakeSession(index_frame([("E", date(2020, 1, 1), date(2023, 1, 1))])))
