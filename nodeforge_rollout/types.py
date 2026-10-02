"""类型定义：设备状态、变动事件等。"""
from __future__ import annotations

import enum


class DeviceStatus(enum.Enum):
    """设备状态。只有在用设备参与灰度。"""

    DISCOVERED = "discovered"        # 刚发现，还没启用
    IN_SERVICE = "in_service"        # 在用
    MAINTENANCE = "maintenance"      # 进维护
    DECOMMISSIONED = "decommissioned"  # 已下架


# 灰度比例的最小步进（百分点）
PCT_STEP = 5
MIN_PCT = 5
MAX_PCT = 100
