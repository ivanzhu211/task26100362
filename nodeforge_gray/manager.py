"""策略灰度放量管理器：供 NodeForge 平台上层直接调用的库接口。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .errors import (
    DeviceNotFoundError,
    GrayError,
    InvalidPercentError,
    PolicyNotFoundError,
    RolloutStateError,
)
from .selection import select_gray_devices
from .store import ALL_STATUSES, STATUS_ACTIVE, Store

MIN_PERCENT = 5
MAX_PERCENT = 100
PERCENT_STEP = 5
START_PERCENT = 5


@dataclass(frozen=True)
class VersionSwitch:
    """一次版本切换（上层据此真实下发配置）。"""

    policy_id: str
    device_id: str
    old_version: str
    new_version: str


class GrayRolloutManager:
    """策略灰度放量主入口。"""

    def __init__(self, db_path: str | Path) -> None:
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._store.conn

    # ---- 内部工具 ----

    def _policy_row(self, policy_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM policies WHERE policy_id = ?", (policy_id,)
        ).fetchone()
        if row is None:
            raise PolicyNotFoundError(f"策略不存在: {policy_id}")
        return row

    def _device_row(self, device_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            raise DeviceNotFoundError(f"设备不存在: {device_id}")
        return row

    def _check_ts(self, ts: int) -> None:
        row = self._conn.execute(
            "SELECT MAX(ts) AS m FROM ("
            "  SELECT ts FROM assignments UNION ALL SELECT ts FROM policy_history"
            ")"
        ).fetchone()
        last = row["m"]
        if last is not None and ts < last:
            raise GrayError(f"变动时间戳必须按到达顺序不减: {ts} < {last}")

    def _active_device_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT device_id FROM devices WHERE status = ?", (STATUS_ACTIVE,)
        ).fetchall()
        return [r["device_id"] for r in rows]

    def _gray_set(self, policy_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT device_id FROM gray_members WHERE policy_id = ?", (policy_id,)
        ).fetchall()
        return {r["device_id"] for r in rows}

    def _rolling_policy_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT policy_id FROM policies WHERE gray_version IS NOT NULL"
        ).fetchall()
        return [r["policy_id"] for r in rows]

    def _record_assignments(
        self, ts: int, policy_id: str, changes: list[tuple[str, str]]
    ) -> None:
        self._conn.executemany(
            "INSERT INTO assignments (ts, policy_id, device_id, version)"
            " VALUES (?, ?, ?, ?)",
            [(ts, policy_id, d, v) for d, v in changes],
        )

    def _snapshot_policy(self, ts: int, policy_id: str) -> None:
        row = self._policy_row(policy_id)
        self._conn.execute(
            "INSERT INTO policy_history"
            " (ts, policy_id, stable_version, gray_version, gray_percent)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                ts,
                policy_id,
                row["stable_version"],
                row["gray_version"],
                row["gray_percent"],
            ),
        )

    def _reconcile(self, ts: int, policy_id: str) -> list[VersionSwitch]:
        """让灰度名单与当前在用设备数、当前比例对齐，返回版本切换列表。"""
        policy = self._policy_row(policy_id)
        gray_version = policy["gray_version"]
        stable_version = policy["stable_version"]
        active = self._active_device_ids()
        target_count = (len(active) * policy["gray_percent"] + 99) // 100
        target = select_gray_devices(policy_id, gray_version, active, target_count)
        current = self._gray_set(policy_id)

        added = sorted(target - current)
        removed = sorted(current - target)
        switches = [
            VersionSwitch(policy_id, d, stable_version, gray_version) for d in added
        ] + [
            VersionSwitch(policy_id, d, gray_version, stable_version) for d in removed
        ]

        self._conn.executemany(
            "INSERT INTO gray_members (policy_id, device_id) VALUES (?, ?)",
            [(policy_id, d) for d in added],
        )
        self._conn.executemany(
            "DELETE FROM gray_members WHERE policy_id = ? AND device_id = ?",
            [(policy_id, d) for d in removed],
        )
        self._record_assignments(
            ts, policy_id, [(s.device_id, s.new_version) for s in switches]
        )
        return switches

    # ---- 策略生命周期 ----

    def create_policy(self, policy_id: str, stable_version: str, ts: int) -> None:
        """登记一条策略及其初始稳定版本。"""
        self._check_ts(ts)
        with self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO policies"
                    " (policy_id, stable_version, gray_version, gray_percent)"
                    " VALUES (?, ?, NULL, NULL)",
                    (policy_id, stable_version),
                )
            except sqlite3.IntegrityError:
                raise RolloutStateError(f"策略已存在: {policy_id}") from None
            self._snapshot_policy(ts, policy_id)

    def start_rollout(self, policy_id: str, new_version: str, ts: int) -> list[VersionSwitch]:
        """开启灰度：新版本从 5% 开始放量。"""
        self._check_ts(ts)
        with self._conn:
            policy = self._policy_row(policy_id)
            if policy["gray_version"] is not None:
                raise RolloutStateError(f"策略 {policy_id} 已有灰度在进行")
            self._conn.execute(
                "UPDATE policies SET gray_version = ?, gray_percent = ?"
                " WHERE policy_id = ?",
                (new_version, START_PERCENT, policy_id),
            )
            switches = self._reconcile(ts, policy_id)
            self._snapshot_policy(ts, policy_id)
        return switches

    def adjust_percent(self, policy_id: str, percent: int, ts: int) -> list[VersionSwitch]:
        """把灰度比例调整到 percent（5 的倍数，5-100）。"""
        if (
            percent < MIN_PERCENT
            or percent > MAX_PERCENT
            or percent % PERCENT_STEP != 0
        ):
            raise InvalidPercentError(
                f"灰度比例必须是 {MIN_PERCENT}-{MAX_PERCENT} 之间"
                f" {PERCENT_STEP} 的倍数: {percent}"
            )
        self._check_ts(ts)
        with self._conn:
            policy = self._policy_row(policy_id)
            if policy["gray_version"] is None:
                raise RolloutStateError(f"策略 {policy_id} 当前没有灰度")
            self._conn.execute(
                "UPDATE policies SET gray_percent = ? WHERE policy_id = ?",
                (percent, policy_id),
            )
            switches = self._reconcile(ts, policy_id)
            self._snapshot_policy(ts, policy_id)
        return switches

    def promote(self, policy_id: str, ts: int) -> list[VersionSwitch]:
        """全量：灰度版本成为稳定版本，灰度结束。"""
        self._check_ts(ts)
        with self._conn:
            policy = self._policy_row(policy_id)
            if policy["gray_version"] is None:
                raise RolloutStateError(f"策略 {policy_id} 当前没有灰度")
            old_stable = policy["stable_version"]
            new_stable = policy["gray_version"]
            gray = self._gray_set(policy_id)

            switches: list[VersionSwitch] = []
            if new_stable != old_stable:
                rows = self._conn.execute("SELECT device_id FROM devices").fetchall()
                switches = [
                    VersionSwitch(policy_id, r["device_id"], old_stable, new_stable)
                    for r in rows
                    if r["device_id"] not in gray
                ]
            self._conn.execute(
                "UPDATE policies SET stable_version = ?, gray_version = NULL,"
                " gray_percent = NULL WHERE policy_id = ?",
                (new_stable, policy_id),
            )
            self._conn.execute(
                "DELETE FROM gray_members WHERE policy_id = ?", (policy_id,)
            )
            self._record_assignments(
                ts, policy_id, [(s.device_id, s.new_version) for s in switches]
            )
            self._snapshot_policy(ts, policy_id)
        return switches

    def rollback(self, policy_id: str, ts: int) -> list[VersionSwitch]:
        """回滚：灰度结束，所有设备回到稳定版本。"""
        self._check_ts(ts)
        with self._conn:
            policy = self._policy_row(policy_id)
            if policy["gray_version"] is None:
                raise RolloutStateError(f"策略 {policy_id} 当前没有灰度")
            gray_version = policy["gray_version"]
            stable_version = policy["stable_version"]
            gray = sorted(self._gray_set(policy_id))

            switches = [
                VersionSwitch(policy_id, d, gray_version, stable_version)
                for d in gray
            ]
            self._conn.execute(
                "UPDATE policies SET gray_version = NULL, gray_percent = NULL"
                " WHERE policy_id = ?",
                (policy_id,),
            )
            self._conn.execute(
                "DELETE FROM gray_members WHERE policy_id = ?", (policy_id,)
            )
            self._record_assignments(
                ts, policy_id, [(s.device_id, s.new_version) for s in switches]
            )
            self._snapshot_policy(ts, policy_id)
        return switches

    # ---- 设备生命周期 ----

    def register_device(self, device_id: str, status: str, ts: int) -> list[VersionSwitch]:
        """登记设备（初始状态由上层给定，如 discovered）。"""
        if status not in ALL_STATUSES:
            raise GrayError(f"未知设备状态: {status}")
        self._check_ts(ts)
        with self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO devices (device_id, status) VALUES (?, ?)",
                    (device_id, status),
                )
            except sqlite3.IntegrityError:
                raise RolloutStateError(f"设备已存在: {device_id}") from None
            switches = self._reconcile_all(ts) if status == STATUS_ACTIVE else []
        return switches

    def set_device_status(self, device_id: str, status: str, ts: int) -> list[VersionSwitch]:
        """上层通知设备状态变化（在用/维护/下架/发现）。"""
        if status not in ALL_STATUSES:
            raise GrayError(f"未知设备状态: {status}")
        self._check_ts(ts)
        with self._conn:
            device = self._device_row(device_id)
            if device["status"] == status:
                return []
            involves_active = STATUS_ACTIVE in (device["status"], status)
            self._conn.execute(
                "UPDATE devices SET status = ? WHERE device_id = ?",
                (status, device_id),
            )
            switches = self._reconcile_all(ts) if involves_active else []
        return switches

    def _reconcile_all(self, ts: int) -> list[VersionSwitch]:
        switches: list[VersionSwitch] = []
        for policy_id in self._rolling_policy_ids():
            switches.extend(self._reconcile(ts, policy_id))
        return switches

    # ---- 查询 ----

    def current_version(self, policy_id: str, device_id: str) -> str:
        """设备当前在某策略上使用的版本。"""
        policy = self._policy_row(policy_id)
        self._device_row(device_id)
        if policy["gray_version"] is not None and device_id in self._gray_set(policy_id):
            return policy["gray_version"]
        return policy["stable_version"]

    def version_at(self, policy_id: str, device_id: str, ts: int) -> str:
        """设备在过去某一时刻（该时刻全部变动处理完后）使用的版本。"""
        self._policy_row(policy_id)
        self._device_row(device_id)
        row = self._conn.execute(
            "SELECT version FROM assignments"
            " WHERE policy_id = ? AND device_id = ? AND ts <= ?"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            (policy_id, device_id, ts),
        ).fetchone()
        if row is not None:
            return row["version"]
        row = self._conn.execute(
            "SELECT stable_version FROM policy_history"
            " WHERE policy_id = ? AND ts <= ?"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            (policy_id, ts),
        ).fetchone()
        if row is None:
            raise GrayError(f"时刻 {ts} 时策略 {policy_id} 尚未创建")
        return row["stable_version"]

    def gray_devices(self, policy_id: str) -> set[str]:
        """当前灰度中的设备集合（主要用于核对与测试）。"""
        self._policy_row(policy_id)
        return self._gray_set(policy_id)
