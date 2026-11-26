"""退市股扫描：数据源不能直接列出退市证券时，按代码区间找出它们（#30）。

``capabilities().lists_delisted`` 为 False 的数据源（例如 eltdx），代码表只有在市证券。
只用代码表回测过去会有幸存者偏差，所以 ``data.sync`` 先用 ``lookup_securities`` 补齐：

1. 代码表里的证券记为“扫到过”。以后它退市、从代码表里消失，第 3 步会重新查询它
2. 扫描 :data:`SCAN_RANGES` 里还没扫过的代码，每批结束就记进目录库的 ``security_scan``
   （按数据源分开）。任务失败、取消后，下次从没扫过的代码接着扫
3. 扫到过、但现在不在代码表里的代码，每次同步都重新查询，取得最新的上市、退市日期。
   之前扫到过、这次查不到的，任务失败：查不到是“不知道”，不能当成没有这只证券

扫描完成后才返回证券列表，所以 ``data.sync`` 不会用没扫完的名单写快照。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import polars as pl

from duohelm.isolation.runner import PluginSession
from duohelm.storage import Catalog
from duohelm.tasks.types import TaskContext
from duohelm_sdk.checks import check_securities
from duohelm_sdk.schema import SECURITIES_SCHEMA

SCAN_RANGES: tuple[tuple[str, int, int], ...] = (
    ("SH", 600000, 605999),  # 沪市主板
    ("SH", 688000, 689999),  # 科创板
    ("SZ", 1, 4999),  # 深市主板（含原中小板 002、003、001）
    ("SZ", 300000, 301999),  # 创业板
    ("BJ", 920000, 920999),  # 北交所现用代码
)
"""扫描的代码区间（交易所、起、止，含两端）。

不含 B 股；北交所 43、83、87 开头的旧代码不扫，那些代码上的退市股属于已知缺口。
"""
SCAN_BATCH = 100
"""每次 lookup_securities 查询多少个代码。"""
MIN_LISTED_RATIO = 0.5
"""代码表的证券数少于扫到过的代码数的这个比例时，认为代码表不完整，任务失败。"""


class ScanError(Exception):
    """扫描结果不可信。不自动重试。"""


@dataclass(frozen=True)
class ScanResult:
    securities: pl.DataFrame
    """在市证券加上扫描找到的不在代码表里的证券。"""
    scanned: int
    """这次新扫描的代码数。"""
    rechecked: int
    """这次重新查询的、扫到过但不在代码表里的代码数。"""


def scan_symbols() -> list[str]:
    return [
        f"{code:06d}.{exchange}"
        for exchange, first, last in SCAN_RANGES
        for code in range(first, last + 1)
    ]


def discover_securities(
    ctx: TaskContext,
    session: PluginSession,
    catalog: Catalog,
    *,
    progress: Callable[[float, str], None],
) -> ScanResult:
    """返回完整的证券列表（含退市），扫描记录写进 catalog。progress 的进度为 0 到 1。"""
    source = session.entry.id
    listed = _checked(session.call("securities", include_delisted=False), "securities")
    listed_symbols = set(listed.get_column("symbol").to_list())
    # 代码表为空或比以前少了一大截，多半是数据源一时出错。这时往下走，在市证券会被
    # 当成“不在代码表里”去查询，结果不可信：“不知道”不能当成“没有”
    known = len(catalog.found_symbols(source))
    if not listed_symbols or len(listed_symbols) < MIN_LISTED_RATIO * known:
        raise ScanError(
            f"代码表只有 {len(listed_symbols)} 只证券，而这个数据源扫到过 {known} 个代码，"
            "代码表可能不完整，不做扫描"
        )
    catalog.record_scan(source, [(s, True) for s in sorted(listed_symbols)])

    pending = [s for s in catalog.unscanned(source, scan_symbols()) if s not in listed_symbols]
    ctx.log(f"退市股扫描：代码表 {len(listed_symbols)} 只；还没扫描的代码 {len(pending)} 个")
    found: list[pl.DataFrame] = []
    for i, batch in enumerate(_batches(pending)):
        ctx.check_cancelled()
        progress(0.9 * i * SCAN_BATCH / max(len(pending), 1), f"扫描代码：{i * SCAN_BATCH}")
        rows = _lookup(session, batch)
        hit = set(rows.get_column("symbol").to_list())
        catalog.record_scan(source, [(s, s in hit) for s in batch])
        found.append(rows)
    found_now = {s for df in found for s in df.get_column("symbol").to_list()}

    recheck = [
        s for s in catalog.found_symbols(source) if s not in listed_symbols and s not in found_now
    ]
    for i, batch in enumerate(_batches(recheck)):
        ctx.check_cancelled()
        progress(0.9 + 0.1 * i * SCAN_BATCH / max(len(recheck), 1), "重新查询退市证券")
        rows = _lookup(session, batch)
        missing = sorted(set(batch) - set(rows.get_column("symbol").to_list()))
        if missing:
            raise ScanError(
                f"{len(missing)} 个之前扫到过的代码这次查不到，例如 {missing[:5]}。"
                "查不到是“不知道”，不能当成没有这只证券"
            )
        found.append(rows)

    extra = pl.concat([pl.DataFrame(schema=SECURITIES_SCHEMA), *found])
    still_listed = extra.filter(pl.col("delist_date").is_null()).height
    if still_listed:
        ctx.log(f"有 {still_listed} 只不在代码表里的证券没有退市日，按在市处理", "warning")
    securities = pl.concat([listed, extra]).unique("symbol", keep="first").sort("symbol")
    ctx.log(
        f"退市股扫描：新扫描 {len(pending)} 个代码，找到 {len(found_now)} 只；"
        f"重新查询 {len(recheck)} 只"
    )
    return ScanResult(securities, scanned=len(pending), rechecked=len(recheck))


def _lookup(session: PluginSession, batch: Sequence[str]) -> pl.DataFrame:
    rows = _checked(session.call("lookup_securities", list(batch)), "lookup_securities")
    extra = sorted(set(rows.get_column("symbol").to_list()) - set(batch))
    if extra:
        raise ScanError(f"lookup_securities 返回了没有查询的代码，例如 {extra[:5]}")
    return rows


def _checked(value: object, method: str) -> pl.DataFrame:
    if not isinstance(value, pl.DataFrame):
        raise ScanError(f"{method} 应返回 DataFrame，实际为 {type(value).__name__}")
    result = check_securities(value)
    if not result.passed:
        raise ScanError(f"{method} 的证券列表不合格：{'；'.join(result.details)}")
    return value


def _batches(symbols: Sequence[str]) -> list[Sequence[str]]:
    return [symbols[i : i + SCAN_BATCH] for i in range(0, len(symbols), SCAN_BATCH)]


__all__ = [
    "MIN_LISTED_RATIO",
    "SCAN_BATCH",
    "SCAN_RANGES",
    "ScanError",
    "ScanResult",
    "discover_securities",
    "scan_symbols",
]
