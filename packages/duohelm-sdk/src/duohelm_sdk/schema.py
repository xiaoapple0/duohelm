"""数据标准格式。这是对插件作者的契约，改动需要维护者确认。"""

from __future__ import annotations

import polars as pl

BARS_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date(),
    "symbol": pl.String(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "amount": pl.Float64(),
}
"""日频未复权 K 线。"""

FACTOR_OUTPUT_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date(),
    "symbol": pl.String(),
    "value": pl.Float64(),
}
"""因子插件 compute() 的输出。"""

ADJ_FACTORS_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date(),
    "symbol": pl.String(),
    "factor": pl.Float64(),
}
"""复权因子（行情源插件 adj_factors() 的输出）。

``factor`` 是**累计后复权比例**，自 ``date``（除权除息日）当天起生效，直到下一行为止；
某证券第一行之前的因子视为 1.0。因子只由 ``date`` 及以前的除权事件和行情决定，
不得用之后的数据归一化（否则会引入未来函数）。

- 后复权价 = 未复权价 × factor(t)
- 以 T 日为基准的前复权价 = 未复权价 × factor(t) / factor(T)

前复权在使用时按基准日计算，见 :func:`duohelm_sdk.adjust.adjust_bars`。
"""

SECURITIES_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String(),
    "name": pl.String(),
    "exchange": pl.String(),
    "kind": pl.String(),
    "list_date": pl.Date(),
    "delist_date": pl.Date(),
}
"""证券列表（行情源插件 securities() / lookup_securities() 的输出）。

- ``exchange``：``SH`` / ``SZ`` / ``BJ``，必须与 ``symbol`` 的后缀一致
- ``kind``：``stock`` / ``index``
- ``list_date``：上市日（数据源能取到的第一根 K 线的日期也可以），未知时为 null
- ``delist_date``：退市日（最后一个交易日），仍在市时为 null
"""

EXCHANGES = ("SH", "SZ", "BJ")
SECURITY_KINDS = ("stock", "index")

KEY_COLUMNS = ("date", "symbol")


def schema_problems(df: pl.DataFrame, schema: dict[str, pl.DataType]) -> list[str]:
    """检查 df 是否符合 schema：列名与顺序、类型、主键非空且不重复。返回问题列表，空表示合格。"""
    expected = list(schema)
    if df.columns != expected:
        return [f"列应为 {expected}，实际为 {df.columns}"]

    problems = [
        f"列 {name} 的类型应为 {dtype}，实际为 {df.schema[name]}"
        for name, dtype in schema.items()
        if df.schema[name] != dtype
    ]
    if problems:
        return problems

    keys = [k for k in KEY_COLUMNS if k in schema]
    for key in keys:
        nulls = df.get_column(key).null_count()
        if nulls:
            problems.append(f"列 {key} 有 {nulls} 个空值")
    if keys:
        dup = df.select(pl.struct(keys).is_duplicated().sum()).item()
        if dup:
            problems.append(f"{tuple(keys)} 有 {dup} 行重复")
    return problems
