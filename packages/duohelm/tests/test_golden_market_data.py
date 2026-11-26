"""行情数据的黄金数据集，第一层：构造数据（#44，说明见 docs/subsystems/data.md“黄金数据集”）。

走完整的链路：测试行情源插件（隔离进程）→ ``data.sync`` → 快照 → 读取 → SDK 复权。

**期望值全部独立得出**：因子和复权价是按文档里的公式手算的分数，计算过程写在注释里；
行数、丢弃数由输入数据直接数出来。不得运行被测代码再把输出粘贴成期望值。
结果变化时先判断是谁错了，再改期望值，并在 PR 说明里写原因。
"""

from __future__ import annotations

import json
import textwrap
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from duohelm.isolation import Limits
from duohelm.settings import Settings
from duohelm.storage import DataStore
from duohelm.tasks import TaskStore, TaskTypeRegistry, Worker
from duohelm.tasks.sync import SyncResult, data_sync
from duohelm_sdk.adjust import adjust_bars

FAST = Limits(call_timeout_s=60, startup_timeout_s=60, poll_interval_s=0.1)

# ---- 输入：构造的行情 ----------------------------------------------------------------

# 10 个交易日。2024-01-01 休市，所以 12-29（周五）后面是 01-02；跨 2023、2024 两个分区
DAYS = [
    date(2023, 12, 27),  # d0
    date(2023, 12, 28),  # d1
    date(2023, 12, 29),  # d2
    date(2024, 1, 2),  # d3
    date(2024, 1, 3),  # d4
    date(2024, 1, 4),  # d5
    date(2024, 1, 5),  # d6
    date(2024, 1, 8),  # d7
    date(2024, 1, 9),  # d8
    date(2024, 1, 10),  # d9
]
D0, D1, D2, D3, D4, D5, D6, D7, D8, D9 = DAYS
START, END = D0, D9

# (代码, 名称, 交易所, 种类, 上市日, 退市日)
SECURITIES: list[tuple[str, str, str, str, date | None, date | None]] = [
    ("000001.SH", "指数甲", "SH", "index", date(1990, 12, 19), None),
    ("600100.SH", "派现股", "SH", "stock", date(2000, 1, 3), None),
    ("600200.SH", "送转股", "SH", "stock", date(2000, 1, 3), None),
    ("600300.SH", "配股股", "SH", "stock", date(2000, 1, 3), None),
    ("600400.SH", "跨年股", "SH", "stock", date(2000, 1, 3), None),
    ("600500.SH", "停牌股", "SH", "stock", date(2000, 1, 3), None),
    ("600600.SH", "新股", "SH", "stock", D6, None),  # 上市首日 2024-01-05
    # 摘牌日 2024-01-10，退市日 = 摘牌日之前的最后一个交易日 = d8；最后一根 K 线在 d3
    ("600800.SH", "退市股", "SH", "stock", date(2000, 1, 3), D8),
    ("600900.SH", "早退股", "SH", "stock", date(1999, 5, 6), date(2023, 6, 30)),  # 退市日早于区间
    ("600700.SH", "后市股", "SH", "stock", date(2024, 2, 1), None),  # 上市日晚于区间
]

# 日线：(日期序号, 收盘价, 成交量)；开盘 = 收盘 - 0.25，最高 = 收盘 + 0.5，最低 = 收盘 - 0.5，
# 成交额 = 收盘 × 成交量。价格都取二进制下可以精确表示的数。成交量为 0 的是占位行：价格全等于
# 收盘价，成交额为 0
VOL = 1000.0


def series(closes: list[float], first: int = 0) -> list[tuple[int, float, float]]:
    """从第 first 个交易日起连续的日线（成交量 VOL）。"""
    return [(first + i, close, VOL) for i, close in enumerate(closes)]


CLOSES: dict[str, list[tuple[int, float, float]]] = {
    # 指数：10 行，无事件
    "000001.SH": [
        (i, c, 1e8)
        for i, c in enumerate([3000.0, 3010.0, 3005.0, 3020.0, 3015.0, 3025.0, 3030.0, 3040.0])
    ]
    + [(8, 3035.0, 1e8), (9, 3050.0, 1e8)],
    # 派现：d4 除息，每股 0.5。d3 收盘 10.0
    "600100.SH": series([10.0, 10.0, 10.0, 10.0, 9.5, 9.5, 9.5, 9.5, 9.5, 9.5]),
    # 送转：d5 除权，10 转 5（每股 0.5）。d4 收盘 12.0。d0 是成交量为 0 的占位行（会被丢弃）
    "600200.SH": [(0, 11.5, 0.0), *series([12.0, 12.0, 12.0, 12.0, 8.0, 8.0, 8.0, 8.0, 8.0], 1)],
    # 配股：d5 除权，每股配 0.3 股，配股价 6.00。d4 收盘 10.0
    "600300.SH": series([10.0, 10.0, 10.0, 10.0, 10.0, 9.0, 9.0, 9.0, 9.0, 9.0]),
    # 跨年两次：d2（2023-12-29）10 送 10，d1 收盘 20.0；d6（2024-01-05）每股派现 0.25，d5 收盘 10.5
    "600400.SH": series([20.0, 20.0, 10.0, 10.0, 10.5, 10.5, 10.25, 10.25, 10.5, 10.5]),
    # 停牌：d4 没有行，d5 是占位行（成交量 0，价格等于前收盘 20.0）；d6 复牌当天除息，每股 1.0
    "600500.SH": [*series([20.0] * 4), (5, 20.0, 0.0), *series([19.5] * 4, 6)],
    # 新股：d6 上市首日，之后连续有行
    "600600.SH": [(6, 43.5, VOL), (7, 47.5, VOL), (8, 46.0, VOL), (9, 45.5, VOL)],
    # 退市股：d3 之后停牌直到摘牌（d4 是成交量为 0 的占位行，d5–d8 没有行）
    "600800.SH": [(0, 5.0, VOL), (1, 5.0, VOL), (2, 5.0, VOL), (3, 5.0, VOL), (4, 5.0, 0.0)],
}

# 除权除息事件：(代码, 日期序号, 每股派现, 每股送转, 每股配股数, 配股价)
EVENTS: list[tuple[str, int, float, float, float, float]] = [
    ("600100.SH", 4, 0.5, 0.0, 0.0, 0.0),
    ("600200.SH", 5, 0.0, 0.5, 0.0, 0.0),
    ("600300.SH", 5, 0.0, 0.0, 0.3, 6.0),
    ("600400.SH", 2, 0.0, 1.0, 0.0, 0.0),
    ("600400.SH", 6, 0.25, 0.0, 0.0, 0.0),
    ("600500.SH", 6, 1.0, 0.0, 0.0, 0.0),
    ("600600.SH", 3, 0.1, 0.0, 0.0, 0.0),  # 上市（d6）之前的事件：不产生因子
]


def bar_row(symbol: str, index: int, close: float, volume: float) -> tuple[Any, ...]:
    if volume == 0.0:
        return (DAYS[index], symbol, close, close, close, close, 0.0, 0.0)
    return (
        DAYS[index],
        symbol,
        close - 0.25,
        close + 0.5,
        close - 0.5,
        close,
        volume,
        close * volume,
    )


ALL_BARS = [bar_row(s, i, c, v) for s, rows in CLOSES.items() for i, c, v in rows]
KEPT_BARS = [r for r in ALL_BARS if r[6] != 0.0]  # 成交量为 0 的占位行被丢弃

# ---- 期望值（独立得出，见各自的注释） --------------------------------------------------

# 数出来的：入库的行数 = 输入行数 - 占位行。输入：指数 10、派现 10、送转 10（含 1 行占位）、
# 配股 10、跨年 10、停牌 9（含 1 行占位）、新股 4、退市股 5（含 1 行占位）
DROPPED = 3  # 600200.SH d0、600500.SH d5、600800.SH d4
BARS_ROWS = 65  # 10 + 10 + 9 + 10 + 10 + 8 + 4 + 4
# 2023 年（d0–d2）：指数 3、派现 3、送转 2（d0 被丢弃）、配股 3、跨年 3、停牌 3、新股 0、退市股 3
BARS_2023 = 20
BARS_2024 = 45

# 复权因子：累计后复权比例。每个事件的比例 = P / 参考价，P 是除权日之前最近一个收盘价，
# 参考价 = (P - 派现 + 配股价 × 配股数) / (1 + 送转 + 配股数)。手算：
FACTORS: dict[str, list[tuple[date, Fraction]]] = {
    # P = d3 收盘 10；参考价 = 10 - 0.5 = 9.5；比例 = 10 / 9.5 = 20/19
    "600100.SH": [(D4, Fraction(20, 19))],
    # P = d4 收盘 12；参考价 = 12 / 1.5 = 8；比例 = 12 / 8 = 3/2
    "600200.SH": [(D5, Fraction(3, 2))],
    # P = d4 收盘 10；参考价 = (10 + 6 × 0.3) / 1.3 = 11.8 / 1.3；
    # 比例 = 10 × 1.3 / 11.8 = 13 / 11.8 = 65/59
    "600300.SH": [(D5, Fraction(65, 59))],
    # d2：P = d1 收盘 20；参考价 = 20 / 2 = 10；比例 2。
    # d6：P = d5 收盘 10.5；参考价 = 10.5 - 0.25 = 10.25；比例 = 10.5 / 10.25 = 42/41；
    # 累计 2 × 42/41 = 84/41
    "600400.SH": [(D2, Fraction(2)), (D6, Fraction(84, 41))],
    # 停牌期间没有行，占位行（d5）的收盘价也是 20，所以 P = 20；参考价 = 20 - 1 = 19；比例 = 20/19
    "600500.SH": [(D6, Fraction(20, 19))],
    # 指数、新股（上市前的事件）、退市股：没有因子
}
FACTOR_ROWS = 6

# 后复权收盘价 = 收盘 × 当天生效的因子。重点行手算：
HFQ_CLOSE: dict[tuple[str, date], Fraction] = {
    ("600100.SH", D3): Fraction(10),  # 除权前，因子 1
    ("600100.SH", D4): Fraction(19, 2) * Fraction(20, 19),  # 9.5 × 20/19 = 10：恰好等于除权前收盘
    ("600100.SH", D9): Fraction(19, 2) * Fraction(20, 19),  # 9.5 × 20/19 = 10
    ("600200.SH", D4): Fraction(12),
    ("600200.SH", D5): Fraction(8) * Fraction(3, 2),  # 12
    ("600300.SH", D4): Fraction(10),
    ("600300.SH", D5): Fraction(9) * Fraction(65, 59),  # 585/59
    ("600400.SH", D1): Fraction(20),
    ("600400.SH", D2): Fraction(10) * 2,  # 20：送转后后复权价等于送转前收盘
    ("600400.SH", D5): Fraction(21, 2) * 2,  # 21
    ("600400.SH", D6): Fraction(41, 4) * Fraction(84, 41),  # 10.25 × 84/41 = 21
    ("600400.SH", D9): Fraction(21, 2) * Fraction(84, 41),  # 882/41
    ("600500.SH", D3): Fraction(20),
    ("600500.SH", D6): Fraction(39, 2) * Fraction(20, 19),  # 19.5 × 20/19 = 390/19
    ("600600.SH", D6): Fraction(87, 2),  # 43.5，没有因子
    ("600800.SH", D3): Fraction(5),
}

# 前复权收盘价 = 收盘 × 因子(t) / 因子(基准日)。基准日 d9（2024-01-10）、d3（2024-01-02）：
QFQ_CLOSE: dict[tuple[date, str, date], Fraction] = {
    # 基准 d9：派现因子 20/19；d3 收盘 10 → 10 / (20/19) = 9.5
    (D9, "600100.SH", D3): Fraction(19, 2),
    (D9, "600100.SH", D9): Fraction(19, 2),  # 基准日当天等于未复权价
    # 基准 d9：配股因子 65/59；d0 收盘 10 → 10 × 59/65 = 118/13
    (D9, "600300.SH", D0): Fraction(118, 13),
    # 基准 d9：跨年因子 84/41；d0 收盘 20 → 20 × 41/84 = 205/21
    (D9, "600400.SH", D0): Fraction(205, 21),
    # 基准 d3：跨年因子 2；d6 收盘 10.25，因子 84/41 → 10.25 × (84/41) / 2 = 10.5
    (D3, "600400.SH", D6): Fraction(21, 2),
    # d3 的因子是 2，d3 收盘 10，基准日当天 = 10
    (D3, "600400.SH", D3): Fraction(10),
    # 基准 d3：送转还没发生（因子 1）；d9 收盘 8 × 3/2 / 1 = 12
    (D3, "600200.SH", D9): Fraction(12),
    # 指数没有因子：前复权价就是未复权价
    (D9, "000001.SH", D0): Fraction(3000),
}

# 证券列表：全部 10 只都入库（含退市日早于区间的和上市日晚于区间的），只有 8 只请求了日线
SECURITIES_EXPECTED = {row[0]: (row[4], row[5]) for row in SECURITIES}
SYMBOLS_REQUESTED = 8  # 退市日早于区间的 600900.SH 和上市日晚于区间的 600700.SH 不取日线


def step_factor(symbol: str, day: date) -> Fraction:
    """手算因子的阶梯函数：当天及以前最后一次除权的累计因子，没有则 1。"""
    result = Fraction(1)
    for ex_day, factor in FACTORS.get(symbol, []):
        if ex_day <= day:
            result = factor
    return result


# ---- 夹具：测试用行情源插件 ------------------------------------------------------------

MANIFEST = """
[plugin]
id = "golden-src"
name = "golden-src"
version = "1.0.0"
kind = "datasource"
sdk = ">=0.4,<0.5"
entrypoint = "golden_src:source"
"""

PLUGIN_CODE = """
import json
from datetime import date
from pathlib import Path

import polars as pl
from duohelm_sdk import Capabilities, Freq
from duohelm_sdk.adjust import EX_EVENTS_SCHEMA, factors_from_events
from duohelm_sdk.schema import BARS_SCHEMA, SECURITIES_SCHEMA

IN = json.loads((Path(__file__).parent / "inputs.json").read_text(encoding="utf-8"))


def d(s):
    return None if s is None else date.fromisoformat(s)


CALENDAR = [d(s) for s in IN["calendar"]]
SECURITIES = [(*r[:4], d(r[4]), d(r[5])) for r in IN["securities"]]
BARS = [(d(r[0]), *r[1:]) for r in IN["bars"]]
EVENTS = [(d(r[0]), *r[1:]) for r in IN["events"]]


class Source:
    def capabilities(self):
        return Capabilities(
            markets=("CN",),
            freqs=(Freq.DAY,),
            fields=("open", "high", "low", "close", "volume", "amount"),
            lists_delisted=True,
            exact_delist_date=True,
        )

    def securities(self, *, include_delisted=False):
        rows = [r for r in SECURITIES if include_delisted or r[5] is None]
        return pl.DataFrame(rows, schema=SECURITIES_SCHEMA, orient="row")

    def lookup_securities(self, symbols):
        rows = [r for r in SECURITIES if r[0] in symbols]
        return pl.DataFrame(rows, schema=SECURITIES_SCHEMA, orient="row")

    def trading_calendar(self, market, start, end):
        return pl.DataFrame({"date": [x for x in CALENDAR if start <= x <= end]})

    def bars(self, symbols, freq, start, end):
        rows = [r for r in BARS if r[1] in symbols and start <= r[0] <= end]
        return pl.DataFrame(rows, schema=BARS_SCHEMA, orient="row")

    def adj_factors(self, symbols, start, end):
        # 和真实插件一样：用全部原始日线（含占位行）和事件，由 SDK 计算累计因子
        bars = pl.DataFrame(BARS, schema=BARS_SCHEMA, orient="row")
        events = pl.DataFrame(EVENTS, schema=EX_EVENTS_SCHEMA, orient="row")
        factors = factors_from_events(bars, events)
        return factors.filter(
            pl.col("symbol").is_in(list(symbols)) & pl.col("date").is_between(start, end)
        )

    def index_constituents(self, index, start, end):
        raise NotImplementedError


source = Source()
"""


def _inputs() -> dict[str, Any]:
    iso = date.isoformat
    return {
        "calendar": [iso(x) for x in DAYS],
        "securities": [
            [s, n, ex, k, None if ld is None else iso(ld), None if dd is None else iso(dd)]
            for s, n, ex, k, ld, dd in SECURITIES
        ],
        "bars": [[iso(r[0]), *r[1:]] for r in ALL_BARS],
        "events": [[iso(DAYS[i]), s, c, b, r, p] for s, i, c, b, r, p in EVENTS],
    }


def sync_golden(tmp_path: Path) -> tuple[DataStore, SyncResult]:
    settings = Settings(home=tmp_path / "home")
    plugin = settings.plugin_dir / "golden-src"
    plugin.mkdir(parents=True)
    (plugin / "duohelm-plugin.toml").write_text(MANIFEST, encoding="utf-8")
    (plugin / "golden_src.py").write_text(textwrap.dedent(PLUGIN_CODE), encoding="utf-8")
    (plugin / "inputs.json").write_text(json.dumps(_inputs()), encoding="utf-8")

    store = TaskStore(settings.db_path)
    types = TaskTypeRegistry([data_sync(settings, limits=FAST)])
    body = {"plugin_id": "golden-src", "start": START.isoformat(), "end": END.isoformat()}
    task = store.create("data.sync", types.validate_params("data.sync", body), max_attempts=1)
    assert Worker(store, types).run_once()
    done = store.get(task.id)
    assert done.status == "succeeded", (done.error, [x.message for x in store.logs(task.id)])
    assert done.result is not None
    return DataStore(settings.data_dir), SyncResult.model_validate_json(done.result)


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> tuple[DataStore, SyncResult]:
    return sync_golden(tmp_path_factory.mktemp("golden"))


# ---- 断言 -----------------------------------------------------------------------------


def test_sync_counts(golden: tuple[DataStore, SyncResult]) -> None:
    _, result = golden
    assert result.securities == 10
    assert result.symbols == SYMBOLS_REQUESTED
    assert result.dropped_zero_volume == DROPPED
    assert result.written == {
        "securities": 10,
        "delist_date_quality": 10,
        "calendar": 10,
        "bars_1d": BARS_ROWS,
        "adj_factors": FACTOR_ROWS,
    }


def test_bars_stored_as_given(golden: tuple[DataStore, SyncResult]) -> None:
    """入库的是输入里成交量不为 0 的行，价格是未复权价，逐行一致；占位行和停牌日没有行。"""
    data, _ = golden
    bars = data.read("bars_1d")
    assert bars.height == BARS_ROWS == len(KEPT_BARS)
    assert sorted(bars.rows()) == sorted(KEPT_BARS)
    assert bars.filter(pl.col("volume") == 0).height == 0
    present = {(s, d) for d, s in bars.select("date", "symbol").rows()}
    assert ("600200.SH", D0) not in present  # 年初的占位行
    assert ("600500.SH", D4) not in present  # 停牌日没有行
    assert ("600500.SH", D5) not in present  # 停牌期间的占位行
    assert ("600600.SH", D3) not in present  # 上市前
    assert ("600600.SH", D6) in present  # 上市首日
    assert {d for d in range(10) if ("600800.SH", DAYS[d]) in present} == {
        0,
        1,
        2,
        3,
    }  # 退市股停牌后没有行


def test_bars_partitioned_by_year(golden: tuple[DataStore, SyncResult]) -> None:
    data, result = golden
    assert result.snapshot_id is not None
    files = data.manifest(result.snapshot_id).datasets["bars_1d"]
    assert {e.partition: e.rows for e in files} == {"2023": BARS_2023, "2024": BARS_2024}
    # 跨年读取：d2、d3 两天，各 7 只有行（新股、没有行的 600600.SH 除外；退市股 d2、d3 都有）
    across = data.read("bars_1d", start=D2, end=D3)
    assert across.height == 14
    assert across.get_column("date").unique().sort().to_list() == [D2, D3]


def test_calendar(golden: tuple[DataStore, SyncResult]) -> None:
    data, _ = golden
    calendar = data.read("calendar")
    assert calendar.get_column("date").to_list() == DAYS
    assert calendar.get_column("market").unique().to_list() == ["CN"]


def test_securities_and_delist_dates(golden: tuple[DataStore, SyncResult]) -> None:
    data, _ = golden
    securities = data.read("securities")
    assert securities.height == 10
    got = {r["symbol"]: (r["list_date"], r["delist_date"]) for r in securities.to_dicts()}
    assert got == SECURITIES_EXPECTED
    # 退市前停牌：退市日（d8）晚于最后一根 K 线（d3），不是最后一根 K 线的日期
    last_bar = data.read("bars_1d", symbols=["600800.SH"]).get_column("date").max()
    assert last_bar == D3
    assert got["600800.SH"][1] == D8
    assert got["600800.SH"][1] != last_bar
    # 退市日精确（插件声明 exact_delist_date=True）；在市的是 null
    quality = dict(data.read("delist_date_quality").rows())
    assert quality["600800.SH"] is True and quality["600900.SH"] is True
    assert quality["600100.SH"] is None


def test_adj_factors(golden: tuple[DataStore, SyncResult]) -> None:
    data, _ = golden
    factors = data.read("adj_factors")
    got = {(r["symbol"], r["date"]): r["factor"] for r in factors.to_dicts()}
    expected = {(s, d): f for s, rows in FACTORS.items() for d, f in rows}
    assert got.keys() == expected.keys()  # 指数、上市前的事件、退市股都没有因子
    for key, factor in expected.items():
        assert got[key] == pytest.approx(float(factor), rel=1e-12), key


def test_hfq_prices(golden: tuple[DataStore, SyncResult]) -> None:
    data, _ = golden
    adjusted = adjust_bars(data.read("bars_1d"), data.read("adj_factors"), mode="hfq")
    close = {(r["symbol"], r["date"]): r["close"] for r in adjusted.to_dicts()}
    for key, price in HFQ_CLOSE.items():
        assert close[key] == pytest.approx(float(price), rel=1e-12), key


def test_qfq_prices(golden: tuple[DataStore, SyncResult]) -> None:
    data, _ = golden
    bars, factors = data.read("bars_1d"), data.read("adj_factors")
    for base in {k[0] for k in QFQ_CLOSE}:
        adjusted = adjust_bars(bars, factors, mode="qfq", base_date=base)
        close = {(r["symbol"], r["date"]): r["close"] for r in adjusted.to_dicts()}
        for (b, symbol, day), price in QFQ_CLOSE.items():
            if b == base:
                assert close[(symbol, day)] == pytest.approx(float(price), rel=1e-12), (
                    b,
                    symbol,
                    day,
                )


def test_every_adjusted_row_matches_step_function(golden: tuple[DataStore, SyncResult]) -> None:
    """每一行的 open/high/low/close：与不依赖 SDK 的分数运算（原始价 × 手算因子阶梯函数）一致。"""
    data, _ = golden
    bars, factors = data.read("bars_1d"), data.read("adj_factors")
    base = D3
    hfq = adjust_bars(bars, factors, mode="hfq")
    qfq = adjust_bars(bars, factors, mode="qfq", base_date=base)
    raw = {(r[1], r[0]): r[2:6] for r in KEPT_BARS}
    for mode, frame in (("hfq", hfq), ("qfq", qfq)):
        assert frame.height == BARS_ROWS
        for row in frame.to_dicts():
            key = (row["symbol"], row["date"])
            scale = step_factor(*key)
            if mode == "qfq":
                scale = scale / step_factor(row["symbol"], base)
            for col, price in zip(("open", "high", "low", "close"), raw[key], strict=True):
                want = float(Fraction(price) * scale)
                assert row[col] == pytest.approx(want, rel=1e-12), (mode, key, col)
            # 成交量、成交额不调整
    assert hfq.get_column("volume").to_list() == bars.get_column("volume").to_list()
