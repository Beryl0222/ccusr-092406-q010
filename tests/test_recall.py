import os
import tempfile
import unittest
from datetime import date

from recall import (
    DeviceStatus,
    EntryKind,
    Maintenance,
    RecallError,
    RecallService,
    SubState,
    TaskStatus,
)

OP = "op"
SUP = "主管"


class RecallServiceTest(unittest.TestCase):
    def setUp(self):
        self.svc = RecallService()
        self.svc.set_family_quota("fam-1", 1)
        self.svc.set_family_quota("fam-2", 10)
        day0 = date(2026, 1, 1)
        for serial, batch in [("W-100", "B1"), ("W-200", "B1"), ("W-300", "B2")]:
            self.svc.register_device(serial, "walker-01", "v2", batch,
                                     daily_price=10, subsidy_rate=0.5,
                                     subsidy_cap=1000, on=day0, operator=OP)
        self.svc.register_device("S-1", "walker-02", "v1", "C1",
                                 daily_price=12, subsidy_rate=0.5,
                                 subsidy_cap=1200, on=day0, operator=OP)

    def test_open_recall_holds_devices_and_grades_tasks(self):
        svc = self.svc
        svc.deliver_device("W-200", "fam-1", "loan-1",
                           on=date(2026, 2, 1), operator=OP)
        svc.open_recall("R1", "walker-01", ["B1"], "轮轴裂纹",
                        on=date(2026, 9, 1), operator=SUP)

        # 在库与已交付设备都被锁定，B2 批次不受影响
        self.assertEqual(svc.store.devices["W-100"].recall_holds, {"R1"})
        self.assertEqual(svc.store.devices["W-200"].recall_holds, {"R1"})
        self.assertEqual(svc.store.devices["W-300"].recall_holds, set())

        # 锁定设备禁止交付
        with self.assertRaises(RecallError):
            svc.deliver_device("W-100", "fam-2", "loan-2",
                               on=date(2026, 9, 1), operator=OP)

        # 已交付设备生成分级任务：交付超过 180 天为 P1；在库设备无任务
        task = svc.store.tasks["R1:W-200"]
        self.assertEqual(task.grade, "P1")
        self.assertEqual(task.family_id, "fam-1")
        self.assertNotIn("R1:W-100", svc.store.tasks)

    def test_scope_narrow_does_not_lift_manual_quarantine(self):
        svc = self.svc
        svc.deliver_device("W-300", "fam-2", "loan-2",
                           on=date(2026, 8, 25), operator=OP)
        svc.open_recall("R1", "walker-01", ["B1"], "轮轴裂纹",
                        on=date(2026, 9, 1), operator=SUP)
        svc.expand_recall("R1", ["B2"], on=date(2026, 9, 1), operator=SUP)

        device = svc.store.devices["W-300"]
        self.assertEqual(device.recall_holds, {"R1"})
        task = svc.store.tasks["R1:W-300"]
        self.assertEqual(task.grade, "P3")  # 交付 7 天

        svc.quarantine_device("W-300", "家属反映异响",
                              on=date(2026, 9, 2), operator=SUP)
        svc.narrow_recall("R1", ["B2"], on=date(2026, 9, 3), operator=SUP)

        # 召回锁定解除、任务取消，但人工隔离保留，设备仍被锁定
        self.assertEqual(device.recall_holds, set())
        self.assertTrue(device.manual_quarantine)
        self.assertTrue(device.blocked)
        self.assertEqual(task.status, TaskStatus.CANCELLED)
        loan = svc.store.loans["loan-2"]
        self.assertIsNone(loan.unusable_intervals[-1][1])  # 不可用区间未关闭

        # 只有工作人员显式解除人工隔离后才恢复可用
        svc.release_quarantine("W-300", on=date(2026, 9, 4), operator=SUP)
        self.assertFalse(device.blocked)
        self.assertEqual(loan.unusable_intervals[-1][1], date(2026, 9, 4))

    def test_duplicate_notification_and_offline_receipt_idempotent(self):
        svc = self.svc
        svc.deliver_device("W-200", "fam-1", "loan-1",
                           on=date(2026, 8, 1), operator=OP)
        day = date(2026, 9, 1)
        svc.open_recall("R1", "walker-01", ["B1"], "轮轴裂纹",
                        on=day, operator=SUP, idempotency_key="notify-1")
        # 重复通知：同一幂等键，不产生重复锁定与任务
        svc.open_recall("R1", "walker-01", ["B1"], "轮轴裂纹",
                        on=day, operator=SUP, idempotency_key="notify-1")
        holds = [e for e in svc.store.audit if e.action == "recall_hold_placed"]
        self.assertEqual(len(holds), 2)  # W-100、W-200 各一次
        self.assertEqual(len(svc.store.tasks), 1)

        # 离线回执补录：同一回执重复上报只记录一次
        svc.record_contact("R1:W-200", "UNREACHABLE", on=day, operator=OP,
                           idempotency_key="receipt-1")
        svc.record_contact("R1:W-200", "UNREACHABLE", on=day, operator=OP,
                           idempotency_key="receipt-1")
        task = svc.store.tasks["R1:W-200"]
        self.assertEqual(task.attempts, 1)
        self.assertEqual(task.grade, "P2")  # 联系不到，升级一级

    def test_substitution_one_to_one_and_family_quota(self):
        svc = self.svc
        svc.register_device("S-2", "walker-02", "v1", "C1",
                            daily_price=12, subsidy_rate=0.5, subsidy_cap=1200,
                            on=date(2026, 1, 1), operator=OP)
        svc.deliver_device("W-200", "fam-1", "loan-1",
                           on=date(2026, 8, 1), operator=OP)
        svc.deliver_device("W-300", "fam-2", "loan-2",
                           on=date(2026, 8, 1), operator=OP)
        svc.open_recall("R1", "walker-01", ["B1", "B2"], "轮轴裂纹",
                        on=date(2026, 9, 1), operator=SUP)

        sub = svc.reserve_substitute("R1", "loan-1", "S-1",
                                     on=date(2026, 9, 2), operator=OP)
        self.assertEqual(sub.state, SubState.RESERVED)
        self.assertEqual(svc.store.devices["S-1"].status, DeviceStatus.RESERVED)

        # 一一对应：同一履约不能重复暂占，同一替代设备不能被两户占用
        with self.assertRaises(RecallError):
            svc.reserve_substitute("R1", "loan-1", "S-2",
                                   on=date(2026, 9, 2), operator=OP)
        with self.assertRaises(RecallError):
            svc.reserve_substitute("R1", "loan-2", "S-1",
                                   on=date(2026, 9, 2), operator=OP)

        svc.deliver_substitute("R1:loan-1", on=date(2026, 9, 3), operator=OP)
        # 替代不新增履约，家庭额度不被重复扣减
        self.assertEqual(svc.family_usage("fam-1")["active_loans"], 1)
        with self.assertRaises(RecallError):
            svc.deliver_device("S-2", "fam-1", "loan-9",
                               on=date(2026, 9, 3), operator=OP)

        svc.record_return("R1:W-200", on=date(2026, 9, 4), operator=OP)
        svc.return_substitute("R1:loan-1", on=date(2026, 9, 10), operator=OP)
        self.assertEqual(svc.store.devices["S-1"].status, DeviceStatus.IN_STOCK)
        self.assertEqual(svc.store.loans["loan-1"].closed_on, date(2026, 9, 10))

    def test_repair_requires_conclusion_and_reinspection(self):
        svc = self.svc
        svc.deliver_device("W-200", "fam-1", "loan-1",
                           on=date(2026, 8, 1), operator=OP)
        svc.open_recall("R1", "walker-01", ["B1"], "轮轴裂纹",
                        on=date(2026, 8, 11), operator=SUP)
        svc.record_return("R1:W-200", on=date(2026, 8, 12), operator=OP)
        svc.send_to_repair("W-200", on=date(2026, 8, 13), operator=OP)

        with self.assertRaises(RecallError):  # 无维修结论
            svc.release_to_stock("W-200", on=date(2026, 8, 14), operator=OP)
        svc.record_repair_conclusion("W-200", "FIXED", "更换轮轴",
                                     on=date(2026, 8, 15), operator="技师")
        with self.assertRaises(RecallError):  # 未复检签署
            svc.release_to_stock("W-200", on=date(2026, 8, 16), operator=OP)
        svc.sign_reinspection("W-200", on=date(2026, 8, 17), operator="质检")
        with self.assertRaises(RecallError):  # 召回锁定未解除
            svc.release_to_stock("W-200", on=date(2026, 8, 18), operator=OP)

        svc.close_recall("R1", on=date(2026, 8, 19), operator=SUP)
        svc.release_to_stock("W-200", on=date(2026, 8, 20), operator=OP)
        device = svc.store.devices["W-200"]
        self.assertEqual(device.status, DeviceStatus.IN_STOCK)
        self.assertEqual(device.maintenance, Maintenance.OK)

    def test_unfixable_device_can_only_be_retired(self):
        svc = self.svc
        svc.deliver_device("W-200", "fam-1", "loan-1",
                           on=date(2026, 8, 1), operator=OP)
        svc.open_recall("R1", "walker-01", ["B1"], "轮轴裂纹",
                        on=date(2026, 8, 11), operator=SUP)
        svc.record_return("R1:W-200", on=date(2026, 8, 12), operator=OP)
        svc.send_to_repair("W-200", on=date(2026, 8, 13), operator=OP)
        svc.record_repair_conclusion("W-200", "UNFIXABLE", "车架断裂",
                                     on=date(2026, 8, 15), operator="技师")
        svc.sign_reinspection("W-200", on=date(2026, 8, 16), operator="质检")
        with self.assertRaises(RecallError):
            svc.release_to_stock("W-200", on=date(2026, 8, 17), operator=OP)
        svc.retire_device("W-200", on=date(2026, 8, 17), operator=OP)
        self.assertEqual(svc.store.devices["W-200"].status, DeviceStatus.RETIRED)

    def test_settlement_counts_only_usable_days(self):
        svc = self.svc
        svc.deliver_device("W-200", "fam-1", "loan-1",
                           on=date(2026, 8, 1), operator=OP)
        svc.open_recall("R1", "walker-01", ["B1"], "轮轴裂纹",
                        on=date(2026, 8, 11), operator=SUP)
        svc.reserve_substitute("R1", "loan-1", "S-1",
                               on=date(2026, 8, 11), operator=OP)
        svc.deliver_substitute("R1:loan-1", on=date(2026, 8, 21), operator=OP)

        entries = svc.settle_period("2026-08", on=date(2026, 8, 31))
        # 8/1–8/10 与 8/21–8/31 可用，共 21 天；替代设备不重复入账
        self.assertEqual(len(entries), 1)
        charge = entries[0]
        self.assertEqual(charge.kind, EntryKind.CHARGE)
        self.assertEqual(charge.days, 21)
        self.assertEqual(charge.amount, 210)
        self.assertEqual(charge.subsidy_amount, 105)

        # 重复结算同一期间不会重复入账
        self.assertEqual(svc.settle_period("2026-08", on=date(2026, 8, 31)), [])
        self.assertEqual(len(svc.store.ledger), 1)

    def test_paid_difference_goes_to_next_period_adjustment(self):
        svc = self.svc
        svc.deliver_device("W-200", "fam-1", "loan-1",
                           on=date(2026, 8, 1), operator=OP)
        charge = svc.settle_period("2026-08", on=date(2026, 8, 31))[0]
        self.assertEqual(charge.amount, 310)  # 31 天 × 10
        svc.record_payment(charge.entry_id, 310, on=date(2026, 9, 1), operator="收银")

        # 离线回执：设备实际已于 8/20 归还；重复补录不生效
        svc.return_device("W-200", on=date(2026, 8, 20), operator=OP,
                          idempotency_key="receipt-offline-1")
        svc.return_device("W-200", on=date(2026, 8, 20), operator=OP,
                          idempotency_key="receipt-offline-1")

        adjustments = svc.settle_period("2026-08", on=date(2026, 9, 3))
        self.assertEqual(len(adjustments), 1)
        adj = adjustments[0]
        self.assertEqual(adj.kind, EntryKind.ADJUSTMENT)
        self.assertEqual(adj.period, "2026-09")  # 差额进入下一期
        self.assertEqual(adj.adjusts, charge.entry_id)
        self.assertEqual(adj.days, -11)
        self.assertEqual(adj.amount, -110)
        self.assertEqual(adj.subsidy_amount, -55)

        # 旧账不被覆盖，再次结算不产生新账
        self.assertEqual(charge.amount, 310)
        self.assertTrue(charge.paid)
        self.assertEqual(svc.settle_period("2026-08", on=date(2026, 9, 4)), [])
        self.assertEqual(len(svc.store.ledger), 2)

    def test_process_recovery_keeps_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            svc = RecallService.open(path)
            svc.set_family_quota("fam-1", 1)
            svc.register_device("W-200", "walker-01", "v2", "B1",
                                daily_price=10, subsidy_rate=0.5, subsidy_cap=1000,
                                on=date(2026, 8, 1), operator=OP)
            svc.register_device("S-1", "walker-02", "v1", "C1",
                                daily_price=12, subsidy_rate=0.5, subsidy_cap=1200,
                                on=date(2026, 8, 1), operator=OP)
            svc.deliver_device("W-200", "fam-1", "loan-1",
                               on=date(2026, 8, 1), operator=OP)
            svc.open_recall("R1", "walker-01", ["B1"], "轮轴裂纹",
                            on=date(2026, 9, 1), operator=SUP,
                            idempotency_key="n1")
            svc.reserve_substitute("R1", "loan-1", "S-1",
                                   on=date(2026, 9, 2), operator=OP,
                                   idempotency_key="s1")

            # 进程恢复：重放同一幂等键不重复发放替代设备
            svc2 = RecallService.open(path)
            svc2.open_recall("R1", "walker-01", ["B1"], "轮轴裂纹",
                             on=date(2026, 9, 1), operator=SUP,
                             idempotency_key="n1")
            svc2.reserve_substitute("R1", "loan-1", "S-1",
                                    on=date(2026, 9, 2), operator=OP,
                                    idempotency_key="s1")
            self.assertEqual(len(svc2.store.substitutions), 1)
            self.assertEqual(len(svc2.store.tasks), 1)

            # 恢复后可以继续办理业务，状态正常持久化
            svc2.deliver_substitute("R1:loan-1", on=date(2026, 9, 3), operator=OP)
            svc3 = RecallService.open(path)
            sub = svc3.store.substitutions["R1:loan-1"]
            self.assertEqual(sub.state, SubState.DELIVERED)
            self.assertEqual(svc3.store.devices["S-1"].status,
                             DeviceStatus.DELIVERED)

    def test_device_history_traces_decisions_custody_contacts_money(self):
        svc = self.svc
        svc.deliver_device("W-200", "fam-1", "loan-1",
                           on=date(2026, 8, 1), operator=OP)
        svc.open_recall("R1", "walker-01", ["B1"], "轮轴裂纹",
                        on=date(2026, 8, 11), operator=SUP)
        svc.record_contact("R1:W-200", "REACHED", on=date(2026, 8, 12),
                           operator=OP)
        svc.record_return("R1:W-200", on=date(2026, 8, 13), operator=OP)
        svc.send_to_repair("W-200", on=date(2026, 8, 14), operator=OP)
        svc.record_repair_conclusion("W-200", "FIXED", "更换轮轴",
                                     on=date(2026, 8, 15), operator="技师")
        svc.sign_reinspection("W-200", on=date(2026, 8, 16), operator="质检")
        svc.close_recall("R1", on=date(2026, 8, 17), operator=SUP)
        svc.release_to_stock("W-200", on=date(2026, 8, 18), operator=OP)
        svc.settle_period("2026-08", on=date(2026, 8, 31))

        history = svc.device_history("W-200")
        categories = {e.category for e in history}
        self.assertEqual(categories, {"RECALL", "CUSTODY", "CONTACT", "MONEY"})
        seqs = [e.seq for e in history]
        self.assertEqual(seqs, sorted(seqs))
        actions = [e.action for e in history]
        self.assertIn("recall_hold_placed", actions)   # 召回决定
        self.assertIn("delivered", actions)            # 保管节点
        self.assertIn("contact_recorded", actions)     # 联系结果
        self.assertIn("charge_booked", actions)        # 金额变化


if __name__ == "__main__":
    unittest.main()
