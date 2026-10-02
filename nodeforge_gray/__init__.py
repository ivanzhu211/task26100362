"""NodeForge 策略灰度放量模块。"""

from __future__ import annotations

from .errors import (
    DeviceNotFoundError,
    GrayError,
    InvalidPercentError,
    PolicyNotFoundError,
    RolloutStateError,
)
from .manager import GrayRolloutManager, VersionSwitch
from .store import (
    STATUS_ACTIVE,
    STATUS_DECOMMISSIONED,
    STATUS_DISCOVERED,
    STATUS_MAINTENANCE,
)

__all__ = [
    "GrayRolloutManager",
    "VersionSwitch",
    "GrayError",
    "PolicyNotFoundError",
    "DeviceNotFoundError",
    "RolloutStateError",
    "InvalidPercentError",
    "STATUS_ACTIVE",
    "STATUS_MAINTENANCE",
    "STATUS_DECOMMISSIONED",
    "STATUS_DISCOVERED",
]
