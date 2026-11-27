"""data.cross_check：两个假行情源插件的交叉核对（#32）。不访问网络。"""

from __future__ import annotations

import json
import textwrap
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from duohelm.isolation import Limits
from duohelm.settings import Settings
from duohelm.storage import DataStore
from duohelm.tasks import TaskStore, TaskTypeRegistry, Worker, cross_check
from duohelm.tasks.builtin import builtin_task_types
from duohelm.tasks.cross_check import CrossCheckResult, data_cross_check

FAST = Limits(call_timeout_s=60, startup_timeout_s=60, poll_interval_s=0.1)
DAYS = [date(2024, 1, d) for d in (2, 3, 4, 5, 8, 9, 10)]
START, END = DAYS[0], DAYS[-1]

MANIFEST = """
[plugin]
id = "{id}"
name = "{id}"
version = "1.0.0"
kind = "datasource"
sdk = ">=0.4,<0.5"
entrypoint = "{module}:source"
"""

PLUGIN_CODE = """
import json
from datetime import date
from pathlib import Path

import polars as pl
from duohelm_sdk import Capabilities, ConnectionStatus, Freq
from duohelm_sdk.schema import ADJ_FACTORS_SCHEMA, BARS_SCHEMA, SECURITIES_SCHEMA

D = json.loads((Path(__file__).parent / "data.json").read_text(encoding="utf-8"))


def day(text):
    return date.fromisoformat(text) if text else None


class Source:
    def capabilities(self):
        if D.get("caps_fail"):
            raise RuntimeError("capabilities 出错")
        return Capabilities(
            markets=("CN",), freqs=(Freq.DAY,),
            fields=("open", "high", "low", "close", "volume", "amount"),
            lists_delisted=D.get("lists_delisted", True), exact_delist_date=True,
        )

    def check_connection(self):
        return ConnectionStatus(ok=not D.get("down"), message="连接不上" if D.get("down") else "ok")

    def securities(self, *, include_delisted=False):
        rows = [(s, s, s[-2:], k, day(l), day(dl)) for s, k, l, dl in D["securities"]]
        if not include_delisted:
            rows = [r for r in rows if r[5] is None]
        return pl.DataFrame(rows, schema=SECURITIES_SCHEMA, orient="row")

    def lookup_securities(self, symbols):
        return self.securities(include_delisted=True).filter(pl.col("symbol").is_in(symbols))

    def index_constituents(self, index, start, end):
        raise NotImplementedError

    def trading_calendar(self, market, start, end):
        return pl.DataFrame({"date": []}, schema={"date": pl.Date})

    def bars(self, symbols, freq, start, end):
        bad = set(D.get("bars_raise", []))
        if bad & set(symbols):
            raise RuntimeError("bars 出错")
        rows = [
            (day(d), s, o, h, l, c, v, None if v is None or c is None else v * c)
            for s, d, o, h, l, c, v in D["bars"]
            if s in symbols and start <= day(d) <= end
        ]
        if D.get("bars_extra"):
            rows.append((date(2024, 1, 2), "999999.SH", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0))
        if D.get("bars_outside"):
            rows.append((date(2024, 2, 1), symbols[0], 1.0, 1.0, 1.0, 1.0, 1.0, 1.0))
        return pl.DataFrame(rows, schema=BARS_SCHEMA, orient="row")

    def adj_factors(self, symbols, start, end):
        rows = [(day(d), s, f) for s, d, f in D.get("factors", []) if s in symbols]
        if D.get("factor_bad"):
            rows.append((date(2024, 1, 2), symbols[0], -1.0))
        return pl.DataFrame(rows, schema=ADJ_FACTORS_SCHEMA, orient="row")


source = Source()
"""

SECURITIES = [
    ["000001.SH", "index", "1990-12-19", None],
    ["000001.SZ", "stock", "1991-04-03", None],
    ["600000.SH", "stock", "1999-11-10", None],
]


def make_bars(symbol: str, base: float, volume: float = 1000.0) -> list[list[Any]]:
    return [
        [symbol, d.isoformat(), base, base + 1, base - 1, base + 0.5 + i * 0.1, volume + i]
        for i, d in enumerate(DAYS)
    ]


def base_data() -> dict[str, Any]:
    bars = make_bars("000001.SZ", 10.0) + make_bars("600000.SH", 8.0) + make_bars("000001.SH", 3000)
    return {
        "securities": SECURITIES,
        "bars": bars,
        "factors": [["600000.SH", "2020-06-01", 1.02], ["600000.SH", "2024-01-04", 1.071]],
    }


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


def install(settings: Settings, plugin_id: str, data: dict[str, Any]) -> None:
    module = plugin_id.replace("-", "_")
    path = settings.plugin_dir / plugin_id
    path.mkdir(parents=True)
    (path / "duohelm-plugin.toml").write_text(
        MANIFEST.format(id=plugin_id, module=module), encoding="utf-8"
    )
    (path / f"{module}.py").write_text(textwrap.dedent(PLUGIN_CODE), encoding="utf-8")
    (path / "data.json").write_text(json.dumps(data), encoding="utf-8")


def submit(settings: Settings, plugin_b: str = "src-b", **params: Any) -> tuple[TaskStore, int]:
    store = TaskStore(settings.db_path)
    types = TaskTypeRegistry([data_cross_check(settings, limits=FAST)])
    body = {
        "plugin_a": "src-a",
        "plugin_b": plugin_b,
        "start": START.isoformat(),
        "end": END.isoformat(),
        **params,
    }
    task = store.create(
        "data.cross_check", types.validate_params("data.cross_check", body), max_attempts=1
    )
    assert Worker(store, types).run_once()
    return store, task.id


def run(
    settings: Settings, a: dict[str, Any], b: dict[str, Any], **params: Any
) -> tuple[TaskStore, int]:
    install(settings, "src-a", a)
    install(settings, "src-b", b)
    return submit(settings, **params)


def check(
    settings: Settings, a: dict[str, Any], b: dict[str, Any], **params: Any
) -> CrossCheckResult:
    store, task_id = run(settings, a, b, **params)
    task = store.get(task_id)
    assert task.status == "succeeded", (task.error, [log.message for log in store.logs(task_id)])
    assert task.result is not None
    return CrossCheckResult.model_validate_json(task.result)


# ---- 一致、阈值、只在一边 ----


def test_identical_sources_have_no_differences_and_write_nothing(settings: Settings) -> None:
    data = base_data()
    result = check(settings, data, data)
    assert result.status == "compared" and result.complete
    assert result.symbols_compared == 3
    assert result.bars_compared == 3 * len(DAYS)
    assert result.counts == {} and result.examples == []
    # 只读：没有数据集、没有快照
    assert DataStore(settings.data_dir).head() is None


def test_differences_over_threshold_and_one_sided_dates(settings: Settings) -> None:
    a, b = base_data(), base_data()
    # 600000.SH：1 月 4 日 a 缺一行（漏数据）；1 月 5 日收盘价差 5%（超阈值）；
    # 1 月 8 日收盘价差 0.01%（阈值内）
    a["bars"] = [r for r in a["bars"] if not (r[0] == "600000.SH" and r[1] == "2024-01-04")]
    for r in b["bars"]:
        if r[0] == "600000.SH" and r[1] == "2024-01-05":
            r[5] *= 1.05
        if r[0] == "600000.SH" and r[1] == "2024-01-08":
            r[5] *= 1.0001
    # 000001.SZ：b 在 1 月 11 日多一行（日期错位）；a 在 1 月 3 日有成交量为 0 的占位行，b 没有
    b["bars"].append(["000001.SZ", "2024-01-11", 10, 11, 9, 10.5, 500])
    for r in a["bars"]:
        if r[0] == "000001.SZ" and r[1] == "2024-01-03":
            r[6] = 0
    b["bars"] = [r for r in b["bars"] if not (r[0] == "000001.SZ" and r[1] == "2024-01-03")]
    # 复权因子：2024-01-04 的因子 a 是 1.071，b 是 1.1；b 另有一个 2022-03-01 的除权日
    for r in b["factors"]:
        if r[1] == "2024-01-04":
            r[2] = 1.1
    b["factors"].append(["600000.SH", "2022-03-01", 1.05])
    result = check(settings, a, b, end="2024-01-11")

    assert result.status == "compared"
    assert result.counts["only_in_b"] == 2
    assert "only_in_a" not in result.counts
    by_key = {(d.kind, d.symbol, d.date): d for d in result.examples}
    assert ("only_in_b", "600000.SH", date(2024, 1, 4)) in by_key
    assert ("only_in_b", "000001.SZ", date(2024, 1, 11)) in by_key
    # 占位行不参与比较：1 月 3 日的 000001.SZ 既不是差异也不是只在一边
    assert not [
        d for d in result.examples if d.symbol == "000001.SZ" and d.date == date(2024, 1, 3)
    ]
    close = by_key[("bar_mismatch", "600000.SH", date(2024, 1, 5))]
    assert close.field == "close" and close.rel_diff is not None and close.rel_diff > 0.04
    assert result.counts["bar_mismatch"] == 1  # 阈值内的 0.01% 不算
    assert result.counts["bar_mismatch.close"] == 1
    # 复权因子：2022-03-01（b 独有的除权日，a 沿用 1.02）和 2024-01-04（值不同）
    factors = {d.date: d for d in result.examples if d.kind == "factor_mismatch"}
    assert set(factors) == {date(2022, 3, 1), date(2024, 1, 4)}
    only_b = factors[date(2022, 3, 1)]
    assert only_b.note == "这一天只有 b 有除权记录"
    assert (only_b.a, only_b.b) == (1.02, 1.05)
    assert result.symbols_with_differences == 2


def test_tolerance_parameters_are_honoured(settings: Settings, tmp_path: Path) -> None:
    a, b = base_data(), base_data()
    for r in b["bars"]:
        if r[0] == "600000.SH" and r[1] == "2024-01-05":
            r[5] *= 1.0001
    assert check(settings, a, b).counts == {}
    strict = Settings(home=tmp_path / "home2")
    assert check(strict, a, b, price_tolerance=0.00001).counts["bar_mismatch"] == 1


# ---- 成交量单位 ----


def test_volume_unit_mismatch_is_reported_once_per_symbol(settings: Settings) -> None:
    """b 的指数成交量是手（a 的 1/100）：报一条单位差异，不逐日列成交量差异。"""
    a, b = base_data(), base_data()
    for r in b["bars"]:
        if r[0] == "000001.SH":
            r[6] = r[6] / 100
    result = check(settings, a, b)
    assert result.counts == {"volume_unit_mismatch": 1}
    [diff] = result.examples
    assert diff.kind == "volume_unit_mismatch" and diff.symbol == "000001.SH"
    assert diff.date is None and diff.note is not None and "100" in diff.note


def test_ordinary_volume_difference_is_not_a_unit_mismatch(settings: Settings) -> None:
    a, b = base_data(), base_data()
    for r in b["bars"]:
        if r[0] == "000001.SZ" and r[1] == "2024-01-05":
            r[6] = r[6] * 100  # 只有一天差 100 倍：是数据差异，不是单位
    result = check(settings, a, b)
    assert result.counts == {"bar_mismatch": 1, "bar_mismatch.volume": 1}


# ---- 未知 ----


def test_failed_batch_is_unknown_not_no_difference(settings: Settings) -> None:
    a, b = base_data(), base_data()
    b["bars_raise"] = ["600000.SH"]  # b 读 600000.SH 所在的批次时出错
    result = check(settings, a, b)
    assert result.status == "unknown" and not result.complete
    assert result.symbols_compared == 0
    assert result.counts == {}  # 没有差异，但这不是“一致”
    assert result.unknown_symbols_total == 3
    assert {u.symbol for u in result.unknown_symbols} == {s[0] for s in SECURITIES}
    assert all(u.unknown_in == ["b"] for u in result.unknown_symbols)
    assert result.sources["b"].batch_errors == 1


def test_one_failed_batch_among_several_gives_partial(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = base_data(), base_data()
    b["bars_raise"] = ["600000.SH"]
    monkeypatch.setattr(cross_check, "BATCH_SIZE", 1)
    result = check(settings, a, b)
    assert result.status == "partial" and not result.complete
    assert result.symbols_compared == 2
    assert [u.symbol for u in result.unknown_symbols] == ["600000.SH"]


def test_source_down_is_unknown(settings: Settings) -> None:
    a, b = base_data(), base_data()
    b["down"] = True
    result = check(settings, a, b, symbols=[s[0] for s in SECURITIES])
    assert result.status == "unknown" and not result.complete
    assert result.sources["a"].ok and not result.sources["b"].ok
    assert result.sources["b"].error is not None and "连接不上" in result.sources["b"].error
    assert result.counts == {} and result.symbols_compared == 0
    assert result.unknown_symbols_total == 3


def test_source_down_without_symbols_is_unknown(settings: Settings) -> None:
    """没传 symbols，又有一边的证券列表拿不到：不能因此得出“没有差异”。"""
    a, b = base_data(), base_data()
    b["caps_fail"] = True
    result = check(settings, a, b)
    assert result.status == "unknown" and not result.complete
    assert not result.sources["b"].ok


# ---- 证券范围、样例上限 ----


def test_default_symbols_are_the_intersection(settings: Settings) -> None:
    a, b = base_data(), base_data()
    b["securities"] = [s for s in SECURITIES if s[0] != "000001.SH"]
    b["securities"].append(["600001.SH", "stock", "1998-01-22", "2009-12-25"])  # 区间前已退市
    a["securities"] = [*SECURITIES, ["600005.SH", "stock", "1999-08-03", None]]
    result = check(settings, a, b)
    assert result.symbols_requested == 2 and result.symbols_compared == 2
    assert result.only_listed_in_a == ["000001.SH", "600005.SH"]
    assert result.only_listed_in_a_total == 2 and result.only_listed_in_b_total == 0


def test_explicit_symbols_limit_the_comparison(settings: Settings, tmp_path: Path) -> None:
    a, b = base_data(), base_data()
    b["bars"] = [r for r in b["bars"] if r[0] != "600000.SH"]  # 整只证券 b 都没有
    only = check(settings, a, b, symbols=["000001.SZ"])
    assert only.symbols_requested == 1 and only.counts == {}
    other = Settings(home=tmp_path / "h2")
    both = check(other, a, b, symbols=["000001.SZ", "600000.SH"])
    assert both.counts == {"only_in_a": len(DAYS)}


def test_examples_are_capped_but_totals_are_kept(settings: Settings) -> None:
    a, b = base_data(), base_data()
    for r in b["bars"]:
        r[5] *= 1.01  # 所有行的收盘价都不同（相对差异 1%）
    result = check(settings, a, b, max_examples=2)
    assert result.counts["bar_mismatch"] == 3 * len(DAYS)
    assert len([d for d in result.examples if d.kind == "bar_mismatch"]) == 2
    assert result.examples_truncated
    assert result.symbols_with_differences == 3 and len(result.top_symbols) == 2


# ---- 参数和入口 ----


def test_params_are_validated(settings: Settings) -> None:
    types = TaskTypeRegistry([data_cross_check(settings)])
    ok = {"plugin_a": "x", "plugin_b": "y", "start": "2024-01-02"}
    types.validate_params("data.cross_check", ok)
    for bad in (
        {**ok, "plugin_b": "x"},
        {**ok, "end": "2023-01-01"},
        {**ok, "symbols": []},
        {**ok, "symbols": [" "]},
        {**ok, "price_tolerance": -1},
        {**ok, "unknown": 1},
    ):
        with pytest.raises(ValueError):
            types.validate_params("data.cross_check", bad)


def test_unclosed_end_fails_the_task(settings: Settings) -> None:
    store, task_id = run(settings, base_data(), base_data(), end="2999-01-01")
    task = store.get(task_id)
    assert task.status == "failed" and task.error is not None and "没有收盘" in task.error


def test_missing_plugin_fails_the_task(settings: Settings) -> None:
    install(settings, "src-a", base_data())
    store, task_id = submit(settings, plugin_b="nope")
    task = store.get(task_id)
    assert task.status == "failed" and task.error is not None and "nope" in task.error


def test_registered_as_builtin(settings: Settings) -> None:
    assert "data.cross_check" in {t.name for t in builtin_task_types(settings)}


# ---- 空值、NaN ----


def set_close(data: dict[str, Any], symbol: str, day: str, value: float | None) -> None:
    for r in data["bars"]:
        if r[0] == symbol and r[1] == day:
            r[5] = value


@pytest.mark.parametrize("empty", [None, float("nan")], ids=["null", "nan"])
def test_empty_field_is_unknown_not_consistent(settings: Settings, empty: float | None) -> None:
    """一边的收盘价为空（或 NaN）：这个字段不比较，计入 unknown_fields，这一行不算一致，
    也不算 bar_mismatch。"""
    a, b = base_data(), base_data()
    set_close(b, "600000.SH", "2024-01-05", empty)
    result = check(settings, a, b)
    assert result.status == "partial" and not result.complete  # 有字段没比较，不能说一致
    assert result.unknown_fields == {"close": 1}
    assert result.counts == {}
    assert result.bars_compared == 3 * len(DAYS)
    assert result.bars_consistent == 3 * len(DAYS) - 1
    # 结果能序列化成合法 JSON：没有 NaN 字面量
    task_json = result.model_dump_json()
    assert "NaN" not in task_json
    json.loads(task_json, parse_constant=_reject_constant)


def _reject_constant(name: str) -> None:
    raise AssertionError(f"JSON 里出现了 {name}")


def test_nan_does_not_hide_a_real_difference_elsewhere(settings: Settings) -> None:
    a, b = base_data(), base_data()
    set_close(a, "600000.SH", "2024-01-05", float("nan"))
    set_close(b, "600000.SH", "2024-01-08", 99.0)
    result = check(settings, a, b)
    assert result.unknown_fields == {"close": 1}
    assert result.counts == {"bar_mismatch": 1, "bar_mismatch.close": 1}
    assert result.bars_consistent == 3 * len(DAYS) - 2


# ---- 批次校验失败 ----


@pytest.mark.parametrize("flag", ["bars_extra", "bars_outside", "factor_bad"])
def test_batch_validation_failure_is_unknown(settings: Settings, flag: str) -> None:
    """插件返回了没请求的证券、区间外的日期、不合格的复权因子：这一批证券都记为未知。"""
    a, b = base_data(), base_data()
    b[flag] = True
    result = check(settings, a, b)
    assert result.status == "unknown" and not result.complete
    assert result.counts == {} and result.symbols_compared == 0
    assert result.unknown_symbols_total == 3
    assert all(u.unknown_in == ["b"] for u in result.unknown_symbols)
    assert result.sources["b"].batch_errors == 1


# ---- 样例的分配和顺序 ----


def test_examples_are_fair_across_fields_and_sorted(settings: Settings) -> None:
    a, b = base_data(), base_data()
    for r in b["bars"]:
        r[2] *= 1.01  # open 全部不同
        r[5] *= 1.01  # close 全部不同
    result = check(settings, a, b, max_examples=2)
    by_field: dict[str | None, int] = {}
    for d in result.examples:
        by_field[d.field] = by_field.get(d.field, 0) + 1
    assert by_field == {"open": 2, "close": 2}  # open 没有占满名额
    keys = [(d.kind, d.symbol, d.date, d.field) for d in result.examples]
    assert keys == sorted(keys, key=lambda k: (k[0], k[1], k[2], k[3] or ""))
    assert result.examples_truncated


def test_empty_intersection_is_unknown(settings: Settings) -> None:
    """两个数据源都正常，但证券列表的交集为空：没有比较任何证券，不能说“一致”。"""
    a, b = base_data(), base_data()
    a["securities"] = [SECURITIES[0]]
    b["securities"] = [SECURITIES[1], SECURITIES[2]]
    result = check(settings, a, b)
    assert result.sources["a"].ok and result.sources["b"].ok
    assert result.status == "unknown" and not result.complete
    assert result.symbols_requested == 0 and result.only_listed_in_a_total == 1


def test_both_sources_failing_keeps_both_reasons(settings: Settings) -> None:
    a, b = base_data(), base_data()
    a["bars_raise"] = ["600000.SH"]
    b["bars_extra"] = True
    result = check(settings, a, b)
    assert result.status == "unknown"
    for u in result.unknown_symbols:
        assert u.unknown_in == ["a", "b"]
        assert "数据源 a：" in u.reason and "bars 出错" in u.reason
        assert "数据源 b：" in u.reason and "没有请求的证券" in u.reason
