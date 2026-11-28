"""数据源配置：每个数据类别的主数据源和可选的备用数据源（#99）。

设计见 docs/subsystems/data-sources.md 的“配置”。保存在 ``DUOHELM_HOME/plugin-config.db`` 的
``data_source_config`` 表（该库的 v2，迁移见 :func:`duohelm.endpoints.migrate_plugin_config`），
是用户配置，不进任何仓库，不随快照走（快照里另有 ``source_config`` 数据集记录写入时的配置）。

这里只做存储和存储层面的约束（类别认识、主数据源不为空、备用不和主数据源相同）。插件是否
``ready``、是否声明了这个类别，由调用方（API）校验。**没有任何行就是“没有配置”**，同步按
现在的单数据源行为运行。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from duohelm.endpoints import BUSY_TIMEOUT_MS, migrate_plugin_config
from duohelm.storage.sources import CATEGORIES
from duohelm.tasks.store import Clock, now_ms

logger = logging.getLogger(__name__)


class DataSourceConfigError(ValueError):
    """配置不成立。消息可直接展示给用户。"""


@dataclass(frozen=True)
class CategoryConfig:
    primary: str
    backup: str | None
    updated_at: datetime


class DataSourceConfigStore:
    def __init__(self, path: Path, clock: Clock = now_ms) -> None:
        self._path = path
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            migrate_plugin_config(conn, path)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def get(self) -> dict[str, CategoryConfig]:
        """当前配置（类别 -> 主、备）。空字典表示没有配置。不认识的类别跳过并记日志。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM data_source_config ORDER BY category").fetchall()
        result: dict[str, CategoryConfig] = {}
        for r in rows:
            if r["category"] not in CATEGORIES:
                logger.warning("数据源配置里有不认识的类别 %r，跳过", r["category"])
                continue
            result[r["category"]] = CategoryConfig(
                r["primary_plugin"],
                r["backup_plugin"],
                datetime.fromtimestamp(r["updated_at"] / 1000, tz=UTC),
            )
        return result

    def put(self, config: Mapping[str, tuple[str, str | None]]) -> dict[str, CategoryConfig]:
        """整体替换配置（类别 -> (主数据源, 备用数据源或 None)）。空的 config 即清空配置。

        内容没变的类别不改 ``updated_at``。
        """
        for category, (primary, backup) in config.items():
            if category not in CATEGORIES:
                raise DataSourceConfigError(
                    f"未知的数据类别：{category!r}，应为 {list(CATEGORIES)} 之一"
                )
            if not primary:
                raise DataSourceConfigError(f"{category} 必须配置主数据源")
            if backup == primary:
                raise DataSourceConfigError(f"{category} 的备用数据源不能和主数据源是同一个插件")
            if backup == "":
                raise DataSourceConfigError(
                    f"{category} 的备用数据源不能是空字符串，没有备用请留空"
                )
        now = self._clock()
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM data_source_config WHERE category NOT IN "
                f"({', '.join('?' * len(config))})",
                tuple(config),
            )
            for category, (primary, backup) in config.items():
                conn.execute(
                    """
                    INSERT INTO data_source_config
                      (category, primary_plugin, backup_plugin, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (category) DO UPDATE SET
                      primary_plugin = excluded.primary_plugin,
                      backup_plugin = excluded.backup_plugin,
                      updated_at = excluded.updated_at
                    WHERE primary_plugin IS NOT excluded.primary_plugin
                       OR backup_plugin IS NOT excluded.backup_plugin
                    """,
                    (category, primary, backup, now),
                )
        logger.info("数据源配置已保存（%d 个类别）", len(config))
        return self.get()
