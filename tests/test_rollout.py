"""灰度放量引擎的场景化测试。

主场景：随机交替进行设备进出和比例调整（含进维护又回来、调低再调回），
每步之后校验台数、只增不减/只减不增、连带切换三条规则；
最后对过程中每个时间点做历史查询，与当时记下的实时结果逐台比对。
"""
from __future__ import annotations

import random

import pytest

from nodeforge_rollout import DeviceStatus, RolloutEngine, RolloutError

POLICIES = ["policy-a", "policy-b"]
DEVICES = [f"dev-{i:03d}" for i in range(40)]
NON_SERVICE = [DeviceStatus.DISCOVERED, DeviceStatus.MAINTENANCE, DeviceStatus.DECOMMISSIONED]


def ceil_pct(n: int, pct: int) -> int:
    return -(-n * pct // 100)


class Sim:
    """驱动随机变动，维护实时快照与期望状态。"""

    def __init__(self, engine: RolloutEngine, seed: int) -> None:
        self.engine = engine
        self.rng = random.Random(seed)
        self.ts = 0
        self.stable: dict[str, str] = {}
        self.campaign_pct: dict[str, int | None] = {}
        self.campaign_version: dict[str, str | None] = {}
        self.device_status: dict[str, DeviceStatus] = {}
        self.version_seq = 0
        self.snapshots: list[tuple[int, dict[tuple[str, str], str]]] = []

    def next_ts(self, allow_same: bool = True) -> int:
        if not (allow_same and self.rng.random() < 0.2):
            self.ts += 1
        return self.ts

    def new_version(self) -> str:
        self.version_seq += 1
        return f"v{self.version_seq}"

    def snapshot(self) -> dict[tuple[str, str], str]:
        snap = {}
        for p in POLICIES:
            for d in DEVICES:
                snap[(p, d)] = self.engine.current_version(d, p)
        return snap

    def record(self, ts: int) -> None:
        self.snapshots.append((ts, self.snapshot()))

    def in_service_count(self) -> int:
        return sum(1 for s in self.device_status.values() if s == DeviceStatus.IN_SERVICE)

    def gray_set(self, policy: str) -> set[str]:
        version = self.campaign_version[policy]
        return {d for d in DEVICES if self.engine.current_version(d, policy) == version}

    def check_count_rule(self) -> None:
        for p in POLICIES:
            pct = self.campaign_pct[p]
            if pct is None:
                continue
            expected = ceil_pct(self.in_service_count(), pct)
            assert len(self.gray_set(p)) == expected, (
                f"{p}: 灰度台数 {len(self.gray_set(p))} != 期望 {expected}"
            )

    # ---- 各类变动 ----

    def change_device_status(self, device: str, status: DeviceStatus) -> None:
        ts = self.next_ts()
        before = self.snapshot()
        self.engine.notify_device_status(device, status, ts)
        self.device_status[device] = status
        after = self.snapshot()
        for p in POLICIES:
            switched = {d for (pp, d) in before if pp == p and before[(pp, d)] != after[(pp, d)]}
            switched.discard(device)
            assert len(switched) <= 1, (
                f"{p}: 设备 {device} 状态变为 {status.value} 时连带切换了 {switched}"
            )
        self.check_count_rule()
        self.record(ts)

    def start(self, policy: str) -> None:
        ts = self.next_ts()
        version = self.new_version()
        self.engine.start_rollout(policy, version, ts)
        self.campaign_pct[policy] = 5
        self.campaign_version[policy] = version
        self.check_count_rule()
        self.record(ts)

    def adjust(self, policy: str, delta: int) -> None:
        ts = self.next_ts()
        before_gray = self.gray_set(policy)
        old_pct = self.campaign_pct[policy]
        self.engine.adjust_rollout(policy, old_pct + delta, ts)
        self.campaign_pct[policy] = old_pct + delta
        after_gray = self.gray_set(policy)
        if delta > 0:
            assert before_gray <= after_gray, "调高比例时有设备退出灰度"
        else:
            assert after_gray <= before_gray, "调低比例时有新设备进入灰度"
        self.check_count_rule()
        self.record(ts)

    def promote(self, policy: str) -> None:
        ts = self.next_ts()
        new_stable = self.campaign_version[policy]
        self.engine.promote(policy, ts)
        self.stable[policy] = new_stable
        self.campaign_pct[policy] = None
        self.campaign_version[policy] = None
        for d in DEVICES:
            assert self.engine.current_version(d, policy) == new_stable
        self.record(ts)

    def rollback(self, policy: str) -> None:
        ts = self.next_ts()
        self.engine.rollback(policy, ts)
        self.campaign_pct[policy] = None
        self.campaign_version[policy] = None
        for d in DEVICES:
            assert self.engine.current_version(d, policy) == self.stable[policy]
        self.record(ts)


@pytest.fixture()
def sim(tmp_path) -> Sim:
    engine = RolloutEngine(str(tmp_path / "rollout.db"))
    sim = Sim(engine, seed=20261003)
    for p in POLICIES:
        version = sim.new_version()
        ts = sim.next_ts(allow_same=False)
        engine.register_policy(p, version, ts)
        sim.stable[p] = version
        sim.campaign_pct[p] = None
        sim.campaign_version[p] = None
        sim.record(ts)
    for d in DEVICES:
        sim.device_status[d] = DeviceStatus.DISCOVERED
        sim.change_device_status(d, DeviceStatus.DISCOVERED)
    yield sim
    engine.close()


def test_random_interleaved_scenario(sim: Sim) -> None:
    rng = sim.rng
    did_maintenance_roundtrip = False
    did_down_then_restore = False

    for step in range(400):
        active = [p for p in POLICIES if sim.campaign_pct[p] is not None]
        idle = [p for p in POLICIES if sim.campaign_pct[p] is None]
        choices = ["device"] * 5
        if idle:
            choices.append("start")
        if active:
            choices += ["adjust"] * 3 + ["promote", "rollback"]
        op = rng.choice(choices)

        if op == "device":
            device = rng.choice(DEVICES)
            current = sim.device_status[device]
            pool = [s for s in DeviceStatus if s != current]
            # 提高"进维护又回来"的出现率
            if current == DeviceStatus.MAINTENANCE and rng.random() < 0.7:
                new_status = DeviceStatus.IN_SERVICE
            else:
                new_status = rng.choice(pool)
            was_in_service = current == DeviceStatus.IN_SERVICE
            before = {p: sim.engine.current_version(device, p) for p in POLICIES}
            sim.change_device_status(device, new_status)
            if was_in_service and new_status == DeviceStatus.MAINTENANCE:
                # 进维护后立刻回来，中间无任何变动：版本必须和离开前一致
                sim.change_device_status(device, DeviceStatus.IN_SERVICE)
                for p in POLICIES:
                    assert sim.engine.current_version(device, p) == before[p]
                did_maintenance_roundtrip = True
        elif op == "start":
            sim.start(rng.choice(idle))
        elif op == "adjust":
            policy = rng.choice(active)
            pct = sim.campaign_pct[policy]
            deltas = [d for d in (-5, 5) if 5 <= pct + d <= 100]
            if not deltas:
                continue
            delta = rng.choice(deltas)
            if delta < 0 and pct - 5 >= 5:
                # 调低再调回，中间无设备进出：灰度名单必须完全复原
                before_gray = sim.gray_set(policy)
                sim.adjust(policy, -5)
                sim.adjust(policy, +5)
                assert sim.gray_set(policy) == before_gray, "调低再调回后灰度名单未复原"
                did_down_then_restore = True
            else:
                sim.adjust(policy, delta)
        elif op == "promote":
            sim.promote(rng.choice(active))
        elif op == "rollback":
            sim.rollback(rng.choice(active))

    assert did_maintenance_roundtrip, "场景中未覆盖进维护又回来"
    assert did_down_then_restore, "场景中未覆盖调低再调回"

    # ---- 历史回放：每个时间点的查询结果必须与当时实时快照逐台一致 ----
    last_snap_at: dict[int, dict[tuple[str, str], str]] = {}
    for ts, snap in sim.snapshots:
        last_snap_at[ts] = snap
    for ts, snap in last_snap_at.items():
        for (policy, device), version in snap.items():
            got = sim.engine.version_at(device, policy, ts)
            assert got == version, (
                f"历史不一致: ts={ts} {policy}/{device} 期望 {version} 实得 {got}"
            )


def test_validation_errors(tmp_path) -> None:
    engine = RolloutEngine(str(tmp_path / "t.db"))
    engine.register_policy("p", "v1", 1)
    with pytest.raises(RolloutError):
        engine.register_policy("p", "v1", 2)  # 重复登记
    with pytest.raises(RolloutError):
        engine.adjust_rollout("p", 10, 2)  # 没有进行中的灰度
    with pytest.raises(RolloutError):
        engine.start_rollout("unknown", "v2", 2)
    engine.start_rollout("p", "v2", 3)
    with pytest.raises(RolloutError):
        engine.start_rollout("p", "v3", 4)  # 同时只能一个灰度
    with pytest.raises(RolloutError):
        engine.adjust_rollout("p", 15, 4)  # 一次只能调 5 个点
    with pytest.raises(RolloutError):
        engine.adjust_rollout("p", 7, 4)  # 必须是 5 的倍数
    with pytest.raises(RolloutError):
        engine.start_rollout("p", "v2", 4)
    engine.rollback("p", 5)
    with pytest.raises(RolloutError):
        engine.rollback("p", 6)  # 灰度已结束
    engine.close()


def test_gray_sets_independent_across_policies(tmp_path) -> None:
    engine = RolloutEngine(str(tmp_path / "t.db"))
    engine.register_policy("p1", "a1", 1)
    engine.register_policy("p2", "b1", 1)
    devices = [f"d{i}" for i in range(200)]
    for d in devices:
        engine.notify_device_status(d, DeviceStatus.IN_SERVICE, 2)
    engine.start_rollout("p1", "a2", 3)
    engine.start_rollout("p2", "b2", 3)
    g1 = {d for d in devices if engine.current_version(d, "p1") == "a2"}
    g2 = {d for d in devices if engine.current_version(d, "p2") == "b2"}
    assert len(g1) == len(g2) == 10
    # 各自随机抽 10/200，期望重合 0.5 台；重合不应显著超标，也不应总是同一批
    assert len(g1 & g2) <= 4
    assert g1 != g2
    # 同一策略先后两次灰度，名单也应互不相关
    engine.rollback("p1", 4)
    engine.start_rollout("p1", "a3", 5)
    g3 = {d for d in devices if engine.current_version(d, "p1") == "a3"}
    assert len(g3) == 10 and g3 != g1
    engine.close()


def test_same_timestamp_multiple_changes(tmp_path) -> None:
    engine = RolloutEngine(str(tmp_path / "t.db"))
    engine.register_policy("p", "v1", 1)
    engine.notify_device_status("d1", DeviceStatus.IN_SERVICE, 2)
    engine.start_rollout("p", "v2", 3)
    # 同一时刻连续多个变动，历史查询以该时刻全部变动处理完为准
    engine.adjust_rollout("p", 10, 10)
    engine.adjust_rollout("p", 15, 10)
    engine.promote("p", 10)
    assert engine.version_at("d1", "p", 10) == "v2"
    assert engine.version_at("d1", "p", 2) == "v1"
    engine.close()


def test_persistence_across_reopen(tmp_path) -> None:
    db = str(tmp_path / "t.db")
    engine = RolloutEngine(db)
    engine.register_policy("p", "v1", 1)
    for i in range(10):
        engine.notify_device_status(f"d{i}", DeviceStatus.IN_SERVICE, 2)
    engine.start_rollout("p", "v2", 3)
    gray_before = {d for d in (f"d{i}" for i in range(10)) if engine.current_version(d, "p") == "v2"}
    engine.close()

    reopened = RolloutEngine(db)
    gray_after = {d for d in (f"d{i}" for i in range(10)) if reopened.current_version(d, "p") == "v2"}
    assert gray_before == gray_after and len(gray_after) == 1
    assert all(reopened.version_at(d, "p", 3) == reopened.current_version(d, "p") for d in gray_after)
    # 重开后灰度可继续调整，名单仍然只增不减
    reopened.adjust_rollout("p", 10, 4)
    assert gray_after <= {d for d in (f"d{i}" for i in range(10)) if reopened.current_version(d, "p") == "v2"}
    reopened.close()
