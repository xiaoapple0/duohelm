from __future__ import annotations

import importlib
import os
from collections.abc import Sequence
from datetime import date

import pytest

# 本地缺少 polars 时跳过；在 CI 上必须失败，不能静默跳过。
pl = importlib.import_module("polars") if os.environ.get("CI") else pytest.importorskip("polars")

from duohelm_sdk import Capabilities, DataSourcePlugin, Freq
from duohelm_sdk.checks import check_securities
from duohelm_sdk.schema import SECURITIES_SCHEMA


def securities(rows):
    return pl.DataFrame(rows, schema=SECURITIES_SCHEMA, orient="row")


GOOD = [
    ("600000.SH", "浦发银行", "SH", "stock", date(1999, 11, 10), None),
    ("000001.SZ", "平安银行", "SZ", "stock", date(1991, 4, 3), None),
    ("920000.BJ", "安徽凤凰", "BJ", "stock", None, None),
    ("600087.SH", "退市长油", "SH", "stock", date(1997, 6, 12), date(2014, 6, 4)),
    ("000001.SH", "上证指数", "SH", "index", date(1990, 12, 19), None),
]


def test_valid_securities() -> None:
    result = check_securities(securities(GOOD))
    assert result.passed, result.details


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (("600000.SZ", "x", "SH", "stock", None, None), "后缀"),
        (("600000.HK", "x", "HK", "stock", None, None), "SH / SZ / BJ"),
        (("60000.SH", "x", "SH", "stock", None, None), "6 位"),
        (("600000.SH", "x", "SH", "fund", None, None), "stock / index"),
        (("600000.SH", "x", "SH", "stock", date(2020, 1, 2), date(2019, 1, 2)), "delist_date"),
    ],
)
def test_invalid_securities(row, message: str) -> None:
    result = check_securities(securities([row]))
    assert not result.passed
    assert any(message in d for d in result.details), result.details


def test_duplicate_symbol() -> None:
    result = check_securities(securities([GOOD[0], GOOD[0]]))
    assert not result.passed


class _Source:
    def capabilities(self) -> Capabilities:
        return Capabilities(markets=("CN",), freqs=(Freq.DAY,), fields=("open",))

    def securities(self):
        return securities(GOOD)

    def lookup_securities(self, symbols: Sequence[str]):
        return securities([r for r in GOOD if r[0] in symbols])

    def trading_calendar(self, market, start, end):
        return pl.DataFrame({"date": []}, schema={"date": pl.Date})

    def bars(self, symbols, freq, start, end):
        raise NotImplementedError

    def adj_factors(self, symbols, start, end):
        raise NotImplementedError

    def index_constituents(self, index, start, end):
        raise NotImplementedError


class _OldSource(_Source):
    """SDK 0.1 的行情源：没有 securities / lookup_securities。"""

    securities = None  # type: ignore[assignment]
    lookup_securities = None  # type: ignore[assignment]


def test_protocol_requires_security_methods() -> None:
    assert isinstance(_Source(), DataSourcePlugin)
    assert not isinstance(_OldSource(), DataSourcePlugin)
