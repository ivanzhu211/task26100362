"""灰度设备选择：基于哈希的稳定、互不相关的确定性选择。"""

from __future__ import annotations

import hashlib

_HASH_MODULUS = 1 << 64
_SEP = "\x1f"


def device_rank(policy_id: str, gray_version: str, device_id: str) -> float:
    """返回设备在某策略某灰度版本下的稳定随机位次，取值 [0, 1)。

    同一 (policy_id, gray_version, device_id) 永远返回相同值；
    不同策略、不同版本之间位次互不相关。
    """
    key = _SEP.join((policy_id, gray_version, device_id)).encode("utf-8")
    digest = hashlib.sha256(key).digest()
    return int.from_bytes(digest[:8], "big") / _HASH_MODULUS


def select_gray_devices(
    policy_id: str,
    gray_version: str,
    active_device_ids: list[str],
    count: int,
) -> set[str]:
    """从在用设备中按位次从小到大选出 count 台作为灰度设备。"""
    if count <= 0:
        return set()
    ranked = sorted(
        active_device_ids,
        key=lambda d: (device_rank(policy_id, gray_version, d), d),
    )
    return set(ranked[:count])
