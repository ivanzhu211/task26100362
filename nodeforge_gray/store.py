"""SQLite 持久化层：表结构与底层读写。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

# 设备状态：只有在用设备参与灰度，其余一律使用稳定版本。
STATUS_ACTIVE = "active"            # 在用
STATUS_MAINTENANCE = "maintenance"  # 进维护
STATUS_DECOMMISSIONED = "decommissioned"  # 已下架
STATUS_DISCOVERED = "discovered"    # 刚发现、未启用

ALL_STATUSES = (
    STATUS_ACTIVE,
    STATUS_MAINTENANCE,
    STATUS_DECOMMISSIONED,
    STATUS_DISCOVERED,
)


class Store:
    """封装 SQLite 连接与表结构。"""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS policies (
                policy_id     TEXT PRIMARY KEY,
                stable_version TEXT NOT NULL,
                gray_version  TEXT,
                gray_percent  INTEGER
            );

            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                status    TEXT NOT NULL
            );

            -- 当前灰度名单（仅灰度进行中的策略有行）
            CREATE TABLE IF NOT EXISTS gray_members (
                policy_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                PRIMARY KEY (policy_id, device_id)
            );

            -- 设备有效版本变更历史；自增 id 即变动到达顺序
            CREATE TABLE IF NOT EXISTS assignments (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        INTEGER NOT NULL,
                policy_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                version   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_assignments_lookup
                ON assignments (policy_id, device_id, ts, id);

            -- 每个变动处理完后的策略状态快照，供历史查询回退
            CREATE TABLE IF NOT EXISTS policy_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            INTEGER NOT NULL,
                policy_id     TEXT NOT NULL,
                stable_version TEXT NOT NULL,
                gray_version  TEXT,
                gray_percent  INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_policy_history_lookup
                ON policy_history (policy_id, ts, id);
            """
        )
        self._conn.commit()
