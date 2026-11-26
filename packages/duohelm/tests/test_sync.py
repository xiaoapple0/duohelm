"""data.sync：从任务入口跑到隔离进程里的行情源插件，再到快照（#24）。"""

from __future__ import annotations

import json
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
    return [
        (*row[:5], date.fromisoformat(changed[row[0]])) if row[0] in changed else row
        for row in SECURITIES
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
        return pl.DataFrame({"date": [d for d in CALENDAR if start <= d <= end]})

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
        raise NotImplementedError


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
    assert result.written == {"securities": 5, "calendar": 4, "bars_1d": 13, "adj_factors": 2}


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


def test_scan_ranges_cover_a_shares_and_bj() -> None:
    symbols = delisted.scan_symbols()
    assert len(symbols) == len(set(symbols)) == 6000 + 2000 + 4999 + 2000 + 1000
    for symbol in [
        "600000.SH",
        "605999.SH",
        "688981.SH",
        "000001.SZ",
        "002594.SZ",
        "003816.SZ",
        "300750.SZ",
        "301999.SZ",
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
