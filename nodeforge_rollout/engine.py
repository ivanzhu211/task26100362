"""灰度放量引擎：策略版本灰度、设备状态通知、版本查询。

核心机制：每次灰度（campaign）生成一个随机种子，设备灰度键为
HMAC-SHA256(seed, device_id)。灰度名单 = 在用设备中按键最小的
ceil(n * pct / 100) 台。任何变动后按此重算期望版本并与当前版本
diff，差异写入历史流水。该机制天然满足：
- 调高只增、调低只减（top-k 性质）；
- 调低再调回、设备离开再回来，名单可精确复原（键不变）；
- 单台设备进出最多连带一台切换（top-k 的相邻性）；
- 不同策略 / 不同版本的灰度互不相关（种子独立）。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from .storage import Storage
from .types import MAX_PCT, MIN_PCT, PCT_STEP, DeviceStatus


class RolloutError(Exception):
    """业务规则校验失败。"""


class RolloutEngine:
    """NodeForge 策略灰度放量库的主入口，由平台上层直接调用。"""

    def __init__(self, db_path: str) -> None:
        self._storage = Storage(db_path)

    def close(self) -> None:
        self._storage.close()

    def __enter__(self) -> "RolloutEngine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 上层调用入口 ----

    def register_policy(self, policy_id: str, stable_version: str, ts: int) -> None:
        """登记一条策略及其初始稳定版本。"""
        if self._storage.get_policy(policy_id) is not None:
            raise RolloutError(f"策略已存在: {policy_id}")
        self._storage.upsert_policy(policy_id, stable_version)
        self._recompute_policy(policy_id, ts)
        self._storage.commit()

    def notify_device_status(self, device_id: str, status: DeviceStatus, ts: int) -> None:
        """上层通知设备状态变化（新设备首次上报也走这里）。"""
        self._storage.upsert_device(device_id, status.value)
        for policy in self._storage.list_policies():
            self._recompute_policy(policy["policy_id"], ts)
        self._storage.commit()

    def start_rollout(self, policy_id: str, new_version: str, ts: int) -> None:
        """开启灰度：新版本从 5% 开始。同一策略同时最多一个灰度。"""
        policy = self._require_policy(policy_id)
        if self._storage.get_campaign(policy_id) is not None:
            raise RolloutError(f"策略已有进行中的灰度: {policy_id}")
        if new_version == policy["stable_version"]:
            raise RolloutError("灰度版本不能与稳定版本相同")
        self._storage.set_campaign(policy_id, new_version, MIN_PCT, secrets.token_hex(16))
        self._recompute_policy(policy_id, ts)
        self._storage.commit()

    def adjust_rollout(self, policy_id: str, new_pct: int, ts: int) -> None:
        """把灰度比例调高或调低 5 个百分点（5%~100%）。"""
        campaign = self._require_campaign(policy_id)
        if new_pct % PCT_STEP != 0 or not (MIN_PCT <= new_pct <= MAX_PCT):
            raise RolloutError(f"非法灰度比例: {new_pct}")
        if abs(new_pct - campaign["pct"]) != PCT_STEP:
            raise RolloutError(f"每次只能调整 {PCT_STEP} 个百分点: {campaign['pct']} -> {new_pct}")
        self._storage.set_campaign(policy_id, campaign["version"], new_pct, campaign["seed"])
        self._recompute_policy(policy_id, ts)
        self._storage.commit()

    def promote(self, policy_id: str, ts: int) -> None:
        """全量：灰度版本成为稳定版本，所有设备切换过去，灰度结束。"""
        campaign = self._require_campaign(policy_id)
        self._storage.upsert_policy(policy_id, campaign["version"])
        self._storage.delete_campaign(policy_id)
        self._recompute_policy(policy_id, ts)
        self._storage.commit()

    def rollback(self, policy_id: str, ts: int) -> None:
        """回滚：灰度结束，所有设备回到稳定版本。"""
        self._require_campaign(policy_id)
        self._storage.delete_campaign(policy_id)
        self._recompute_policy(policy_id, ts)
        self._storage.commit()

    # ---- 查询 ----

    def current_version(self, device_id: str, policy_id: str) -> str | None:
        """设备当前在该策略上用的版本；设备或策略未知时返回 None。"""
        return self._storage.get_current(device_id, policy_id)

    def version_at(self, device_id: str, policy_id: str, ts: int) -> str | None:
        """设备在过去某一时刻用的版本（含该时刻的全部变动）。"""
        return self._storage.version_at(device_id, policy_id, ts)

    # ---- 内部 ----

    def _require_policy(self, policy_id: str):
        policy = self._storage.get_policy(policy_id)
        if policy is None:
            raise RolloutError(f"未知策略: {policy_id}")
        return policy

    def _require_campaign(self, policy_id: str):
        self._require_policy(policy_id)
        campaign = self._storage.get_campaign(policy_id)
        if campaign is None:
            raise RolloutError(f"策略没有进行中的灰度: {policy_id}")
        return campaign

    @staticmethod
    def _gray_key(seed: str, device_id: str) -> bytes:
        return hmac.new(seed.encode(), device_id.encode(), hashlib.sha256).digest()

    def _desired_versions(self, policy_id: str) -> dict[str, str]:
        """按当前状态重算该策略下每台已知设备的期望版本。"""
        policy = self._require_policy(policy_id)
        stable = policy["stable_version"]
        campaign = self._storage.get_campaign(policy_id)
        devices = self._storage.list_devices()
        desired = {d["device_id"]: stable for d in devices}
        if campaign is not None:
            in_service = [
                d["device_id"] for d in devices if d["status"] == DeviceStatus.IN_SERVICE.value
            ]
            n = len(in_service)
            k = -(-n * campaign["pct"] // 100)  # 向上取整
            ranked = sorted(in_service, key=lambda d: self._gray_key(campaign["seed"], d))
            for device_id in ranked[:k]:
                desired[device_id] = campaign["version"]
        return desired

    def _recompute_policy(self, policy_id: str, ts: int) -> None:
        """把期望版本与当前版本 diff，变化的写入历史并更新当前版本。"""
        for device_id, version in self._desired_versions(policy_id).items():
            if self._storage.get_current(device_id, policy_id) != version:
                self._storage.append_history(ts, device_id, policy_id, version)
                self._storage.set_current(device_id, policy_id, version)
