"""SQLite 持久化层：建表与基础读写。

表结构：
- policies: 每条策略的稳定版本
- campaigns: 进行中的灰度（每条策略最多一行）
- devices: 设备及其状态
- current_assignment: 每台设备在每条策略上当前用的版本
- assignment_history: 版本切换流水，seq 自增保证同一时刻多个变动的先后顺序
"""
from __future__ import annotations

import sqlite3


class Storage:
    """封装本地 SQLite 文件存取。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS policies (
                policy_id TEXT PRIMARY KEY,
                stable_version TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS campaigns (
                policy_id TEXT PRIMARY KEY REFERENCES policies(policy_id),
                version TEXT NOT NULL,
                pct INTEGER NOT NULL,
                seed TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS current_assignment (
                device_id TEXT NOT NULL,
                policy_id TEXT NOT NULL,
                version TEXT NOT NULL,
                PRIMARY KEY (device_id, policy_id)
            );
            CREATE TABLE IF NOT EXISTS assignment_history (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                device_id TEXT NOT NULL,
                policy_id TEXT NOT NULL,
                version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_history_lookup
                ON assignment_history (device_id, policy_id, ts, seq);
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- policies ----

    def upsert_policy(self, policy_id: str, stable_version: str) -> None:
        self._conn.execute(
            "INSERT INTO policies (policy_id, stable_version) VALUES (?, ?) "
            "ON CONFLICT(policy_id) DO UPDATE SET stable_version = excluded.stable_version",
            (policy_id, stable_version),
        )

    def get_policy(self, policy_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM policies WHERE policy_id = ?", (policy_id,)
        ).fetchone()

    def list_policies(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM policies").fetchall()

    # ---- campaigns ----

    def set_campaign(self, policy_id: str, version: str, pct: int, seed: str) -> None:
        self._conn.execute(
            "INSERT INTO campaigns (policy_id, version, pct, seed) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(policy_id) DO UPDATE SET version = excluded.version, "
            "pct = excluded.pct, seed = excluded.seed",
            (policy_id, version, pct, seed),
        )

    def get_campaign(self, policy_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM campaigns WHERE policy_id = ?", (policy_id,)
        ).fetchone()

    def delete_campaign(self, policy_id: str) -> None:
        self._conn.execute("DELETE FROM campaigns WHERE policy_id = ?", (policy_id,))

    # ---- devices ----

    def upsert_device(self, device_id: str, status: str) -> None:
        self._conn.execute(
            "INSERT INTO devices (device_id, status) VALUES (?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET status = excluded.status",
            (device_id, status),
        )

    def get_device(self, device_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()

    def list_devices(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM devices").fetchall()

    # ---- current assignment ----

    def get_current(self, device_id: str, policy_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT version FROM current_assignment WHERE device_id = ? AND policy_id = ?",
            (device_id, policy_id),
        ).fetchone()
        return row["version"] if row else None

    def set_current(self, device_id: str, policy_id: str, version: str) -> None:
        self._conn.execute(
            "INSERT INTO current_assignment (device_id, policy_id, version) VALUES (?, ?, ?) "
            "ON CONFLICT(device_id, policy_id) DO UPDATE SET version = excluded.version",
            (device_id, policy_id, version),
        )

    def current_for_policy(self, policy_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM current_assignment WHERE policy_id = ?", (policy_id,)
        ).fetchall()

    # ---- history ----

    def append_history(self, ts: int, device_id: str, policy_id: str, version: str) -> None:
        self._conn.execute(
            "INSERT INTO assignment_history (ts, device_id, policy_id, version) VALUES (?, ?, ?, ?)",
            (ts, device_id, policy_id, version),
        )

    def version_at(self, device_id: str, policy_id: str, ts: int) -> str | None:
        row = self._conn.execute(
            "SELECT version FROM assignment_history "
            "WHERE device_id = ? AND policy_id = ? AND ts <= ? "
            "ORDER BY ts DESC, seq DESC LIMIT 1",
            (device_id, policy_id, ts),
        ).fetchone()
        return row["version"] if row else None

    def commit(self) -> None:
        self._conn.commit()
