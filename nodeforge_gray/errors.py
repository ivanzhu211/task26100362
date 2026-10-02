"""异常定义。"""

from __future__ import annotations


class GrayError(Exception):
    """本模块所有业务异常的基类。"""


class PolicyNotFoundError(GrayError):
    """策略不存在。"""


class DeviceNotFoundError(GrayError):
    """设备不存在。"""


class RolloutStateError(GrayError):
    """灰度状态不允许当前操作（如重复开启灰度）。"""


class InvalidPercentError(GrayError):
    """灰度比例不合法（非 5 的倍数或超出 5-100）。"""
