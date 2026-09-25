# 智能辅具租赁目录与召回结算服务

本项目在产品分类、租赁价格和维护周期基础上，补充安全召回处置、替代履约与
补贴结算的领域服务，帮助社区辅具中心回答两个问题：

1. 召回发生后，**哪些使用者必须优先联系**、设备当前在谁手里；
2. 受影响期间的补贴**按实际可用天数结清**，替代设备不重复占用家庭额度。

样例数据均为设备与业务流程数据，不包含老人身份和健康信息。

## 领域规则

| 主题 | 规则 |
| --- | --- |
| 设备档案 | 每台设备记录序列号、型号版本、批次、维护状态、保管节点、交付关系与补贴口径（日租金 + 补贴比例） |
| 召回范围 | 按批次发起、可继续扩大；**缩小范围不自动解除人工隔离** |
| 联系任务 | 已交付设备生成通知 → 电话 → 上门三级联系任务，未联系上按宽限期升级，另有归还任务 |
| 替代履约 | 暂占 → 交付 → 归还全程与被召回设备 **1:1 绑定**；同一替代设备不可重复发放，家庭额度只扣一次，未交付可取消暂占 |
| 恢复流转 | 维修结论 + 复检签署双门禁；复检不过退回维修；报废永久退出；召回隔离与人工隔离须分别人工解除 |
| 补贴结算 | 按**实际可用天数**（交付日至召回通知前一日或归还前一日较早者）计算；已支付金额不覆盖，差额生成**下一期调整分录** |
| 可靠性 | 所有变更为不可变事件；命令带 `command_id` 幂等、离线回执按回执号去重、`replay()` 可从日志恢复进程，重放不重复发放替代设备或重复入账 |
| 追踪 | `trace_device(serial)` 汇总一台设备的召回决定、保管节点、联系结果、替代链路与金额变化 |

## 快速示例

```python
from datetime import date
from recall import RecallService, RepairOutcome

svc = RecallService.from_fixture("fixtures/devices.json")
svc.set_family_quota("F-1024", 1)

svc.open_recall("R-2026-009", "助行器刹车失效",
                ["WK-A2-2026-03"], date(2026, 9, 25))
svc.record_contact("AW-WK-2603-0007", False, date(2026, 9, 25))  # 自动升级电话
hold = svc.hold_replacement("R-2026-009", "AW-WK-2603-0007",
                            "AW-WK-2609-0103", date(2026, 9, 26))
svc.deliver_replacement(hold["link_id"], date(2026, 9, 27))
svc.accept_return("AW-WK-2603-0007", date(2026, 9, 30), "RC-20260930-07")
svc.return_replacement(hold["link_id"], date(2026, 10, 2))

svc.send_to_repair("AW-WK-2603-0007", date(2026, 10, 3))
svc.record_repair_conclusion("AW-WK-2603-0007", RepairOutcome.REPAIRED,
                             "更换刹车总成", "张工", date(2026, 10, 6))
svc.sign_recheck("AW-WK-2603-0007", "李检", True, date(2026, 10, 7))
svc.release_quarantine("AW-WK-2603-0007", "recall", date(2026, 10, 7))

svc.settle_period("F-1024", "AW-WK-2603-0007", "2026-09",
                  paid_amount=12000, on=date(2026, 10, 1))
```

进程恢复与幂等：

```python
restored = RecallService.replay(svc.journal())  # 崩溃后从事件日志重建
svc.save_journal("journal.json")                # 落盘归档
```

## 本地运行

测试命令：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q .
```

所有测试和构建均在单个 Linux 应用容器内完成，不需要数据库或外部服务；
事件日志即可作为持久化介质。
