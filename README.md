# nodeforge-rollout

NodeForge 配置平台的策略灰度放量库。由平台上层直接调用，无接口层；
所有状态持久化在本地 SQLite 文件中，重启后可继续。

## 用法

```python
from nodeforge_rollout import RolloutEngine, DeviceStatus

engine = RolloutEngine("rollout.db")

engine.register_policy("alarm-threshold", "v1", ts=1)      # 登记策略与稳定版本
engine.notify_device_status("dev-1", DeviceStatus.IN_SERVICE, ts=2)  # 设备进出在用

engine.start_rollout("alarm-threshold", "v2", ts=3)        # 开启灰度，从 5% 开始
engine.adjust_rollout("alarm-threshold", 10, ts=4)         # 每次调高/调低 5 个点
engine.promote("alarm-threshold", ts=5)                    # 全量：v2 成为稳定版本
# engine.rollback("alarm-threshold", ts=5)                 # 或回滚：全部回到稳定版本

engine.current_version("dev-1", "alarm-threshold")         # 当前版本
engine.version_at("dev-1", "alarm-threshold", ts=4)        # 历史任一时刻版本
```

规则要点：只有在用设备参与灰度；灰度台数 = ceil(在用数 × 比例)；
调高只增、调低只减；单台设备进出最多连带一台切换；调低再调回、
设备离开再回来名单精确复原；不同策略/版本的灰度名单互不相关
（每次灰度独立随机种子，按 HMAC(seed, device_id) 取 top-k）。

## 测试

```bash
python3 -m pytest tests/
```
