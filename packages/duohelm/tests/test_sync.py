"""data.sync：从任务入口跑到隔离进程里的行情源插件，再到快照（#24）。"""

from __future__ import annotations

import json
import sqlite3
import textwrap
import threading
import time
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl
import pydantic
import pytest

from duohelm.endpoints import EndpointStore
from duohelm.isolation import Limits, PluginRunError
from duohelm.registry import PluginRegistry
from duohelm.settings import Settings
from duohelm.storage import DataStore, SnapshotConflictError
from duohelm.storage import catalog as catalog_module
from duohelm.tasks import RetryableError, TaskStore, TaskTypeRegistry, Worker, delisted
from duohelm.tasks.sync import SyncParams, SyncResult, data_sync, last_closed_day
from duohelm.tasks.sync import _plugin_errors as plugin_errors  # pyright: ignore[reportPrivateUsage]

FAST = Limits(call_timeout_s=60, startup_timeout_s=60, poll_interval_s=0.1)
START, END = date(2024, 1, 2), date(2024, 1, 5)

MANIFEST = """
[plugin]
id = "fake-src"
name = "fake-src"
version = "1.0.0"
kind = "datasource"
sdk = ">=0.4,<0.5"
entrypoint = "fake_src:source"

[[permissions.endpoints]]
id = "src"
description = "测试行情服务"
default = "127.0.0.1:1"
scope = "loopback"
"""

PLUGIN_CODE = """
import json
import time
from datetime import date
from pathlib import Path

import polars as pl
from duohelm_sdk import Capabilities, ConnectionStatus, EndpointNotConfiguredError, Freq, endpoint
from duohelm_sdk.schema import ADJ_FACTORS_SCHEMA, BARS_SCHEMA, SECURITIES_SCHEMA

HERE = Path(__file__).parent
B = json.loads((HERE / "behavior.json").read_text(encoding="utf-8"))
CALENDAR = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
SECURITIES = [
    ("000001.SH", "上证指数", "SH", "index", date(1990, 12, 19), None),
    ("000001.SZ", "平安银行", "SZ", "stock", date(1991, 4, 3), None),
    ("600000.SH", "浦发银行", "SH", "stock", date(1999, 11, 10), None),
    ("600001.SH", "邯郸钢铁", "SH", "stock", date(1998, 1, 22), date(2009, 12, 25)),
    ("600005.SH", "武钢股份", "SH", "stock", date(1999, 8, 3), date(2024, 1, 3)),
    ("830000.NQ", "新三板", "NQ", "stock", date(2020, 1, 2), None),
]


def all_securities():
    # delist_now：让在市的证券在这次运行里变成已退市
    changed = B.get("delist_now", {})
    names = B.get("names", {})
    absent = B.get("absent", [])  # 这次运行里数据源还不认识的证券（比如还没上市）
    return [
        (row[0], names.get(row[0], row[1]), *row[2:5],
         date.fromisoformat(changed[row[0]]) if row[0] in changed else row[5])
        for row in SECURITIES
        if row[0] not in absent
    ]


def record(name):
    with (HERE / "calls.txt").open("a", encoding="utf-8") as f:
        f.write(name + "\\n")


class Plain:
    def capabilities(self):
        record("capabilities")
        return Capabilities(
            markets=("CN",),
            freqs=(Freq.DAY,),
            fields=("open", "high", "low", "close", "volume", "amount"),
            lists_delisted=B.get("lists_delisted", True),
            exact_delist_date=B.get("exact_delist_date", True),
        )

    def securities(self, *, include_delisted=False):
        record(f"securities include_delisted={include_delisted}")
        rows = all_securities()
        if not include_delisted:
            rows = [r for r in rows if r[5] is None][: B.get("listed_limit")]
        return pl.DataFrame(rows, schema=SECURITIES_SCHEMA, orient="row")

    def lookup_securities(self, symbols):
        record(f"lookup {','.join(symbols)}")
        if B.get("lookup_fail") in symbols:
            raise RuntimeError("lookup 出错")
        lost = set(B.get("lookup_loses", []))
        rows = [r for r in all_securities() if r[0] in symbols and r[0] not in lost]
        if B.get("lookup_extra"):
            rows += [r for r in all_securities() if r[0] == "600000.SH" and r[0] not in symbols]
        return pl.DataFrame(rows, schema=SECURITIES_SCHEMA, orient="row")

    def trading_calendar(self, market, start, end):
        record(f"trading_calendar {market}")
        return pl.DataFrame(
            {"date": [d for d in CALENDAR if start <= d <= end]}, schema={"date": pl.Date}
        )

    def bars(self, symbols, freq, start, end):
        record(f"bars {','.join(symbols)}")
        if B.get("slow_bars"):
            time.sleep(30)
        rows = []
        for s in symbols:
            last = date(2024, 1, 3) if s == "600005.SH" else end
            for d in CALENDAR:
                if start <= d <= last:
                    volume = 0.0 if (s, d) == ("000001.SZ", date(2024, 1, 3)) else 1000.0
                    if B.get("null_volume") and s == "600000.SH" and d == start:
                        volume = None
                    amount = None if volume is None else volume * 10.5
                    rows.append((d, s, 10.0, 11.0, 9.0, 10.5, volume, amount))
        if B.get("bad_bars"):
            rows.append((start, "999999.SH", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0))
        if B.get("late_bars"):
            rows.append((date(2024, 1, 8), symbols[0], 1.0, 1.0, 1.0, 1.0, 1.0, 1.0))
        return pl.DataFrame(rows, schema=BARS_SCHEMA, orient="row")

    def adj_factors(self, symbols, start, end):
        # 和真实插件一样：用全部历史算累计因子，再按 [start, end] 过滤
        record(f"adj_factors {start}")
        rows = [(date(2020, 6, 1), "600000.SH", 1.02), (date(2024, 1, 4), "600000.SH", 1.071)]
        rows = [r for r in rows if r[1] in symbols and start <= r[0] <= end]
        return pl.DataFrame(rows, schema=ADJ_FACTORS_SCHEMA, orient="row")

    def index_constituents(self, index, start, end):
        record(f"index_constituents {index} {start} {end}")
        spec = B.get("index_members")
        if B.get("index_crash"):
            import os
            os._exit(3)
        if index in B.get("index_raise", []):
            raise RuntimeError("boom")
        if spec is None:
            raise NotImplementedError
        schema = {"symbol": pl.String, "in_date": pl.Date, "out_date": pl.Date}
        if B.get("bad_index_format"):
            return pl.DataFrame({"symbol": ["600000.SH"]})
        rows = [
            (s, date.fromisoformat(i) if i else None, date.fromisoformat(o) if o else None)
            for s, i, o in spec.get(index, [])
        ]
        return pl.DataFrame(rows, schema=schema, orient="row")


class Checked(Plain):
    def check_connection(self):
        record("check_connection")
        try:
            address = endpoint("src")
        except EndpointNotConfiguredError as exc:
            return ConnectionStatus(ok=False, message=str(exc))
        record(f"check_connection {address}")
        return ConnectionStatus(ok=True, message=f"已连接 {address}")


class Broken(Plain):
    check_connection = None  # 写错了：属性存在但不可调用


source = Broken() if B.get("broken_check") else Checked() if B.get("check", True) else Plain()
"""


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


def behave(plugin: Path, **behavior: Any) -> None:
    """改变插件下一次运行的行为（每次运行都启动新的子进程，重新读取）。"""
    (plugin / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")
    (plugin / "calls.txt").unlink(missing_ok=True)


def install(settings: Settings, **behavior: Any) -> Path:
    path = settings.plugin_dir / "fake-src"
    path.mkdir(parents=True)
    (path / "duohelm-plugin.toml").write_text(MANIFEST, encoding="utf-8")
    (path / "fake_src.py").write_text(textwrap.dedent(PLUGIN_CODE), encoding="utf-8")
    (path / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")
    return path


def confirm(settings: Settings, address: str = "127.0.0.1:58612") -> None:
    """用户在插件页确认地址（和 API 用的是同一个存储）。"""
    [entry] = PluginRegistry([settings.plugin_dir]).scan()
    assert entry.manifest is not None, entry.problems
    EndpointStore(settings.plugin_config_path).confirm(
        entry.manifest, "src", address, confirmed=True
    )


def calls(plugin: Path) -> list[str]:
    path = plugin / "calls.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def submit(settings: Settings, **params: Any) -> tuple[TaskStore, TaskTypeRegistry, int]:
    store = TaskStore(settings.db_path)
    types = TaskTypeRegistry([data_sync(settings, limits=FAST)])
    body = {"plugin_id": "fake-src", "start": START.isoformat(), "end": END.isoformat(), **params}
    task = store.create("data.sync", types.validate_params("data.sync", body), max_attempts=1)
    return store, types, task.id


def run(settings: Settings, **params: Any) -> tuple[TaskStore, int]:
    store, types, task_id = submit(settings, **params)
    assert Worker(store, types).run_once()
    return store, task_id


def result_of(store: TaskStore, task_id: int) -> SyncResult:
    task = store.get(task_id)
    assert task.status == "succeeded", (task.error, [log.message for log in store.logs(task_id)])
    assert task.result is not None
    return SyncResult.model_validate_json(task.result)


def failed(store: TaskStore, task_id: int) -> str:
    task = store.get(task_id)
    assert task.status == "failed", task.status
    assert task.error is not None
    return task.error


# ---- 完整路径 ----


def test_sync_uses_confirmed_address_and_writes_snapshot(settings: Settings) -> None:
    """用户确认地址后，从同步任务的入口跑到插件里，插件拿到的是确认过的地址。"""
    plugin = install(settings)
    confirm(settings, "127.0.0.1:58612")
    store, task_id = run(settings)
    result = result_of(store, task_id)

    assert calls(plugin)[:4] == [
        "check_connection",
        "check_connection 127.0.0.1:58612",  # 插件拿到的是用户确认的地址
        "capabilities",
        "securities include_delisted=True",
    ]
    # 不认识的交易所（NQ）跳过；2009 年退市的不在区间内，不取日线
    assert result.securities == 5
    assert result.symbols == 4
    assert result.dropped_zero_volume == 1
    assert result.snapshot_id is not None
    # 任务日志和结果里不写具体地址
    assert result.connection == "已连接 <src 的地址>"
    logs = " ".join(log.message for log in store.logs(task_id))
    assert "58612" not in logs
    assert "58612" not in (store.get(task_id).result or "")

    data = DataStore(settings.data_dir)
    assert data.head() == result.snapshot_id
    assert data.catalog.snapshot(result.snapshot_id).task_id == task_id
    bars = data.read("bars_1d")
    assert bars.height == 4 * 4 - 2 - 1  # 600005 退市后没有 2 天，平安银行丢弃 1 行成交量为 0
    assert bars.filter(pl.col("volume") == 0).height == 0
    assert data.read("calendar").get_column("date").to_list() == [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
    ]
    assert data.read("adj_factors").rows() == [
        (date(2020, 6, 1), "600000.SH", 1.02),
        (date(2024, 1, 4), "600000.SH", 1.071),
    ]
    securities = data.read("securities")
    assert "830000.NQ" not in securities.get_column("symbol").to_list()
    assert result.written == {
        "securities": 5,
        "delist_date_quality": 5,
        "calendar": 4,
        "bars_1d": 13,
        "adj_factors": 2,
        "st_daily": 0,  # end 早于最近一个已收盘的日子，不写快照
        "index_members_daily": 0,
    }
    assert result.snapshot_date is None and result.snapshot_skipped is not None
    assert data.read("st_daily").height == 0
    quality = dict(data.read("delist_date_quality").rows())
    assert quality == {
        "000001.SH": None,
        "000001.SZ": None,
        "600000.SH": None,
        "600001.SH": True,
        "600005.SH": True,
    }


def test_approximate_delist_dates_are_marked(settings: Settings) -> None:
    """数据源声明退市日是近似值：有退市日的证券记为不精确；之后精确的数据源覆盖它。"""
    plugin = install(settings, exact_delist_date=False)
    confirm(settings)
    run(settings)
    data = DataStore(settings.data_dir)
    quality = dict(data.read("delist_date_quality").rows())
    assert quality["600001.SH"] is False and quality["600005.SH"] is False
    assert quality["600000.SH"] is None

    behave(plugin)  # 同一只证券，换成给出精确退市日的数据
    run(settings)
    quality = dict(data.read("delist_date_quality").rows())
    assert quality["600001.SH"] is True and quality["600005.SH"] is True


def test_adj_factors_requested_from_earliest(settings: Settings) -> None:
    """同步区间从除权日之后开始，也要拿到之前的累计因子，否则区间开头按 1.0 复权。"""
    plugin = install(settings)
    confirm(settings)
    store, task_id = run(settings, start="2024-01-03")
    result_of(store, task_id)
    assert {c for c in calls(plugin) if c.startswith("adj_factors")} == {"adj_factors 1990-12-19"}
    factors = DataStore(settings.data_dir).read("adj_factors")
    assert (date(2020, 6, 1), "600000.SH", 1.02) in factors.rows()


def test_null_volume_fails(settings: Settings) -> None:
    """成交量为空是“不知道”，不能当成 0 按停牌丢掉。"""
    install(settings, null_volume=True)
    confirm(settings)
    store, task_id = run(settings)
    assert "成交量为空" in failed(store, task_id)
    assert DataStore(settings.data_dir).head() is None


def test_bars_outside_range_fail(settings: Settings) -> None:
    install(settings, late_bars=True)
    confirm(settings)
    store, task_id = run(settings)
    assert "日线有 1 行不在 2024-01-02 至 2024-01-05 之间" in failed(store, task_id)
    assert DataStore(settings.data_dir).head() is None


def test_snapshot_conflict_is_retried(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """提交时当前快照已被别的写入改变：转成 RetryableError，任务回到队列，不留下文件。"""
    install(settings)
    confirm(settings)

    def conflict(self: object, *args: object, **kwargs: object) -> str:
        raise SnapshotConflictError(None, "别的快照")

    monkeypatch.setattr(DataStore, "_commit", conflict)
    store = TaskStore(settings.db_path)
    types = TaskTypeRegistry([data_sync(settings, limits=FAST)])
    body = {"plugin_id": "fake-src", "start": START.isoformat(), "end": END.isoformat()}
    task = store.create("data.sync", types.validate_params("data.sync", body), max_attempts=2)
    assert Worker(store, types).run_once()
    record = store.get(task.id)
    assert record.status == "queued", record.error
    assert record.error is not None and "快照" in record.error
    assert list((settings.data_dir / "files").rglob("*.parquet")) == []


def test_rerun_writes_nothing(settings: Settings) -> None:
    """重复执行只写有变化的行：同样的数据不产生新快照。"""
    install(settings)
    confirm(settings)
    store, first = run(settings)
    head = result_of(store, first).snapshot_id
    store, second = run(settings)
    result = result_of(store, second)
    assert result.snapshot_id is None
    assert set(result.written.values()) == {0}
    assert DataStore(settings.data_dir).head() == head


# ---- 开始前的检查 ----


def test_unconfirmed_endpoint_fails_before_download(settings: Settings) -> None:
    """没确认地址：check_connection 不通过，任务直接失败，提示去插件页确认，不下载。"""
    plugin = install(settings)
    store, task_id = run(settings)
    error = failed(store, task_id)
    assert "连接检查没有通过" in error and "插件页确认" in error
    assert calls(plugin) == ["check_connection"]
    assert DataStore(settings.data_dir).head() is None


def test_plugin_without_check_connection_is_not_checked(settings: Settings) -> None:
    plugin = install(settings, check=False)
    store, task_id = run(settings)
    assert result_of(store, task_id).connection is None
    assert "check_connection" not in calls(plugin)


def test_non_callable_check_connection_fails(settings: Settings) -> None:
    """check_connection 存在但不可调用是插件的错，不能当成“没实现”跳过连接检查。"""
    plugin = install(settings, broken_check=True)
    store, task_id = run(settings)
    error = failed(store, task_id)
    assert "plugin_error" in error and "不可调用" in error
    assert "capabilities" not in calls(plugin)


# ---- 退市股扫描（lists_delisted 为 False 的数据源，#30）----

SMALL_RANGES = (("SH", 600000, 600009), ("SZ", 1, 3))
"""13 个代码：在市的 600000.SH、000001.SZ，退市的 600001.SH（2009）、600005.SH（2024-01-03）。"""


@pytest.fixture
def small_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delisted, "SCAN_RANGES", SMALL_RANGES)
    monkeypatch.setattr(delisted, "SCAN_BATCH", 5)


def lookups(plugin: Path) -> list[str]:
    return [c.removeprefix("lookup ") for c in calls(plugin) if c.startswith("lookup ")]


def test_scan_finds_delisted_securities(settings: Settings, small_scan: None) -> None:
    """代码表只有在市证券：扫描代码区间找到退市股，结果和能直接列出退市证券时一样。"""
    plugin = install(settings, lists_delisted=False)
    confirm(settings)
    store, task_id = run(settings)
    result = result_of(store, task_id)

    assert "securities include_delisted=False" in calls(plugin)
    assert "securities include_delisted=True" not in calls(plugin)
    # 代码表里的 600000.SH、000001.SZ 不用再扫；其余 11 个每 5 个一批
    scanned = [s for batch in lookups(plugin) for s in batch.split(",")]
    assert len(scanned) == 11
    assert "600000.SH" not in scanned and "000001.SZ" not in scanned
    assert (result.scanned, result.rechecked) == (11, 0)

    data = DataStore(settings.data_dir)
    securities = {r["symbol"]: r for r in data.read("securities").iter_rows(named=True)}
    assert securities["600001.SH"]["delist_date"] == date(2009, 12, 25)
    assert securities["600005.SH"]["delist_date"] == date(2024, 1, 3)
    assert result.securities == 5 and result.symbols == 4
    # 区间内退市的 600005.SH 有日线
    bars = data.read("bars_1d")
    assert bars.filter(pl.col("symbol") == "600005.SH").height == 2
    summary = data.catalog.scan_summary("fake-src")
    # 13 个区间代码，加上代码表里不在区间内的 000001.SH、830000.NQ
    assert summary == {"scanned": 15, "found": 6}
    assert {"600001.SH", "600005.SH"} <= set(data.catalog.found_symbols("fake-src"))


def test_scan_is_not_repeated_and_delisted_are_rechecked(
    settings: Settings, small_scan: None
) -> None:
    """扫过的代码不再扫；扫到过但不在代码表里的，每次都重新查询。"""
    plugin = install(settings, lists_delisted=False)
    confirm(settings)
    run(settings)
    behave(plugin, lists_delisted=False)
    store, task_id = run(settings)
    result = result_of(store, task_id)
    assert (result.scanned, result.rechecked) == (0, 2)
    assert lookups(plugin) == ["600001.SH,600005.SH"]
    assert result.snapshot_id is None  # 数据没有变化


def test_newly_delisted_security_is_found(settings: Settings, small_scan: None) -> None:
    """上次还在代码表里、这次不在了：重新查询，写入退市日期。"""
    plugin = install(settings, lists_delisted=False)
    confirm(settings)
    run(settings)
    behave(plugin, lists_delisted=False, delist_now={"600000.SH": "2024-01-04"})
    store, task_id = run(settings)
    result = result_of(store, task_id)
    assert "600000.SH" in lookups(plugin)[0]
    securities = DataStore(settings.data_dir).read("securities")
    row = securities.filter(pl.col("symbol") == "600000.SH").row(0, named=True)
    assert row["delist_date"] == date(2024, 1, 4)
    assert result.rechecked == 3


def test_previously_found_code_missing_fails(settings: Settings, small_scan: None) -> None:
    """之前扫到过、这次查不到：不知道，不能当成没有这只证券，任务失败。"""
    plugin = install(settings, lists_delisted=False)
    confirm(settings)
    store, first = run(settings)
    head = result_of(store, first).snapshot_id
    behave(plugin, lists_delisted=False, lookup_loses=["600001.SH"])
    store, task_id = run(settings)
    assert "查不到" in failed(store, task_id)
    assert DataStore(settings.data_dir).head() == head


def test_failed_scan_resumes_where_it_stopped(settings: Settings, small_scan: None) -> None:
    """扫描中途失败：已经扫完的批次记下来，下次从没扫过的代码接着扫；失败时不写快照。"""
    plugin = install(settings, lists_delisted=False, lookup_fail="600008.SH")
    confirm(settings)
    store, task_id = run(settings)
    failed(store, task_id)
    data = DataStore(settings.data_dir)
    assert data.head() is None
    done = [s for batch in lookups(plugin)[:-1] for s in batch.split(",")]
    assert len(done) == 5  # 第一批扫完，第二批（含 600008.SH）出错

    behave(plugin, lists_delisted=False)
    store, task_id = run(settings)
    result = result_of(store, task_id)
    assert result.scanned == 11 - 5
    *scan_batches, recheck = lookups(plugin)
    assert recheck == "600001.SH,600005.SH"  # 第一批找到的退市股，重新查询
    second = {s for batch in scan_batches for s in batch.split(",")}
    assert len(second) == 6 and not second & set(done)
    assert data.head() == result.snapshot_id


def age_not_found(settings: Settings, days: float, symbols: list[str] | None = None) -> None:
    """把“没找到”的代码的查询时间往前推，模拟时间过去了。"""
    conn = sqlite3.connect(settings.data_dir / "catalog.db")
    where = "found = 0" + (f" AND symbol IN ({','.join('?' * len(symbols))})" if symbols else "")
    conn.execute(
        f"UPDATE security_scan SET scanned_at = scanned_at - ? WHERE {where}",
        [int(days * 86_400_000), *(symbols or [])],
    )
    conn.commit()
    conn.close()


def test_not_found_is_not_rescanned_within_interval(settings: Settings, small_scan: None) -> None:
    """数据源对存在的代码一时返回空（#37）：间隔内不重查，证券列表里暂时没有它。"""
    plugin = install(settings, lists_delisted=False, lookup_loses=["600001.SH"])
    confirm(settings)
    run(settings)
    behave(plugin, lists_delisted=False)  # 数据源恢复了
    age_not_found(settings, 6)  # 离默认的 7 天还差一天
    store, task_id = run(settings)
    result = result_of(store, task_id)
    assert (result.scanned, result.rescanned) == (0, 0)
    assert "600001.SH" not in {s for b in lookups(plugin) for s in b.split(",")}
    securities = DataStore(settings.data_dir).read("securities")
    assert "600001.SH" not in securities.get_column("symbol").to_list()


def test_empty_lookup_is_found_after_rescan_interval(settings: Settings, small_scan: None) -> None:
    """#37：超过间隔后重扫，发现被误记为没有的 600001.SH，写进证券列表并取到它的日线。"""
    plugin = install(settings, lists_delisted=False, lookup_loses=["600001.SH"])
    confirm(settings)
    run(settings)
    behave(plugin, lists_delisted=False)
    age_not_found(settings, 8)
    store, task_id = run(settings)
    result = result_of(store, task_id)
    assert result.scanned == 0
    assert result.rescanned == 10  # 13 个区间代码，去掉代码表里的 2 个和找到过的 600005.SH
    data = DataStore(settings.data_dir)
    row = data.read("securities").filter(pl.col("symbol") == "600001.SH").row(0, named=True)
    assert row["delist_date"] == date(2009, 12, 25)
    assert "600001.SH" in data.catalog.found_symbols("fake-src")

    # 查询时间更新了，间隔内不会再重扫；已经转为扫到过，每次同步重新查询
    behave(plugin, lists_delisted=False)
    store, task_id = run(settings)
    assert result_of(store, task_id).rescanned == 0
    assert "600001.SH" in lookups(plugin)[-1].split(",")


def test_security_listed_and_delisted_between_syncs_is_found(
    settings: Settings, small_scan: None
) -> None:
    """#36：第一次同步时数据源还不认识 600005.SH（它之后才上市又退市）。
    代码被记为没找到；超过间隔后重扫，发现它并写进证券列表。"""
    plugin = install(settings, lists_delisted=False, absent=["600005.SH"])
    confirm(settings)
    run(settings)
    assert "600005.SH" not in DataStore(settings.data_dir).catalog.found_symbols("fake-src")
    behave(plugin, lists_delisted=False)  # 现在是已退市的证券
    age_not_found(settings, 8)
    store, task_id = run(settings)
    assert (
        result_of(store, task_id).rescanned == 10
    )  # 13 个区间代码，去掉代码表里的 2 个和找到过的 600001.SH
    data = DataStore(settings.data_dir)
    row = data.read("securities").filter(pl.col("symbol") == "600005.SH").row(0, named=True)
    assert row["delist_date"] == date(2024, 1, 3)
    assert data.read("bars_1d").filter(pl.col("symbol") == "600005.SH").height == 2


def test_rescan_days_zero_rescans_every_time(settings: Settings, small_scan: None) -> None:
    plugin = install(settings, lists_delisted=False)
    confirm(settings)
    run(settings)
    behave(plugin, lists_delisted=False)
    store, task_id = run(settings, rescan_days=0)
    assert result_of(store, task_id).rescanned == 9
    store, task_id = run(settings, rescan_days=0)
    assert result_of(store, task_id).rescanned == 9


def test_rescan_resumes_after_failure(settings: Settings, small_scan: None) -> None:
    """重扫按批落库：中途失败后，下次只接着重扫还没重扫的代码，不从头来。"""
    plugin = install(settings, lists_delisted=False)
    confirm(settings)
    run(settings)
    age_not_found(settings, 8)
    behave(plugin, lists_delisted=False, lookup_fail="600008.SH")
    store, task_id = run(settings)
    failed(store, task_id)
    done = [s for batch in lookups(plugin)[:-1] for s in batch.split(",")]
    assert len(done) == 5  # 第一批重扫完，第二批（含 600008.SH）出错

    behave(plugin, lists_delisted=False)
    store, task_id = run(settings)
    result = result_of(store, task_id)
    assert result.rescanned == 4
    *rescan_batches, _recheck = lookups(plugin)
    assert not {s for b in rescan_batches for s in b.split(",")} & set(done)


def test_catalog_v2_rows_are_rescanned_after_migration(tmp_path: Path) -> None:
    """v2 的“没找到”行没有可靠的查询时间：迁移后视为已过期，总是重扫；扫到过的不受影响。"""
    path = tmp_path / "catalog.db"
    conn = sqlite3.connect(path)
    for script in (catalog_module._SCHEMA_V1, catalog_module._SCHEMA_V2):  # pyright: ignore[reportPrivateUsage]
        for statement in script.split(";"):
            if statement.strip():
                conn.execute(statement)
    now = 1_800_000_000_000
    conn.execute("INSERT INTO security_scan VALUES ('a', '600001.SH', 0, ?)", (now,))
    conn.execute("INSERT INTO security_scan VALUES ('a', '600002.SH', 1, ?)", (now,))
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    catalog = catalog_module.Catalog(path, clock=lambda: now + 1000)
    assert catalog.rescan_due("a", 7 * 86400) == ["600001.SH"]
    assert catalog.found_symbols("a") == ["600002.SH"]


@pytest.mark.parametrize("limit", [0, 1])
def test_incomplete_listing_fails(settings: Settings, small_scan: None, limit: int) -> None:
    """代码表为空或少了一大截：不知道是不是数据源出错，不扫描、不写快照。"""
    plugin = install(settings, lists_delisted=False)
    confirm(settings)
    store, first = run(settings)
    head = result_of(store, first).snapshot_id
    behave(plugin, lists_delisted=False, listed_limit=limit)
    store, task_id = run(settings)
    assert "代码表可能不完整" in failed(store, task_id)
    assert lookups(plugin) == []
    data = DataStore(settings.data_dir)
    assert data.head() == head
    assert len(data.catalog.found_symbols("fake-src")) == 6


def test_empty_listing_on_first_sync_fails(settings: Settings, small_scan: None) -> None:
    install(settings, lists_delisted=False, listed_limit=0)
    confirm(settings)
    store, task_id = run(settings)
    assert "代码表可能不完整" in failed(store, task_id)
    assert DataStore(settings.data_dir).catalog.scan_summary("fake-src")["scanned"] == 0


def test_lookup_returning_unrequested_code_fails(settings: Settings, small_scan: None) -> None:
    install(settings, lists_delisted=False, lookup_extra=True)
    confirm(settings)
    store, task_id = run(settings)
    assert "没有查询的代码" in failed(store, task_id)
    assert DataStore(settings.data_dir).head() is None


def test_scanned_delist_dates_marked_approximate(settings: Settings, small_scan: None) -> None:
    """eltdx 的真实场景：扫描找到的退市股，退市日记为近似值。"""
    install(settings, lists_delisted=False, exact_delist_date=False)
    confirm(settings)
    run(settings)
    quality = dict(DataStore(settings.data_dir).read("delist_date_quality").rows())
    assert quality == {
        "000001.SH": None,
        "000001.SZ": None,
        "600000.SH": None,
        "600001.SH": False,
        "600005.SH": False,
    }


def test_scan_ranges_cover_a_shares_and_bj() -> None:
    symbols = delisted.scan_symbols()
    assert len(symbols) == len(set(symbols)) == 6000 + 2000 + 4999 + 3000 + 1000
    for symbol in [
        "600000.SH",
        "605999.SH",
        "688981.SH",
        "000001.SZ",
        "002594.SZ",
        "003816.SZ",
        "300750.SZ",
        "301999.SZ",
        "302132.SZ",
        "302999.SZ",
        "920000.BJ",
    ]:
        assert symbol in symbols
    assert "000000.SZ" not in symbols
    assert "900901.SH" not in symbols  # 不含 B 股


def test_unknown_plugin_fails(settings: Settings) -> None:
    settings.plugin_dir.mkdir(parents=True)
    store, task_id = run(settings, plugin_id="nope")
    assert "找不到插件 nope" in failed(store, task_id)


# ---- 数据不合格、取消：不留下快照 ----


def test_bad_bars_abort_without_snapshot(settings: Settings) -> None:
    install(settings, bad_bars=True)
    confirm(settings)
    store, task_id = run(settings)
    assert "没有请求的证券" in failed(store, task_id)
    data = DataStore(settings.data_dir)
    assert data.head() is None
    assert list(data.files_dir.rglob("*.parquet")) == []  # 已写的文件随事务中止删除


def test_cancel_stops_long_plugin_call(settings: Settings) -> None:
    """取消运行中的同步：正在进行的插件调用被立即结束，不等它返回，也不留下快照。"""
    plugin = install(settings, slow_bars=True)
    confirm(settings)
    store, types, task_id = submit(settings)
    worker = threading.Thread(target=Worker(store, types).run_once)
    worker.start()
    deadline = time.monotonic() + 60
    while not any(c.startswith("bars") for c in calls(plugin)):
        assert time.monotonic() < deadline, calls(plugin)
        time.sleep(0.1)
    cancelled_at = time.monotonic()
    store.request_cancel(task_id)
    worker.join(timeout=60)
    assert not worker.is_alive()
    assert time.monotonic() - cancelled_at < 20  # 插件的 bars 要睡 30 秒
    assert store.get(task_id).status == "cancelled"
    assert DataStore(settings.data_dir).head() is None


# ---- 小单元 ----


class _Ctx:
    def __init__(self) -> None:
        self.logs: list[str] = []

    def log(self, message: str, level: str = "info") -> None:
        self.logs.append(message)

    def check_cancelled(self) -> None:
        return None


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("timeout", RetryableError), ("crashed", RetryableError), ("memory", Exception)],
)
def test_plugin_errors_retry_only_transient(reason: str, expected: type[Exception]) -> None:
    ctx: Any = _Ctx()
    with pytest.raises(expected) as info, plugin_errors(ctx, "p"):
        raise PluginRunError(reason, "x")  # type: ignore[arg-type]
    if expected is Exception:
        assert not isinstance(info.value, RetryableError)


def test_params_validation() -> None:
    with pytest.raises(pydantic.ValidationError):
        SyncParams(plugin_id="p", start=date(2024, 1, 5), end=date(2024, 1, 2))
    with pytest.raises(pydantic.ValidationError):
        SyncParams.model_validate({"plugin_id": "p", "extra": 1})
    assert SyncParams(plugin_id="p").start == date(1990, 12, 19)


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1, 3651])
def test_rescan_days_validation(bad: float) -> None:
    """inf 会让重扫截止时间溢出，过大的值会超出 SQLite 的整数范围：都在参数校验里拒绝。"""
    with pytest.raises(pydantic.ValidationError):
        SyncParams(plugin_id="p", rescan_days=bad)
    assert SyncParams(plugin_id="p", rescan_days=3650).rescan_days == 3650
    assert SyncParams(plugin_id="p", rescan_days=0).rescan_days == 0


def beijing(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, minute, tzinfo=timezone(timedelta(hours=8)))


def test_last_closed_day_uses_beijing_time() -> None:
    assert last_closed_day(lambda: beijing(5, 10)) == date(2024, 1, 4)  # 盘中：昨天
    assert last_closed_day(lambda: beijing(5, 15, 29)) == date(2024, 1, 4)
    assert last_closed_day(lambda: beijing(5, 15, 30)) == date(2024, 1, 5)  # 收盘后：今天
    # UTC 1 月 4 日 08:00 是北京时间 1 月 4 日 16:00，已收盘
    assert last_closed_day(lambda: datetime(2024, 1, 4, 8, 0, tzinfo=UTC)) == date(2024, 1, 4)
    # UTC 1 月 4 日 17:00 是北京时间 1 月 5 日 01:00，最近收盘的是 1 月 4 日
    assert last_closed_day(lambda: datetime(2024, 1, 4, 17, 0, tzinfo=UTC)) == date(2024, 1, 4)


def run_at(settings: Settings, clock: datetime, **params: Any) -> tuple[TaskStore, int]:
    store = TaskStore(settings.db_path)
    types = TaskTypeRegistry([data_sync(settings, limits=FAST, now=lambda: clock)])
    body = {"plugin_id": "fake-src", "start": START.isoformat(), **params}
    task = store.create("data.sync", types.validate_params("data.sync", body), max_attempts=1)
    assert Worker(store, types).run_once()
    return store, task.id


def test_default_end_skips_unclosed_day(settings: Settings) -> None:
    """盘中运行：默认 end 是昨天，今天还在变的日线不进快照。"""
    plugin = install(settings)
    confirm(settings)
    store, task_id = run_at(settings, beijing(5, 10))
    result = result_of(store, task_id)
    assert result.end == date(2024, 1, 4)
    assert "bars" in " ".join(calls(plugin))
    dates = DataStore(settings.data_dir).read("bars_1d").get_column("date")
    assert dates.max() == date(2024, 1, 4)


def test_default_end_after_close_is_today(settings: Settings) -> None:
    install(settings)
    confirm(settings)
    store, task_id = run_at(settings, beijing(5, 16))
    assert result_of(store, task_id).end == date(2024, 1, 5)


def test_explicit_unclosed_end_fails(settings: Settings) -> None:
    """显式传入还没收盘的 end 也不行，不下载任何数据。"""
    plugin = install(settings)
    confirm(settings)
    store, task_id = run_at(settings, beijing(5, 10), end="2024-01-05")
    assert "还没有收盘" in failed(store, task_id)
    assert calls(plugin) == []


# ---- ST 名单和指数成分快照（#43）----

CLOSE_DAY = beijing(5, 16)  # 2024-01-05（星期五）收盘后：最近已收盘的交易日是这一天
INDEXES = ["000300.SH", "000905.SH"]
MEMBERS = {
    "000300.SH": [
        ("600000.SH", "1999-11-10", None),
        ("000001.SZ", "2010-01-04", None),
        ("600001.SH", "2000-01-01", "2009-12-25"),  # 早已调出
        ("600005.SH", "2024-01-08", None),  # 快照日期时还没调入
        ("000002.SZ", None, "2024-01-09"),  # 已宣布的调出日在快照日期之后：当天仍是成分
    ],
}


def snapshot_rows(settings: Settings, dataset: str) -> list[tuple[Any, ...]]:
    return DataStore(settings.data_dir).read(dataset).rows()


def test_snapshots_written_at_close_in_same_transaction(settings: Settings) -> None:
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "*ST平安", "600000.SH": "浦发ST"}, index_members=MEMBERS)
    confirm(settings)
    store, task_id = run_at(settings, CLOSE_DAY, indexes=INDEXES)
    result = result_of(store, task_id)

    assert result.snapshot_date == date(2024, 1, 5) and result.snapshot_skipped is None
    # 只有名称前缀是 ST 的算；名称中间含 ST 的不算；退市的、指数不在名单里
    assert snapshot_rows(settings, "st_daily") == [(date(2024, 1, 5), "000001.SZ", "*ST平安", "st")]
    assert snapshot_rows(settings, "index_members_daily") == [
        (date(2024, 1, 5), "000300.SH", "000001.SZ"),
        (date(2024, 1, 5), "000300.SH", "000002.SZ"),
        (date(2024, 1, 5), "000300.SH", "600000.SH"),
    ]
    # 000905.SH 插件没有返回成分：未知，不写成空名单，原因写进结果
    assert list(result.index_snapshot_failed) == ["000905.SH"]
    assert "为空" in result.index_snapshot_failed["000905.SH"]
    assert result.written["st_daily"] == 1 and result.written["index_members_daily"] == 3
    # 与日线等在同一个快照里
    data = DataStore(settings.data_dir)
    assert result.snapshot_id is not None
    for dataset in ("bars_1d", "st_daily", "index_members_daily"):
        assert data.read(dataset, snapshot=result.snapshot_id).height > 0
    assert "index_constituents 000300.SH 2024-01-05 2024-01-05" in calls(plugin)


def test_default_indexes_are_requested(settings: Settings) -> None:
    plugin = install(settings)
    behave(plugin, index_members=MEMBERS)
    confirm(settings)
    store, task_id = run_at(settings, CLOSE_DAY)
    result_of(store, task_id)
    asked = [c.split()[1] for c in calls(plugin) if c.startswith("index_constituents")]
    assert asked == [
        "000300.SH",
        "000905.SH",
        "000852.SH",
        "000016.SH",
        "399006.SZ",
        "000688.SH",
    ]


def test_empty_indexes_param_skips_members(settings: Settings) -> None:
    plugin = install(settings)
    confirm(settings)
    store, task_id = run_at(settings, CLOSE_DAY, indexes=[])
    result = result_of(store, task_id)
    assert not [c for c in calls(plugin) if c.startswith("index_constituents")]
    assert result.index_snapshot_failed == {} and result.snapshot_date == date(2024, 1, 5)


@pytest.mark.parametrize("behavior", [{}, {"bad_index_format": True}])
def test_index_failure_is_unknown_not_empty(settings: Settings, behavior: dict[str, Any]) -> None:
    """index_constituents 抛 NotImplementedError（插件错误）、返回格式不对：不写成分，不写空名单，
    同步照常成功。方法根本不存在（unsupported）的情形见 test_snapshots.py 的假会话测试。"""
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "ST平安"}, **behavior)
    confirm(settings)
    store, task_id = run_at(settings, CLOSE_DAY, indexes=["000300.SH"])
    result = result_of(store, task_id)
    assert list(result.index_snapshot_failed) == ["000300.SH"]
    assert snapshot_rows(settings, "index_members_daily") == []
    assert snapshot_rows(settings, "st_daily") == [(date(2024, 1, 5), "000001.SZ", "ST平安", "st")]
    assert DataStore(settings.data_dir).read("bars_1d").height > 0
    warnings = [log.message for log in store.logs(task_id) if log.level == "warning"]
    assert any("000300.SH" in m for m in warnings)


def test_no_snapshot_when_end_is_before_last_closed_day(settings: Settings) -> None:
    """回补历史：今天的名单不能当成过去的名单。"""
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "*ST平安"}, index_members=MEMBERS)
    confirm(settings)
    store, task_id = run_at(settings, CLOSE_DAY, end="2024-01-04", indexes=INDEXES)
    result = result_of(store, task_id)
    assert result.snapshot_date is None and "早于" in (result.snapshot_skipped or "")
    assert snapshot_rows(settings, "st_daily") == []
    assert snapshot_rows(settings, "index_members_daily") == []
    assert not [c for c in calls(plugin) if c.startswith("index_constituents")]
    assert DataStore(settings.data_dir).read("bars_1d").height > 0


def test_backfill_does_not_touch_existing_snapshots(settings: Settings) -> None:
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "*ST平安"})
    confirm(settings)
    result_of(*run_at(settings, CLOSE_DAY, indexes=[]))
    before = snapshot_rows(settings, "st_daily")
    behave(plugin, names={"000001.SZ": "平安银行"})  # 之后名称变了
    result_of(*run_at(settings, CLOSE_DAY, end="2024-01-04", indexes=[]))
    assert snapshot_rows(settings, "st_daily") == before


def test_weekend_run_writes_no_snapshot_and_keeps_friday(settings: Settings) -> None:
    """北京时间 1 月 6 日（星期六）16:00：最近已收盘的日子是周六，不是交易日：不写快照，
    也不动周五已有的快照。"""
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "ST平安"})
    confirm(settings)
    result_of(*run_at(settings, CLOSE_DAY, indexes=[]))
    before = snapshot_rows(settings, "st_daily")
    behave(plugin, names={"000001.SZ": "平安银行"})
    result = result_of(*run_at(settings, beijing(6, 16), indexes=[]))
    assert result.end == date(2024, 1, 6) and result.snapshot_date is None
    assert "不是交易日" in (result.snapshot_skipped or "")
    assert snapshot_rows(settings, "st_daily") == before


def test_snapshot_date_uses_shanghai_time_not_local_time(settings: Settings) -> None:
    """机器在美东（UTC-5）：本机时间 1 月 5 日 03:00 是北京时间 16:00，快照日期是 1 月 5 日。
    按本机时间算会得到 1 月 4 日。"""
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "ST平安"})
    confirm(settings)
    new_york = datetime(2024, 1, 5, 3, 0, tzinfo=timezone(timedelta(hours=-5)))
    result = result_of(*run_at(settings, new_york, indexes=[]))
    assert result.snapshot_date == date(2024, 1, 5)
    assert snapshot_rows(settings, "st_daily") == [(date(2024, 1, 5), "000001.SZ", "ST平安", "st")]


def test_pre_open_run_does_not_overwrite_previous_day(settings: Settings) -> None:
    """1 月 5 日（周五）16:00 同步一次；1 月 8 日（周一）09:00（最近已收盘的是周日）、
    1 月 9 日（周二）09:00（最近已收盘的是周一，但名单已是周二的）盘前运行，名称已经变了：
    周五的行不变，周一周二没写。"""
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "ST平安"}, index_members=MEMBERS)
    confirm(settings)
    result_of(*run_at(settings, CLOSE_DAY, indexes=INDEXES))
    st_before = snapshot_rows(settings, "st_daily")
    members_before = snapshot_rows(settings, "index_members_daily")
    assert st_before and members_before

    behave(plugin, names={"000001.SZ": "平安银行", "600000.SH": "ST浦发"}, index_members={})
    for clock in (beijing(8, 9), beijing(9, 9, 0)):  # 周一 09:00、周二 09:00
        result = result_of(*run_at(settings, clock, indexes=INDEXES))
        assert result.snapshot_date is None and "收盘前" in (result.snapshot_skipped or "")
        assert snapshot_rows(settings, "st_daily") == st_before
        assert snapshot_rows(settings, "index_members_daily") == members_before


def test_pre_open_run_on_trading_day_after_trading_day(settings: Settings) -> None:
    """1 月 4 日（周四）16:00 写了快照；1 月 5 日 09:00 运行时最近已收盘的日子 1 月 4 日
    是交易日，但名单是 1 月 5 日开盘前的：不写。"""
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "ST平安"})
    confirm(settings)
    result_of(*run_at(settings, beijing(4, 16), indexes=[]))
    before = snapshot_rows(settings, "st_daily")
    assert before[0][0] == date(2024, 1, 4)
    behave(plugin, names={})
    result = result_of(*run_at(settings, beijing(5, 9), indexes=[]))
    assert result.snapshot_date is None
    assert snapshot_rows(settings, "st_daily") == before


def test_unknown_st_status_is_persisted_and_not_deleted(settings: Settings) -> None:
    """名称拿不到的股票写成 unknown（不是不写）；同一天重复同步时名称又拿不到，
    已有的 ST 行保持不变，不被改成 unknown 也不被删除。"""
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "*ST平安", "600000.SH": "浦发银行"})
    confirm(settings)
    result_of(*run_at(settings, CLOSE_DAY, indexes=[]))
    assert snapshot_rows(settings, "st_daily") == [(date(2024, 1, 5), "000001.SZ", "*ST平安", "st")]

    behave(plugin, names={"000001.SZ": "", "600000.SH": ""})  # 这次两只都拿不到名称
    # 全部为空整个 ST 快照不写：原来的行不动
    result = result_of(*run_at(settings, CLOSE_DAY, indexes=[]))
    assert result.st_snapshot_failed is not None
    assert snapshot_rows(settings, "st_daily") == [(date(2024, 1, 5), "000001.SZ", "*ST平安", "st")]

    behave(plugin, names={"000001.SZ": "", "600000.SH": "ST浦发"})  # 只有一只拿不到
    result = result_of(*run_at(settings, CLOSE_DAY, indexes=[]))
    assert result.st_unknown_names == 1
    assert snapshot_rows(settings, "st_daily") == [
        (date(2024, 1, 5), "000001.SZ", "*ST平安", "st"),  # 保持原来知道的
        (date(2024, 1, 5), "600000.SH", "ST浦发", "st"),
    ]


def test_unknown_st_status_is_written_on_first_snapshot(settings: Settings) -> None:
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "", "600000.SH": "ST浦发"})
    confirm(settings)
    result = result_of(*run_at(settings, CLOSE_DAY, indexes=[]))
    assert result.st_unknown_names == 1
    assert snapshot_rows(settings, "st_daily") == [
        (date(2024, 1, 5), "000001.SZ", None, "unknown"),
        (date(2024, 1, 5), "600000.SH", "ST浦发", "st"),
    ]

    behave(plugin, names={"000001.SZ": "平安银行", "600000.SH": "ST浦发"})  # 后来拿到了：不是 ST
    result_of(*run_at(settings, CLOSE_DAY, indexes=[]))
    assert snapshot_rows(settings, "st_daily") == [(date(2024, 1, 5), "600000.SH", "ST浦发", "st")]


def test_index_exception_only_affects_that_index(settings: Settings) -> None:
    plugin = install(settings)
    behave(plugin, index_members=MEMBERS, index_raise=["000905.SH"])
    confirm(settings)
    result = result_of(*run_at(settings, CLOSE_DAY, indexes=INDEXES))
    assert list(result.index_snapshot_failed) == ["000905.SH"]
    assert "boom" in result.index_snapshot_failed["000905.SH"]
    assert len(snapshot_rows(settings, "index_members_daily")) == 3  # 000300.SH 照常写入


def test_index_crash_fails_task(settings: Settings) -> None:
    """插件子进程在取成分时崩溃：不是“只影响一个指数”，任务按崩溃失败，不留下快照。"""
    plugin = install(settings)
    behave(plugin, index_members=MEMBERS, index_crash=True)
    confirm(settings)
    store, task_id = run_at(settings, CLOSE_DAY, indexes=INDEXES)
    assert "crashed" in failed(store, task_id)
    assert DataStore(settings.data_dir).head() is None


def test_rerun_same_day_replaces_that_days_rows(settings: Settings) -> None:
    """同一天再同步一次：以最后一次为准，已经不在名单里的行作废。"""
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "*ST平安", "600000.SH": "ST浦发"}, index_members=MEMBERS)
    confirm(settings)
    result_of(*run_at(settings, CLOSE_DAY, indexes=INDEXES))
    assert len(snapshot_rows(settings, "st_daily")) == 2

    members = {"000300.SH": [("600000.SH", None, None)]}
    behave(plugin, names={"600000.SH": "ST浦发"}, index_members=members)
    result_of(*run_at(settings, CLOSE_DAY, indexes=INDEXES))
    assert snapshot_rows(settings, "st_daily") == [(date(2024, 1, 5), "600000.SH", "ST浦发", "st")]
    assert snapshot_rows(settings, "index_members_daily") == [
        (date(2024, 1, 5), "000300.SH", "600000.SH")
    ]


def test_failed_sync_leaves_no_snapshot_rows(settings: Settings) -> None:
    install(settings, bad_bars=True, names={"000001.SZ": "ST平安"})
    confirm(settings)
    store, task_id = run_at(settings, CLOSE_DAY, indexes=[])
    assert "没有请求的证券" in failed(store, task_id)
    data = DataStore(settings.data_dir)
    assert data.head() is None
    assert list(data.files_dir.rglob("*.parquet")) == []


def test_all_names_empty_is_unknown(settings: Settings) -> None:
    """在市股票的名称全是空：ST 状态未知，不写（不能写成“没有 ST”）。"""
    plugin = install(settings)
    behave(plugin, names={"000001.SZ": "", "600000.SH": " "})
    confirm(settings)
    result = result_of(*run_at(settings, CLOSE_DAY, indexes=[]))
    assert result.st_snapshot_failed is not None
    assert snapshot_rows(settings, "st_daily") == []


def test_params_indexes_validation() -> None:
    assert SyncParams(plugin_id="p", indexes=[" 000300.SH ", "000300.SH"]).indexes == ["000300.SH"]
    assert SyncParams(plugin_id="p").indexes is None
    with pytest.raises(pydantic.ValidationError):
        SyncParams(plugin_id="p", indexes=[""])


BOTH = {**MEMBERS, "000905.SH": [("600000.SH", None, None), ("000001.SZ", None, None)]}


def test_failed_index_keeps_its_existing_rows(settings: Settings) -> None:
    """同一天重复同步，某个指数这次读取失败：它已有的行不变（未知不是空，也不是作废）。"""
    plugin = install(settings)
    behave(plugin, index_members=BOTH)
    confirm(settings)
    result_of(*run_at(settings, CLOSE_DAY, indexes=INDEXES))
    before = snapshot_rows(settings, "index_members_daily")
    assert {r[1] for r in before} == set(INDEXES)

    behave(plugin, index_members=BOTH, index_raise=["000300.SH"])
    result = result_of(*run_at(settings, CLOSE_DAY, indexes=INDEXES))
    assert list(result.index_snapshot_failed) == ["000300.SH"]
    assert snapshot_rows(settings, "index_members_daily") == before


def test_index_not_in_this_run_is_left_alone(settings: Settings) -> None:
    """这次 indexes 里没有的指数，当天已有的行不动；在列表里的照常以这次为准。"""
    plugin = install(settings)
    behave(plugin, index_members=BOTH)
    confirm(settings)
    result_of(*run_at(settings, CLOSE_DAY, indexes=INDEXES))
    others = [r for r in snapshot_rows(settings, "index_members_daily") if r[1] == "000905.SH"]
    assert others

    changed = {"000300.SH": [("600000.SH", None, None)], "000905.SH": [("600000.SH", None, None)]}
    behave(plugin, index_members=changed)
    result_of(*run_at(settings, CLOSE_DAY, indexes=["000300.SH"]))
    rows = snapshot_rows(settings, "index_members_daily")
    assert [r for r in rows if r[1] == "000905.SH"] == others
    assert [r[2] for r in rows if r[1] == "000300.SH"] == ["600000.SH"]
