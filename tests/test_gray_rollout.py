"""策略灰度放量模块的自动化测试。"""

from __future__ import annotations

import random

import pytest

from nodeforge_gray import (
    STATUS_ACTIVE,
    STATUS_DECOMMISSIONED,
    STATUS_DISCOVERED,
    STATUS_MAINTENANCE,
    GrayError,
    GrayRolloutManager,
    InvalidPercentError,
    PolicyNotFoundError,
    RolloutStateError,
)


@pytest.fixture()
def mgr(tmp_path):
    m = GrayRolloutManager(tmp_path / "gray.db")
    yield m
    m.close()


def make_manager(tmp_path, n_devices=0, policy="p1", stable="v1"):
    m = GrayRolloutManager(tmp_path / "gray.db")
    m.create_policy(policy, stable, ts=0)
    for i in range(n_devices):
        m.register_device(f"d{i}", STATUS_ACTIVE, ts=0)
    return m


# ---- 基本生命周期 ----

def test_rollout_lifecycle(tmp_path):
    m = make_manager(tmp_path, n_devices=20)
    switches = m.start_rollout("p1", "v2", ts=1)
    assert len(m.gray_devices("p1")) == 1  # ceil(20 * 5%)
    assert len(switches) == 1
    assert switches[0].old_version == "v1" and switches[0].new_version == "v2"

    m.adjust_percent("p1", 50, ts=2)
    assert len(m.gray_devices("p1")) == 10

    switches = m.promote("p1", ts=3)
    assert len(m.gray_devices("p1")) == 0
    assert len(switches) == 10  # 其余 10 台从 v1 切到 v2
    for i in range(20):
        assert m.current_version("p1", f"d{i}") == "v2"
    m.close()


def test_rollback(tmp_path):
    m = make_manager(tmp_path, n_devices=20)
    m.start_rollout("p1", "v2", ts=1)
    m.adjust_percent("p1", 30, ts=2)
    switches = m.rollback("p1", ts=3)
    assert len(switches) == 6  # ceil(20*30%)=6 台回到稳定版
    assert all(s.new_version == "v1" for s in switches)
    assert len(m.gray_devices("p1")) == 0
    for i in range(20):
        assert m.current_version("p1", f"d{i}") == "v1"
    m.close()


def test_count_is_ceil(tmp_path):
    m = make_manager(tmp_path, n_devices=21)
    m.start_rollout("p1", "v2", ts=1)
    assert len(m.gray_devices("p1")) == 2  # ceil(21*0.05)=2
    m.adjust_percent("p1", 100, ts=2)
    assert len(m.gray_devices("p1")) == 21
    m.close()


# ---- 设备状态 ----

def test_non_active_devices_use_stable(tmp_path):
    m = make_manager(tmp_path, n_devices=0)
    m.register_device("a", STATUS_MAINTENANCE, ts=1)
    m.register_device("b", STATUS_DECOMMISSIONED, ts=1)
    m.register_device("c", STATUS_DISCOVERED, ts=1)
    m.start_rollout("p1", "v2", ts=2)
    m.adjust_percent("p1", 100, ts=3)
    assert len(m.gray_devices("p1")) == 0
    for d in ("a", "b", "c"):
        assert m.current_version("p1", d) == "v1"
    m.close()


def test_leave_and_return_keeps_version(tmp_path):
    m = make_manager(tmp_path, n_devices=20)
    m.start_rollout("p1", "v2", ts=1)
    m.adjust_percent("p1", 50, ts=2)
    before = m.gray_devices("p1")
    victim = sorted(before)[0]

    m.set_device_status(victim, STATUS_MAINTENANCE, ts=3)
    assert m.current_version("p1", victim) == "v1"
    m.set_device_status(victim, STATUS_ACTIVE, ts=4)
    assert m.current_version("p1", victim) == "v2"
    assert m.gray_devices("p1") == before
    m.close()


def test_decrease_then_restore_restores_gray_set(tmp_path):
    m = make_manager(tmp_path, n_devices=40)
    m.start_rollout("p1", "v2", ts=1)
    m.adjust_percent("p1", 60, ts=2)
    before = m.gray_devices("p1")
    m.adjust_percent("p1", 20, ts=3)
    assert m.gray_devices("p1") < before
    m.adjust_percent("p1", 60, ts=4)
    assert m.gray_devices("p1") == before
    m.close()


# ---- 参数与状态校验 ----

def test_invalid_percent(tmp_path):
    m = make_manager(tmp_path, n_devices=10)
    m.start_rollout("p1", "v2", ts=1)
    for bad in (0, 3, 7, 105, -5):
        with pytest.raises(InvalidPercentError):
            m.adjust_percent("p1", bad, ts=2)
    m.close()


def test_rollout_state_errors(tmp_path):
    m = make_manager(tmp_path, n_devices=10)
    with pytest.raises(RolloutStateError):
        m.adjust_percent("p1", 10, ts=1)
    with pytest.raises(RolloutStateError):
        m.rollback("p1", ts=1)
    with pytest.raises(RolloutStateError):
        m.promote("p1", ts=1)
    m.start_rollout("p1", "v2", ts=2)
    with pytest.raises(RolloutStateError):
        m.start_rollout("p1", "v3", ts=3)
    with pytest.raises(PolicyNotFoundError):
        m.start_rollout("nope", "v2", ts=4)
    with pytest.raises(GrayError):
        m.adjust_percent("p1", 10, ts=1)  # 时间戳回退
    m.close()


# ---- 独立性 ----

def test_independence_across_policies(tmp_path):
    m = GrayRolloutManager(tmp_path / "gray.db")
    m.create_policy("p1", "v1", ts=0)
    m.create_policy("p2", "w1", ts=0)
    for i in range(200):
        m.register_device(f"d{i}", STATUS_ACTIVE, ts=0)
    m.start_rollout("p1", "v2", ts=1)
    m.start_rollout("p2", "w2", ts=1)
    g1 = m.gray_devices("p1")
    g2 = m.gray_devices("p2")
    assert len(g1) == len(g2) == 10
    # 随机抽两批 10/200，期望重合 0.5，允许很宽的上界
    assert len(g1 & g2) <= 5
    m.close()


def test_independence_across_versions(tmp_path):
    m = make_manager(tmp_path, n_devices=200)
    m.start_rollout("p1", "v2", ts=1)
    m.adjust_percent("p1", 50, ts=2)
    first = m.gray_devices("p1")
    m.promote("p1", ts=3)
    m.start_rollout("p1", "v3", ts=4)
    m.adjust_percent("p1", 50, ts=5)
    second = m.gray_devices("p1")
    assert len(first) == len(second) == 100
    # 两批应明显不同，远不是同一批试验品
    assert len(first & second) < 80
    m.close()


# ---- 持久化与历史 ----

def test_persistence_across_reopen(tmp_path):
    m = make_manager(tmp_path, n_devices=20)
    m.start_rollout("p1", "v2", ts=1)
    gray = m.gray_devices("p1")
    m.close()

    m2 = GrayRolloutManager(tmp_path / "gray.db")
    assert m2.gray_devices("p1") == gray
    d = sorted(gray)[0]
    assert m2.current_version("p1", d) == "v2"
    assert m2.version_at("p1", d, 1) == "v2"
    m2.close()


def test_same_timestamp_multiple_events(tmp_path):
    m = make_manager(tmp_path, n_devices=20)
    m.start_rollout("p1", "v2", ts=5)
    m.adjust_percent("p1", 50, ts=5)  # 同一时刻多个变动
    m.set_device_status("d0", STATUS_MAINTENANCE, ts=5)
    assert len(m.gray_devices("p1")) == (19 * 50 + 99) // 100
    for i in range(20):
        v = m.current_version("p1", f"d{i}")
        assert m.version_at("p1", f"d{i}", 5) == v
    m.close()


# ---- 随机场景：进出 + 比例调整交替，逐步校验规则，最后逐时刻核对历史 ----

def test_random_scenario_with_history_replay(tmp_path):
    rng = random.Random(20261003)
    m = GrayRolloutManager(tmp_path / "gray.db")
    m.create_policy("p1", "v1", ts=0)

    n_devices = 60
    device_status = {}
    for i in range(n_devices):
        d = f"d{i}"
        status = STATUS_ACTIVE if rng.random() < 0.8 else STATUS_DISCOVERED
        m.register_device(d, status, ts=0)
        device_status[d] = status

    m.start_rollout("p1", "v2", ts=1)
    percent = 5

    def active_count():
        return sum(1 for s in device_status.values() if s == STATUS_ACTIVE)

    def expected_count():
        return (active_count() * percent + 99) // 100

    def snapshot_versions():
        return {d: m.current_version("p1", d) for d in device_status}

    ts = 1
    history = {}  # ts -> {device: version}，该时刻全部变动处理完后的实时结果
    history[ts] = snapshot_versions()
    prev_gray = m.gray_devices("p1")

    def check_count():
        assert len(m.gray_devices("p1")) == expected_count()

    # 预编排：进维护又回来、调低再调回，必须出现在场景里。
    # 预编排块内部没有其它设备进出，可用于严格比对"回来后版本不变"
    # 与"调低再调回后名单完全复原"。
    scripted = [
        ("status", "d0", STATUS_ACTIVE),  # 确保 d0 先回到在用
        ("status", "d0", STATUS_MAINTENANCE),
        ("status", "d0", STATUS_ACTIVE),
        ("adjust", 30),
        ("adjust", 15),  # 调低
        ("adjust", 30),  # 再调回原值
    ]
    script_at = 40  # 插入位置，块内下标 0..5

    ops = []
    for _ in range(120):
        if rng.random() < 0.5:
            d = f"d{rng.randrange(n_devices)}"
            cur = device_status[d]
            if cur == STATUS_ACTIVE:
                new = rng.choice([STATUS_MAINTENANCE, STATUS_DECOMMISSIONED])
            else:
                new = STATUS_ACTIVE
            ops.append(("status", d, new))
        else:
            choices = []
            if percent < 100:
                choices.append(percent + 5)
            if percent > 5:
                choices.append(percent - 5)
            ops.append(("adjust", rng.choice(choices)))
    # 把预编排插到中间，确保被覆盖
    ops[script_at:script_at] = scripted

    for idx, op in enumerate(ops):
        # 偶尔让两个变动共享同一时间戳
        if idx == 0 or rng.random() > 0.1:
            ts += 1
        before_versions = snapshot_versions()
        before_gray = set(prev_gray)

        if op[0] == "status":
            _, d, new = op
            if device_status[d] == new:
                history[ts] = snapshot_versions()
                continue
            m.set_device_status(d, new, ts)
            device_status[d] = new
            after_versions = snapshot_versions()
            # 规则：除它自己外，最多再带一台设备切换版本
            others = [x for x in device_status
                      if x != d and before_versions[x] != after_versions[x]]
            assert len(others) <= 1, f"ts={ts} {op}: 连带切换 {others}"
        else:
            _, new_percent = op
            m.adjust_percent("p1", new_percent, ts)
            after_gray = m.gray_devices("p1")
            if new_percent > percent:
                # 调高：已灰度的一台都不能退出
                assert before_gray <= after_gray
            elif new_percent < percent:
                # 调低：不在灰度的一台都不能进来
                assert after_gray <= before_gray
            percent = new_percent

        prev_gray = m.gray_devices("p1")
        check_count()
        history[ts] = snapshot_versions()

        # 预编排块的严格比对
        if idx == script_at + 1:
            assert device_status["d0"] == STATUS_MAINTENANCE
            scripted_version_before_leave = before_versions["d0"]
        if idx == script_at + 2:
            # 中间没有比例调整也没有其它设备进出，回来后版本不变
            assert m.current_version("p1", "d0") == scripted_version_before_leave
        if idx == script_at + 3:
            gray_before_decrease = set(prev_gray)
        if idx == script_at + 5:
            # 调低再调回原值，中间无设备进出，灰度名单完全复原
            assert prev_gray == gray_before_decrease

    # 最终台数仍对齐
    check_count()

    # 对过程中每个时间点做历史查询，与当时实时结果逐台一致
    for t, expected in history.items():
        for d, v in expected.items():
            got = m.version_at("p1", d, t)
            assert got == v, f"ts={t} device={d}: 历史={got} 实时={v}"

    m.close()
