"""行情读取：核心库 MarketReader 和 /api/data 接口（#46）。

构造的数据（2024 年 1 月，CN 日历 14 个交易日：2、3、4、5、8、9、10、11、12、15、16、17、
18、19 日；1 月 6 日是周六，不在日历上）：

- 600000.SH 浦发银行：除 9、10 日（停牌）外每个交易日都有日线，close = 日期的日号；
  11 日除权，因子 2.0
- 000001.SZ ST平安：8 日上市，之后每个交易日都有日线
- 600001.SH 退市股：2、3、5、8 日有日线（4 日缺），12 日退市
- 000300.SH 沪深300（指数）：每个交易日有日线，另有一根 6 日（周六）的日线
- 920000.BJ：在证券列表里，没有任何日线

快照 1 的 600000.SH 只有 11 日一个因子；快照 2 再加 17 日的因子 3.0 和一只新股。
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from duohelm.settings import Settings
from duohelm.storage import DataStore
from duohelm.storage.reader import (
    DataNotFoundError,
    DataReadError,
    InvalidQueryError,
    MarketReader,
)
from duohelm_sdk.schema import ADJ_FACTORS_SCHEMA, BARS_SCHEMA, SECURITIES_SCHEMA

LOCAL = "http://127.0.0.1:8765"
CAL_DAYS = [2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16, 17, 18, 19]
MISSING_ID = "a" * 64


def d(day: int) -> date:
    return date(2024, 1, day)


def bar_rows(symbol: str, days: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "date": d(day),
            "symbol": symbol,
            "open": float(day),
            "high": day + 1.0,
            "low": day - 1.0,
            "close": float(day),
            "volume": 100.0,
            "amount": 1000.0,
        }
        for day in days
    ]


def bars_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=BARS_SCHEMA)


def securities_frame(extra: list[dict[str, Any]] | None = None) -> pl.DataFrame:
    def sec(
        symbol: str,
        name: str,
        exchange: str,
        *,
        kind: str = "stock",
        list_date: date | None = None,
        delist_date: date | None = None,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "name": name,
            "exchange": exchange,
            "kind": kind,
            "list_date": list_date,
            "delist_date": delist_date,
        }

    rows = [
        sec("600000.SH", "浦发银行", "SH", list_date=date(1999, 11, 10)),
        sec("000001.SZ", "ST平安", "SZ", list_date=d(8)),
        sec("600001.SH", "退市股", "SH", list_date=date(1999, 1, 1), delist_date=d(12)),
        sec("000300.SH", "沪深300", "SH", kind="index"),
        sec("920000.BJ", "北交所股票", "BJ"),
        *(extra or []),
    ]
    return pl.DataFrame(rows, schema=SECURITIES_SCHEMA)


def factors_frame(rows: list[tuple[int, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        [{"date": d(day), "symbol": "600000.SH", "factor": f} for day, f in rows],
        schema=ADJ_FACTORS_SCHEMA,
    )


def calendar_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {"market": ["CN"] * len(CAL_DAYS), "date": [d(day) for day in CAL_DAYS]},
        schema={"market": pl.String, "date": pl.Date},
    )


class Built:
    def __init__(self, store: DataStore, snap1: str, snap2: str) -> None:
        self.store = store
        self.snap1 = snap1
        self.snap2 = snap2


def build(store: DataStore) -> Built:
    with store.transaction() as tx:
        tx.write("calendar", calendar_frame())
        tx.write("securities", securities_frame())
        tx.write(
            "bars_1d",
            bars_frame(
                bar_rows("600000.SH", [2, 3, 4, 5, 8, 11, 12, 15, 16, 17, 18, 19])
                + bar_rows("000001.SZ", [8, 9, 10, 11, 12, 15, 16, 17, 18, 19])
                + bar_rows("600001.SH", [2, 3, 5, 8])
                + bar_rows("000300.SH", [6, *CAL_DAYS])
            ),
        )
        tx.write("adj_factors", factors_frame([(11, 2.0)]))
    snap1 = tx.snapshot_id
    assert snap1 is not None
    with store.transaction() as tx:
        tx.write("adj_factors", factors_frame([(17, 3.0)]))
        tx.write(
            "securities",
            securities_frame(
                [
                    {
                        "symbol": "688001.SH",
                        "name": "新股",
                        "exchange": "SH",
                        "kind": "stock",
                        "list_date": d(18),
                        "delist_date": None,
                    }
                ]
            ),
        )
    snap2 = tx.snapshot_id
    assert snap2 is not None and snap2 != snap1
    return Built(store, snap1, snap2)


@pytest.fixture
def home(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path)


@pytest.fixture
def built(home: Settings) -> Built:
    return build(DataStore(home.data_dir))


@pytest.fixture
def reader(built: Built) -> MarketReader:
    return MarketReader(built.store)


Get = Callable[..., Any]


@pytest.fixture
def get_empty(home: Settings) -> Get:
    # 本地缺少依赖时跳过；在 CI 上必须失败，不能静默跳过
    for module in ("fastapi", "httpx"):
        if os.environ.get("CI"):
            importlib.import_module(module)
        else:
            pytest.importorskip(module)
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from duohelm.api import create_app  # noqa: PLC0415

    client = TestClient(create_app(home), base_url=LOCAL, headers={"Origin": LOCAL})

    def call(path: str, **params: Any) -> Any:
        return client.get(path, params={k: v for k, v in params.items() if v is not None})

    return call


@pytest.fixture
def get(get_empty: Get, built: Built) -> Get:
    """已经写好两个快照的数据目录。"""
    return get_empty


def closes(body: dict[str, Any]) -> dict[int, float]:
    return {int(b["date"][-2:]): b["close"] for b in body["bars"]}


# ---- 快照 ----


def test_snapshots_list_and_current(get: Get, built: Built) -> None:
    body = get("/api/data/snapshots").json()
    assert body["current"] == built.snap2
    assert [s["id"] for s in body["snapshots"]] == [built.snap2, built.snap1]
    assert [s["is_current"] for s in body["snapshots"]] == [True, False]
    current = get("/api/data/snapshots/current").json()
    assert current["id"] == built.snap2
    assert current["parent"] == built.snap1


def test_no_snapshot_yet_is_404_not_empty(get_empty: Get) -> None:
    assert get_empty("/api/data/snapshots").json() == {"current": None, "snapshots": []}
    for path in (
        "/api/data/snapshots/current",
        "/api/data/securities",
        "/api/data/securities/600000.SH/bars",
        "/api/data/coverage",
    ):
        r = get_empty(path)
        assert r.status_code == 404, path
        assert "快照" in r.json()["detail"]


def test_unknown_or_malformed_snapshot(get: Get, built: Built) -> None:
    r = get("/api/data/securities", snapshot=MISSING_ID)
    assert r.status_code == 404
    assert MISSING_ID in r.json()["detail"]
    for bad in ("xyz", "../manifests/x", built.snap1.upper()):
        assert get("/api/data/securities", snapshot=bad).status_code == 422, bad


def test_reading_does_not_change_data(get: Get, built: Built) -> None:
    before = sorted(p.name for p in built.store.root.rglob("*") if p.is_file())
    head = built.store.head()
    for path in ("/api/data/snapshots", "/api/data/securities", "/api/data/coverage"):
        assert get(path).status_code == 200
    assert get("/api/data/securities/600000.SH/bars", adjust="qfq").status_code == 200
    assert sorted(p.name for p in built.store.root.rglob("*") if p.is_file()) == before
    assert built.store.head() == head


# ---- 证券 ----


def test_securities_filters_and_paging(get: Get, built: Built) -> None:
    body = get("/api/data/securities").json()
    assert body["snapshot"] == built.snap2
    assert body["total"] == 6
    assert [s["symbol"] for s in body["items"]] == [
        "000001.SZ",
        "000300.SH",
        "600000.SH",
        "600001.SH",
        "688001.SH",
        "920000.BJ",
    ]
    assert get("/api/data/securities", symbol="600000.SH").json()["items"][0]["name"] == "浦发银行"
    assert [s["symbol"] for s in get("/api/data/securities", name_prefix="ST").json()["items"]] == [
        "000001.SZ"
    ]
    # 前缀按字面匹配，不是正则
    assert get("/api/data/securities", name_prefix=".*").json()["total"] == 0
    assert get("/api/data/securities", kind="index").json()["total"] == 1
    page = get("/api/data/securities", limit=2, offset=2).json()
    assert page["total"] == 6
    assert [s["symbol"] for s in page["items"]] == ["600000.SH", "600001.SH"]
    # 筛选没有匹配：200 + total 0，不是 404
    none = get("/api/data/securities", symbol="999999.SH")
    assert none.status_code == 200 and none.json()["total"] == 0
    assert get("/api/data/securities", limit=0).status_code == 422
    assert get("/api/data/securities", offset=-1).status_code == 422


def test_securities_of_old_snapshot(get: Get, built: Built) -> None:
    old = get("/api/data/securities", snapshot=built.snap1).json()
    assert old["snapshot"] == built.snap1
    assert old["total"] == 5
    assert "688001.SH" not in [s["symbol"] for s in old["items"]]


def test_missing_securities_dataset_is_404(home: Settings, get_empty: Get) -> None:
    store = DataStore(home.data_dir)
    with store.transaction() as tx:
        tx.write("calendar", calendar_frame())
    r = get_empty("/api/data/securities")
    assert r.status_code == 404
    assert "securities" in r.json()["detail"]


# ---- 日线 ----


def test_bars_unadjusted_range(get: Get, built: Built) -> None:
    body = get(
        "/api/data/securities/600000.SH/bars", start="2024-01-05", end="2024-01-12", adjust="none"
    ).json()
    assert body["snapshot"] == built.snap2
    assert body["empty"] is False
    assert body["adjust"] == "none"
    assert body["base_date"] is None
    assert body["last_bar_date"] == "2024-01-19"
    assert closes(body) == {5: 5.0, 8: 8.0, 11: 11.0, 12: 12.0}  # 9、10 日停牌，没有行
    assert body["bars"][0] == {
        "date": "2024-01-05",
        "open": 5.0,
        "high": 6.0,
        "low": 4.0,
        "close": 5.0,
        "volume": 100.0,
        "amount": 1000.0,
    }


def test_bars_unknown_symbol_is_404(get: Get) -> None:
    r = get("/api/data/securities/999999.SH/bars")
    assert r.status_code == 404
    assert "999999.SH" in r.json()["detail"]


def test_bars_empty_range_is_marked_not_404(get: Get) -> None:
    after = get("/api/data/securities/600000.SH/bars", start="2024-02-01").json()
    assert after["bars"] == [] and after["empty"] is True
    assert after["last_bar_date"] == "2024-01-19"
    suspended = get("/api/data/securities/600000.SH/bars", start="2024-01-09", end="2024-01-10")
    assert suspended.status_code == 200 and suspended.json()["empty"] is True
    never = get("/api/data/securities/920000.BJ/bars").json()
    assert never["empty"] is True and never["last_bar_date"] is None
    # 空区间的前复权：没有传入基准日就没有基准日
    qfq = get("/api/data/securities/600000.SH/bars", start="2024-02-01", adjust="qfq").json()
    assert qfq["empty"] is True and qfq["base_date"] is None


def test_bars_delisted_security_keeps_history(get: Get) -> None:
    body = get("/api/data/securities/600001.SH/bars").json()
    assert closes(body) == {2: 2.0, 3: 3.0, 5: 5.0, 8: 8.0}
    assert body["last_bar_date"] == "2024-01-08"


def test_bars_hfq(get: Get, built: Built) -> None:
    body = get("/api/data/securities/600000.SH/bars", snapshot=built.snap1, adjust="hfq").json()
    c = closes(body)
    assert c[2] == 2.0 and c[8] == 8.0  # 除权前因子为 1
    assert c[11] == 22.0 and c[19] == 38.0  # 11 日起因子 2.0
    assert body["bars"][0]["volume"] == 100.0  # 成交量不调整
    body2 = get("/api/data/securities/600000.SH/bars", adjust="hfq").json()
    c2 = closes(body2)
    assert c2[16] == 32.0 and c2[17] == 51.0  # 快照 2：17 日起因子 3.0


def test_bars_qfq_default_base_is_last_day_of_range(get: Get, built: Built) -> None:
    body = get(
        "/api/data/securities/600000.SH/bars", snapshot=built.snap1, adjust="qfq", end="2024-01-12"
    ).json()
    assert body["base_date"] == "2024-01-12"
    c = closes(body)
    assert c[2] == 1.0 and c[8] == 4.0  # 因子 1 / 基准日因子 2
    assert c[11] == 11.0 and c[12] == 12.0
    # 不传 end：基准日是返回的最后一根日线（19 日）
    full = get("/api/data/securities/600000.SH/bars", snapshot=built.snap1, adjust="qfq").json()
    assert full["base_date"] == "2024-01-19"
    assert closes(full)[19] == 19.0


def test_bars_qfq_explicit_base_before_range_start(get: Get, built: Built) -> None:
    body = get(
        "/api/data/securities/600000.SH/bars",
        snapshot=built.snap1,
        adjust="qfq",
        start="2024-01-11",
        end="2024-01-19",
        base_date="2024-01-05",
    ).json()
    assert body["base_date"] == "2024-01-05"
    assert closes(body)[11] == 22.0  # 11 * 2.0 / 1.0


def test_bars_qfq_unaffected_by_factors_after_base(get: Get, built: Built) -> None:
    """未来函数红线：基准日之后的因子变化不改变基准日及之前的前复权价格。"""
    for end in ("2024-01-12", "2024-01-16"):
        old = get(
            "/api/data/securities/600000.SH/bars",
            snapshot=built.snap1,
            adjust="qfq",
            end=end,
        ).json()
        new = get(
            "/api/data/securities/600000.SH/bars",
            snapshot=built.snap2,  # 比快照 1 多了 17 日的因子 3.0
            adjust="qfq",
            end=end,
        ).json()
        assert old["bars"] == new["bars"], end
        assert old["base_date"] == new["base_date"] == end
    # 固定基准日读到更晚的区间：基准日及之前的价格仍不变，之后的价格才用到新因子
    old = get(
        "/api/data/securities/600000.SH/bars",
        snapshot=built.snap1,
        adjust="qfq",
        end="2024-01-19",
        base_date="2024-01-12",
    ).json()
    new = get(
        "/api/data/securities/600000.SH/bars",
        snapshot=built.snap2,
        adjust="qfq",
        end="2024-01-19",
        base_date="2024-01-12",
    ).json()
    assert {k: v for k, v in closes(old).items() if k <= 12} == {
        k: v for k, v in closes(new).items() if k <= 12
    }
    assert closes(old)[19] == 19.0 and closes(new)[19] == 28.5  # 19 * 3.0 / 2.0
    # 默认基准日是 19 日，历史价格随之改变：前复权的性质，不是未来函数
    default = get("/api/data/securities/600000.SH/bars", adjust="qfq").json()
    assert closes(default)[12] == pytest.approx(8.0)  # 12 * 2.0 / 3.0


def test_bars_qfq_base_date_bounds(get: Get) -> None:
    path = "/api/data/securities/600000.SH/bars"
    later_than_end = get(path, adjust="qfq", end="2024-01-12", base_date="2024-01-15")
    assert later_than_end.status_code == 422
    assert "区间" in later_than_end.json()["detail"]
    later_than_last_bar = get(path, adjust="qfq", base_date="2024-01-22")
    assert later_than_last_bar.status_code == 422
    assert "最后一个日线日期" in later_than_last_bar.json()["detail"]
    # end 超过最后一根日线时，基准日仍不能超过最后一根日线
    assert get(path, adjust="qfq", end="2024-02-29", base_date="2024-01-22").status_code == 422
    assert get(path, adjust="none", base_date="2024-01-12").status_code == 422
    assert get(path, adjust="hfq", base_date="2024-01-12").status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2024-1-5"},
        {"start": "20240105"},
        {"start": "1704412800"},
        {"end": "2024-01-05T00:00:00"},
        {"end": "2024-02-30"},
        {"base_date": "abc"},
        {"start": "2024-01-12", "end": "2024-01-05"},
        {"adjust": "foo"},
    ],
)
def test_bars_invalid_params_are_422(get: Get, params: dict[str, str]) -> None:
    assert get("/api/data/securities/600000.SH/bars", **params).status_code == 422


def test_bars_without_adj_factors_dataset(home: Settings, get_empty: Get) -> None:
    store = DataStore(home.data_dir)
    with store.transaction() as tx:
        tx.write("securities", securities_frame())
        tx.write("bars_1d", bars_frame(bar_rows("600000.SH", [2, 3])))
    assert get_empty("/api/data/securities/600000.SH/bars").status_code == 200
    for mode in ("hfq", "qfq"):
        r = get_empty("/api/data/securities/600000.SH/bars", adjust=mode)
        assert r.status_code == 404, mode
        assert "adj_factors" in r.json()["detail"]
    # 数据集存在、但这只证券没有因子行：没有除权事件，因子为 1
    with store.transaction() as tx:
        tx.write("adj_factors", factors_frame([(11, 2.0)]))
    r = get_empty("/api/data/securities/000001.SZ/bars", adjust="hfq")
    assert r.status_code == 200 and r.json()["empty"] is True
    with store.transaction() as tx:
        tx.write("bars_1d", bars_frame(bar_rows("000001.SZ", [8, 9])))
    assert closes(get_empty("/api/data/securities/000001.SZ/bars", adjust="hfq").json()) == {
        8: 8.0,
        9: 9.0,
    }


# ---- 覆盖统计 ----


def test_coverage_missing_days(reader: MarketReader, built: Built) -> None:
    page = reader.coverage(snapshot=built.snap1)
    assert page.total == 5
    rows = {r["symbol"]: r for r in page.items.to_dicts()}
    assert list(rows) == ["000001.SZ", "000300.SH", "600000.SH", "600001.SH", "920000.BJ"]
    assert rows["600000.SH"] == {
        "symbol": "600000.SH",
        "name": "浦发银行",
        "kind": "stock",
        "exchange": "SH",
        "delist_date": None,
        "first_date": d(2),
        "last_date": d(19),
        "bar_count": 12,
        "expected_days": 14,
        "missing_days": 2,  # 9、10 日停牌
        "off_calendar_bars": 0,
    }
    # 8 日上市：首末之间 10 个交易日，全都有
    assert rows["000001.SZ"]["first_date"] == d(8)
    assert (
        rows["000001.SZ"]["bar_count"],
        rows["000001.SZ"]["expected_days"],
        rows["000001.SZ"]["missing_days"],
    ) == (10, 10, 0)
    # 退市股：2、3、5、8 日，首末之间 2、3、4、5、8 共 5 天，缺 4 日；12 日退市不计入
    delisted = rows["600001.SH"]
    assert (delisted["bar_count"], delisted["expected_days"], delisted["missing_days"]) == (4, 5, 1)
    assert delisted["last_date"] == d(8)
    # 指数多一根周六的日线：不在日历上，不抵消缺失
    index = rows["000300.SH"]
    assert (index["bar_count"], index["expected_days"], index["missing_days"]) == (15, 14, 0)
    assert index["off_calendar_bars"] == 1
    # 没有任何日线：根数为 0，其余为 null，不是“没有这只证券”
    never = rows["920000.BJ"]
    assert never["bar_count"] == 0
    assert never["first_date"] is None and never["expected_days"] is None
    assert never["missing_days"] is None


def test_coverage_api_paging_kind_and_snapshot(get: Get, built: Built) -> None:
    body = get("/api/data/coverage").json()
    assert body["snapshot"] == built.snap2 and body["total"] == 6
    new = next(i for i in body["items"] if i["symbol"] == "688001.SH")
    assert new["bar_count"] == 0 and new["missing_days"] is None
    old = get("/api/data/coverage", snapshot=built.snap1).json()
    assert old["total"] == 5
    page = get("/api/data/coverage", limit=2, offset=1).json()
    assert [i["symbol"] for i in page["items"]] == ["000300.SH", "600000.SH"]
    idx = get("/api/data/coverage", kind="index").json()
    assert idx["total"] == 1 and idx["items"][0]["symbol"] == "000300.SH"
    sh = next(i for i in body["items"] if i["symbol"] == "600000.SH")
    assert sh["missing_days"] == 2 and sh["expected_days"] == 14


def test_coverage_rows_have_exchange_and_delist_date(reader: MarketReader, built: Built) -> None:
    rows = {r["symbol"]: r for r in reader.coverage(snapshot=built.snap1).items.to_dicts()}
    assert [(rows[s]["exchange"], rows[s]["delist_date"]) for s in rows] == [
        ("SZ", None),
        ("SH", None),
        ("SH", None),
        ("SH", d(12)),  # 600001.SH 退市股
        ("BJ", None),
    ]


def add_unlisted_symbol_bars(store: DataStore) -> str:
    """日线里有、证券列表里没有的代码。"""
    with store.transaction() as tx:
        tx.write("bars_1d", bars_frame(bar_rows("999999.SH", [2, 3, 5])))
    assert tx.snapshot_id is not None
    return tx.snapshot_id


def test_coverage_symbol_not_in_securities_is_unknown(reader: MarketReader, built: Built) -> None:
    snap = add_unlisted_symbol_bars(built.store)
    rows = {r["symbol"]: r for r in reader.coverage(snapshot=snap).items.to_dicts()}
    ghost = rows["999999.SH"]
    # 未知就是 null，不按代码前缀猜成 SH
    assert ghost["exchange"] is None and ghost["delist_date"] is None and ghost["name"] is None
    assert ghost["bar_count"] == 3
    summary = reader.coverage_summary(snapshot=snap, today=d(20))
    unknown = [g for g in summary.groups.to_dicts() if g["status"] is None]
    assert len(unknown) == 1
    assert (unknown[0]["exchange"], unknown[0]["kind"], unknown[0]["securities"]) == (None, None, 1)
    assert summary.total == 7  # 当前快照的 6 只加上这个代码


def test_coverage_summary_groups(reader: MarketReader, built: Built) -> None:
    summary = reader.coverage_summary(snapshot=built.snap1, today=d(20))
    assert (summary.snapshot, summary.as_of, summary.total) == (built.snap1, d(20), 5)
    groups = {(g["exchange"], g["kind"], g["status"]): g for g in summary.groups.to_dicts()}
    assert list(groups) == [  # 按交易所、种类、状态排序
        ("BJ", "stock", "listed"),
        ("SH", "index", "listed"),
        ("SH", "stock", "delisted"),
        ("SH", "stock", "listed"),
        ("SZ", "stock", "listed"),
    ]
    assert groups[("SH", "stock", "delisted")] == {
        "exchange": "SH",
        "kind": "stock",
        "status": "delisted",
        "securities": 1,
        "with_bars": 1,
        "no_bars": 0,
        "bar_count": 4,
        "with_missing": 1,
        "missing_days": 1,
        "off_calendar_bars": 0,
    }
    sh = groups[("SH", "stock", "listed")]
    assert (sh["securities"], sh["bar_count"], sh["missing_days"]) == (1, 12, 2)
    # 没有任何日线的北交所股票：算作没有日线，不计缺失
    bj = groups[("BJ", "stock", "listed")]
    assert (bj["with_bars"], bj["no_bars"], bj["with_missing"], bj["missing_days"]) == (0, 1, 0, 0)
    assert groups[("SH", "index", "listed")]["off_calendar_bars"] == 1


def test_coverage_summary_status_depends_on_as_of(reader: MarketReader, built: Built) -> None:
    """退市日 1 月 12 日：当天及之后算退市，之前仍在市。"""

    def status_at(today: date) -> str | None:
        groups = reader.coverage_summary(snapshot=built.snap1, today=today).groups
        sh = groups.filter((pl.col("exchange") == "SH") & (pl.col("kind") == "stock"))
        return ",".join(sorted(sh.get_column("status").to_list()))

    assert status_at(d(11)) == "listed"
    assert status_at(d(12)) == "delisted,listed"


def test_coverage_summary_api(get: Get, built: Built) -> None:
    body = get("/api/data/coverage/summary").json()
    assert body["snapshot"] == built.snap2 and body["total"] == 6
    assert sum(g["securities"] for g in body["groups"]) == 6
    assert {g["exchange"] for g in body["groups"]} == {"BJ", "SH", "SZ"}
    assert sum(g["securities"] for g in body["groups"] if g["status"] == "delisted") == 1
    old = get("/api/data/coverage/summary", snapshot=built.snap1).json()
    assert old["total"] == 5
    assert get("/api/data/coverage/summary", snapshot="xyz").status_code == 422


def test_coverage_summary_needs_snapshot(get_empty: Get) -> None:
    r = get_empty("/api/data/coverage/summary")
    assert r.status_code == 404 and "快照" in r.json()["detail"]


def break_securities(built: Built) -> None:
    for path in (built.store.files_dir / "securities").rglob("*.parquet"):
        path.unlink()


def test_securities_read_failure_is_an_error_not_unknown(
    reader: MarketReader, get: Get, built: Built
) -> None:
    break_securities(built)
    with pytest.raises(DataReadError, match="证券列表"):
        reader.coverage()
    for path in ("/api/data/coverage", "/api/data/coverage/summary"):
        r = get(path)
        assert r.status_code == 500 and "证券列表" in r.json()["detail"], path


def test_failed_scan_is_not_cached(reader: MarketReader, built: Built) -> None:
    files = {p: p.read_bytes() for p in (built.store.files_dir / "securities").rglob("*.parquet")}
    break_securities(built)
    with pytest.raises(DataReadError):
        reader.coverage()
    for p, content in files.items():
        p.write_bytes(content)
    assert reader.coverage().total == 6


def test_coverage_scans_bars_once_per_snapshot(
    reader: MarketReader, built: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    scans: list[str | None] = []
    real_read = built.store.read

    def counting(dataset: str, **kw: Any) -> pl.DataFrame:
        if dataset == "bars_1d":
            scans.append(kw.get("snapshot"))
        return real_read(dataset, **kw)

    monkeypatch.setattr(built.store, "read", counting)
    reader.coverage(snapshot=built.snap1, limit=2)
    reader.coverage(snapshot=built.snap1, limit=2, offset=2)
    reader.coverage(snapshot=built.snap1, kind="index")
    reader.coverage_summary(snapshot=built.snap1)
    assert scans == [built.snap1]
    reader.coverage_summary()  # 另一个快照：再扫一次
    assert scans == [built.snap1, built.snap2]


def test_coverage_needs_calendar(home: Settings, get_empty: Get) -> None:
    store = DataStore(home.data_dir)
    with store.transaction() as tx:
        tx.write("securities", securities_frame())
        tx.write("bars_1d", bars_frame(bar_rows("600000.SH", [2, 3])))
    r = get_empty("/api/data/coverage")
    assert r.status_code == 404
    assert "calendar" in r.json()["detail"]


def test_coverage_calendar_of_other_market_is_not_cn(home: Settings, get_empty: Get) -> None:
    store = DataStore(home.data_dir)
    with store.transaction() as tx:
        tx.write("securities", securities_frame())
        tx.write("bars_1d", bars_frame(bar_rows("600000.SH", [2, 3])))
        tx.write(
            "calendar",
            pl.DataFrame(
                {"market": ["HK"], "date": [d(2)]}, schema={"market": pl.String, "date": pl.Date}
            ),
        )
    r = get_empty("/api/data/coverage")
    assert r.status_code == 404
    assert "CN" in r.json()["detail"]


# ---- 核心库 ----


def test_reader_errors(reader: MarketReader, built: Built) -> None:
    with pytest.raises(DataNotFoundError):
        reader.bars("999999.SH")
    with pytest.raises(DataNotFoundError):
        reader.securities(snapshot=MISSING_ID)
    with pytest.raises(InvalidQueryError):
        reader.securities(snapshot="../x")
    with pytest.raises(InvalidQueryError):
        reader.bars("600000.SH", start=d(5), end=d(2))
    with pytest.raises(InvalidQueryError):
        reader.coverage(limit=0)


def test_null_fields_do_not_cause_500(home: Settings, get_empty: Get) -> None:
    """数据标准只要求 date、symbol 非空；名称、价格为空的行读出来是 null，不是 500。"""
    store = DataStore(home.data_dir)
    sec = securities_frame().filter(pl.col("symbol") == "600000.SH")
    sec = sec.with_columns(
        pl.lit(None, dtype=pl.String).alias("name"), pl.lit(None, dtype=pl.String).alias("kind")
    )
    rows = bar_rows("600000.SH", [2, 3])
    rows[0]["close"] = None
    rows[0]["volume"] = None
    with store.transaction() as tx:
        tx.write("securities", sec)
        tx.write("bars_1d", bars_frame(rows))
        tx.write("adj_factors", factors_frame([(11, 2.0)]))
    items = get_empty("/api/data/securities").json()["items"]
    assert items[0]["name"] is None and items[0]["kind"] is None
    for mode in ("none", "hfq", "qfq"):
        r = get_empty("/api/data/securities/600000.SH/bars", adjust=mode)
        assert r.status_code == 200, mode
        first = r.json()["bars"][0]
        assert first["close"] is None and first["volume"] is None
        assert first["open"] == 2.0
