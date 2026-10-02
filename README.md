# NodeForge 策略灰度放量模块

纯 Python 库（无接口层），由 NodeForge 平台上层直接调用，数据持久化在本地 SQLite 文件中。

## 功能

- 每条策略任一时刻最多一个新版本灰度，从 5% 起，每次 ±5 个百分点（5%–100%）
- 随时全量（`promote`）或回滚（`rollback`）
- 只有在用设备参与灰度；维护/下架/未启用设备一律用稳定版本
- 灰度台数严格等于 `ceil(在用设备数 × 比例)`，在用设备数变化立即对齐
- 调高只增不减、调低只减不增；单台设备进出在用最多连带一台设备切换
- 基于哈希的稳定选择：设备离开又回来版本不变；调低再调回名单复原；
  不同策略、不同版本的灰度设备互不相关
- 每次版本切换都返回 `VersionSwitch` 列表，供上层真实下发配置
- 支持按时刻查询历史版本（`version_at`），与当时实时结果完全一致

## 用法

```python
from nodeforge_gray import GrayRolloutManager, STATUS_ACTIVE, STATUS_MAINTENANCE

m = GrayRolloutManager("nodeforge.db")
m.create_policy("alert-threshold", "v1.0", ts=1)
m.register_device("server-01", STATUS_ACTIVE, ts=2)

m.start_rollout("alert-threshold", "v1.1", ts=3)   # 5% 起灰
m.adjust_percent("alert-threshold", 20, ts=4)      # 调到 20%
m.set_device_status("server-01", STATUS_MAINTENANCE, ts=5)  # 上层通知进出在用

m.current_version("alert-threshold", "server-01")      # 当前版本
m.version_at("alert-threshold", "server-01", ts=4)     # 历史某时刻版本

m.promote("alert-threshold", ts=6)    # 全量：v1.1 成为稳定版
# 或 m.rollback("alert-threshold", ts=6)  # 回滚：全部回到稳定版
```

所有变更方法的 `ts` 由上层传入，必须按到达顺序不减（同一时刻允许多个变动）。

## 测试

```bash
python3 -m pytest tests/ -q
```
