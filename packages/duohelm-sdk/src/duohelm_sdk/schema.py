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
