"""行情数据的黄金数据集，第二层：本地录制数据（#44，说明见 docs/subsystems/data.md“黄金数据集”）。

带 ``local_data`` 标记，默认不运行，CI 不运行；运行：``uv run pytest -m local_data``。
读取 ``.local-data/`` 下 xtdata 插件录制的真实数据（目录可用环境变量 DUOHELM_LOCAL_DATA 指定）。
录制数据不存在时跳过：这些测试只在本机运行，不适用“CI 上依赖缺失必须失败”。

只断言公开可查的事实。录制数据不进仓库，也不把它的内容复制进测试。
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.local_data

SHANGHAI = timezone(timedelta(hours=8))


def _local_data_dir() -> Path:
    configured = os.environ.get("DUOHELM_LOCAL_DATA")
    return Path(configured) if configured else Path(__file__).resolve().parents[3] / ".local-data"


@pytest.fixture(scope="module")
def xtdata() -> dict[str, Any]:
    """xtdata 的录制：按调用记录的响应，键是 JSON 编码的调用参数。"""
    files = sorted((_local_data_dir() / "xtdata").glob("xtdata-*.json.gz"))
    if not files:
        pytest.skip(f"没有 xtdata 的录制数据（找过 {_local_data_dir() / 'xtdata'}）")
    with gzip.open(files[-1], "rt", encoding="utf-8") as f:
        return json.load(f)["calls"]


def shanghai_day(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, UTC).astimezone(SHANGHAI).date()


def call(calls: dict[str, Any], *key: str | None) -> Any:
    try:
        return calls[json.dumps(list(key))]
    except KeyError:
        pytest.skip(f"录制数据里没有这次调用：{key}")


# 公开可查：上市日
LIST_DATES = {
    "300750.SZ": date(2018, 6, 11),  # 宁德时代
    "600000.SH": date(1999, 11, 10),  # 浦发银行
    "600519.SH": date(2001, 8, 27),  # 贵州茅台
    "000001.SZ": date(1991, 4, 3),  # 平安银行
}


@pytest.mark.parametrize("symbol", sorted(LIST_DATES))
def test_listing_dates(xtdata: dict[str, Any], symbol: str) -> None:
    info = call(xtdata, "instrument", symbol)
    open_date = info["open_date"]  # 形如 20180611
    assert date(int(open_date[:4]), int(open_date[4:6]), int(open_date[6:])) == LIST_DATES[symbol]


def test_first_bar_of_new_listing_is_listing_day(xtdata: dict[str, Any]) -> None:
    """宁德时代的首根日线在上市日：日期由毫秒时间戳按 Asia/Shanghai 换算，与本机时区无关。"""
    bars = call(xtdata, "daily", "300750.SZ", "20180527")
    assert shanghai_day(bars[0]["time_ms"]) == date(2018, 6, 11)
    assert (
        shanghai_day(bars[0]["time_ms"]).weekday() == 0
    )  # 2018-06-11 是星期一，不是 06-10（星期日）


def test_calendar_starts_on_sse_first_trading_day(xtdata: dict[str, Any]) -> None:
    """上证指数的第一个交易日是 1990-12-19，日历从这天开始。"""
    dates = call(xtdata, "trading_dates", "SH")
    assert shanghai_day(dates[0]) == date(1990, 12, 19)


def _cash_dividend(xtdata: dict[str, Any], symbol: str, day: date) -> list[float]:
    rows = call(xtdata, "divid", symbol)
    return [r["interest"] for r in rows if shanghai_day(r["time_ms"]) == day]


def test_public_cash_dividends(xtdata: dict[str, Any]) -> None:
    """公开分红：茅台 2024-06-19 每股派 30.876 元，平安银行 2024-06-14 每股派 0.719 元（税前）。"""
    assert _cash_dividend(xtdata, "600519.SH", date(2024, 6, 19)) == pytest.approx([30.876])
    assert _cash_dividend(xtdata, "000001.SZ", date(2024, 6, 14)) == pytest.approx([0.719])
