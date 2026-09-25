# 智能辅具租赁目录

本项目保存社区智能辅具的产品分类、租赁价格和维护周期样例，为养老服务机构管理设备提供基础数据。

运行 `python -m unittest discover -s tests -v` 检查补贴比例边界。样例不包含老人身份和健康信息。

## 召回与替代履约（recall 包）

`recall` 包在目录数据之上提供安全召回与替代履约服务：

- **设备台账**：每台设备记录序列号、型号版本、维护状态、交付关系与补贴口径。
- **召回范围**：按批次扩大；缩小范围只解除召回锁定，人工隔离须工作人员显式解除。
- **联系任务**：已交付设备自动生成分级（P1–P3）联系与归还任务，联系不到自动升级。
- **替代履约**：替代设备暂占 → 交付 → 归还一一对应，不重复扣减家庭额度。
- **维修复检**：维修结论与复检签署齐全、锁定解除后才可恢复流转；无法修复的设备只能报废。
- **结算**：按实际可用天数结算；已入账（含已支付）期间的差额进入下一期调整，不覆盖旧账。
- **幂等与恢复**：所有变更操作接受幂等键，离线回执、重复通知、进程恢复不会重复发放替代设备或多算补贴。
- **追踪**：`device_history(serial)` 给出每台设备的召回决定、保管节点、联系结果与金额变化。

```python
from datetime import date
from recall import RecallService

svc = RecallService.open("state.json")  # 持久化恢复；幂等键随状态保存
svc.register_device("W-200", "walker-01", "v2", "B1", daily_price=10,
                    subsidy_rate=0.5, subsidy_cap=1000,
                    on=date(2026, 8, 1), operator="op")
svc.deliver_device("W-200", "fam-1", "loan-1", on=date(2026, 8, 1), operator="op")
svc.open_recall("R1", "walker-01", ["B1"], "轮轴裂纹",
                on=date(2026, 8, 11), operator="主管", idempotency_key="notify-1")
svc.reserve_substitute("R1", "loan-1", "S-1", on=date(2026, 8, 11), operator="op")
svc.deliver_substitute("R1:loan-1", on=date(2026, 8, 21), operator="op")
entries = svc.settle_period("2026-08", on=date(2026, 8, 31))  # 按可用天数入账
```

## 本地运行

测试命令：

```bash
python3 -m unittest discover -s tests
```

编译或构建命令：

```bash
python3 -m compileall -q .
```

所有测试和构建均在单个 Linux 应用容器内完成，不需要另行启动数据库或外部服务。
