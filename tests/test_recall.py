"""召回处置、替代履约与补贴结算的业务规则测试。"""

import os
import unittest
from datetime import date

from recall import (
    ContactLevel,
    Custody,
    IdempotencyError,
    MaintenanceState,
    RecallService,
    RepairOutcome,
    RuleError,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures",
                       "devices.json")


def device(serial, batch="B2026-03", custody=None,
           family_id=None, delivered_on=None, rate=800, subsidy=0.5):
    if custody is None:
        custody = (Custody.FAMILY.value if family_id
                   else Custody.WAREHOUSE.value)
    return {
        "serial": serial, "model": "智能助行器", "version": "v2",
        "batch": batch, "custody": custody, "family_id": family_id,
        "delivered_on": delivered_on,
        "daily_rate": rate, "subsidy_rate": subsidy,
    }


def seeded_service():
    """三台同型号设备：W-001 在家庭，W-002 在库，W-009 其他批次在库。"""
    svc = RecallService([
        device("W-001", family_id="F-1",
               delivered_on=date(2026, 3, 1).isoformat()),
        device("W-002"),
        device("W-009", batch="B2026-09"),
    ])
    svc.set_family_quota("F-1", 1)
    return svc


class DeviceRecordTest(unittest.TestCase):
    def test_register_keeps_serial_version_custody_and_policy(self):
        svc = RecallService([device("W-001", family_id="F-1",
                                    delivered_on="2026-03-01")])
        t = svc.trace_device("W-001")
        self.assertEqual(t.serial, "W-001")
        self.assertEqual((t.model, t.version), ("智能助行器", "v2"))
        self.assertEqual(t.custody, Custody.FAMILY)
        self.assertEqual(t.family_id, "F-1")

    def test_invalid_subsidy_policy_rejected(self):
        with self.assertRaises(ValueError):
            RecallService([device("W-001", subsidy=1.5)])


class RecallScopeTest(unittest.TestCase):
    def test_open_recall_quarantines_all_batch_devices(self):
        svc = seeded_service()
        res = svc.open_recall("R-1", "刹车失效", ["B2026-03"],
                              date(2026, 3, 11), "cmd-open")
        self.assertEqual(res["affected"], ["W-001", "W-002"])
        for s in ("W-001", "W-002"):
            self.assertIn("recall",
                          svc.trace_device(s).quarantine_reasons)
            self.assertEqual(svc.trace_device(s).maintenance,
                             MaintenanceState.QUARANTINED)
        # 其他批次不受影响
        self.assertEqual(svc.trace_device("W-009").quarantine_reasons, [])

    def test_family_devices_get_contact_and_return_tasks_only(self):
        svc = seeded_service()
        svc.open_recall("R-1", "刹车失效", ["B2026-03"], date(2026, 3, 11))
        t1 = svc.trace_device("W-001")
        self.assertEqual(len(t1.contacts), 1)
        self.assertEqual(t1.contacts[0]["level"], ContactLevel.NOTICE.value)
        self.assertEqual(len(t1.returns), 1)
        # 在库设备不产生联系/归还任务
        t2 = svc.trace_device("W-002")
        self.assertEqual(t2.contacts, [])
        self.assertEqual(t2.returns, [])

    def test_expand_recall_only_adds_batches(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        res = svc.expand_recall("R-1", ["B2026-03", "B2026-09"],
                                date(2026, 3, 12))
        self.assertEqual(res["added_batches"], ["B2026-09"])
        self.assertIn("recall",
                      svc.trace_device("W-009").quarantine_reasons)

    def test_narrow_does_not_release_manual_quarantine(self):
        svc = seeded_service()
        svc.manually_quarantine("W-009", "巡检异响", date(2026, 3, 5))
        svc.open_recall("R-9", "y", ["B2026-09"], date(2026, 3, 6))
        res = svc.narrow_recall("R-9", ["B2026-09"], date(2026, 3, 7))
        # 范围登记已缩小
        self.assertEqual(res["removed_batches"], ["B2026-09"])
        self.assertNotIn("B2026-09",
                         svc._recall_batches["R-9"])
        # 但人工隔离与召回隔离都还在设备上
        reasons = svc.trace_device("W-009").quarantine_reasons
        self.assertEqual(set(reasons), {"manual", "recall"})
        self.assertEqual(svc.trace_device("W-009").maintenance,
                         MaintenanceState.QUARANTINED)

    def test_narrow_then_expand_does_not_recreate_quarantine_events(self):
        svc = seeded_service()
        svc.open_recall("R-9", "y", ["B2026-09"], date(2026, 3, 6))
        svc.narrow_recall("R-9", ["B2026-09"], date(2026, 3, 7))
        before = len(svc.journal())
        svc.expand_recall("R-9", ["B2026-09"], date(2026, 3, 8))
        # 设备此前未被人工解除隔离，扩围不应重复下发隔离事件
        self.assertEqual(len(svc.journal()), before + 1)


class ContactEscalationTest(unittest.TestCase):
    def test_escalates_notice_phone_onsite(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        r1 = svc.record_contact("W-001", False, date(2026, 3, 11))
        self.assertEqual(r1["level"], ContactLevel.NOTICE.value)
        self.assertEqual(r1["escalated_to"], ContactLevel.PHONE.value)
        r2 = svc.record_contact("W-001", False, date(2026, 3, 13))
        self.assertEqual(r2["level"], ContactLevel.PHONE.value)
        self.assertEqual(r2["escalated_to"], ContactLevel.ONSITE.value)
        # 上门取得联系：不再升级
        r3 = svc.record_contact("W-001", True, date(2026, 3, 16),
                                "约定 3/18 归还")
        self.assertEqual(r3["level"], ContactLevel.ONSITE.value)
        self.assertIsNone(r3["escalated_to"])
        self.assertIsNone(svc.pending_contact("W-001"))
        contacts = svc.trace_device("W-001").contacts
        self.assertEqual([c["level"] for c in contacts],
                         ["notice", "phone", "onsite"])

    def test_duplicate_notification_does_not_escalate_twice(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        svc.record_contact("W-001", False, date(2026, 3, 11),
                           command_id="cmd-c1")
        events_after = len(svc.journal())
        replay = svc.record_contact("W-001", False, date(2026, 3, 11),
                                    command_id="cmd-c1")
        self.assertEqual(replay["escalated_to"], ContactLevel.PHONE.value)
        self.assertEqual(len(svc.journal()), events_after)
        # 仍只有一个升级后的电话任务
        levels = [c["level"] for c in svc.trace_device("W-001").contacts]
        self.assertEqual(levels, ["notice", "phone"])


class ReturnTest(unittest.TestCase):
    def _recalled(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        return svc

    def test_return_moves_to_recalled_custody_and_closes_task(self):
        svc = self._recalled()
        res = svc.accept_return("W-001", date(2026, 3, 18), "RC-1001")
        self.assertEqual(res["custody"], Custody.RECALLED.value)
        t = svc.trace_device("W-001")
        self.assertTrue(t.returns[0]["done"])
        self.assertEqual(t.returns[0]["receipt_no"], "RC-1001")
        self.assertEqual(t.custody, Custody.RECALLED)
        self.assertIsNone(t.family_id)

    def test_offline_receipt_replay_is_idempotent(self):
        svc = self._recalled()
        first = svc.accept_return("W-001", date(2026, 3, 18), "RC-1001",
                                  command_id="cmd-rc")
        events_after = len(svc.journal())
        # 网络恢复后同号回执重传：返回相同结果，不产生事件
        second = svc.accept_return("W-001", date(2026, 3, 18), "RC-1001",
                                   command_id="cmd-rc")
        self.assertEqual(second, first)
        self.assertEqual(len(svc.journal()), events_after)

    def test_receipt_number_reused_on_other_device_rejected(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        svc.accept_return("W-001", date(2026, 3, 18), "RC-1001")
        # 另一台设备交付 F-2 后被另一召回单命中，产生归还任务
        svc.deliver_to_family("W-009", "F-2", date(2026, 3, 1))
        svc.open_recall("R-2", "同型号风险", ["B2026-09"],
                        date(2026, 3, 11))
        # 把已使用的回执号挪用到 W-009 必须被拒绝
        with self.assertRaises(RuleError):
            svc.accept_return("W-009", date(2026, 3, 18), "RC-1001")


class ReplacementTest(unittest.TestCase):
    def _ready(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11),
                        "cmd-open")
        return svc

    def test_hold_deliver_return_is_one_to_one(self):
        svc = self._ready()
        hold = svc.hold_replacement("R-1", "W-001", "W-009",
                                    date(2026, 3, 12), "cmd-hold")
        self.assertTrue(hold["link_id"].startswith("R-1:W-001"))
        self.assertEqual(svc._quota_used["F-1"], 1)
        svc.deliver_replacement(hold["link_id"], date(2026, 3, 13),
                                "cmd-deliver")
        # 替代设备已在家庭手中，且绑定原设备
        view = svc.replacement_link("W-001")
        self.assertEqual(view["replacement_serial"], "W-009")
        self.assertEqual(svc.trace_device("W-009").family_id, "F-1")
        svc.return_replacement(hold["link_id"], date(2026, 3, 20),
                               "cmd-return")
        self.assertFalse(svc.replacement_link("W-001")["active"])
        self.assertEqual(svc._quota_used["F-1"], 0)
        self.assertEqual(svc.trace_device("W-009").custody, Custody.WAREHOUSE)

    def test_cannot_hold_same_replacement_twice(self):
        svc = self._ready()
        svc.hold_replacement("R-1", "W-001", "W-009", date(2026, 3, 12))
        with self.assertRaises(RuleError):
            svc.hold_replacement("R-1", "W-001", "W-009", date(2026, 3, 12))

    def test_cannot_deliver_twice(self):
        svc = self._ready()
        hold = svc.hold_replacement("R-1", "W-001", "W-009",
                                    date(2026, 3, 12))
        svc.deliver_replacement(hold["link_id"], date(2026, 3, 13))
        with self.assertRaises(RuleError):
            svc.deliver_replacement(hold["link_id"], date(2026, 3, 13))

    def test_quota_prevents_second_replacement(self):
        svc = seeded_service()
        # 家庭 F-1 额度 1；两台同批次设备都在 F-1
        svc.deliver_to_family("W-002", "F-1", date(2026, 3, 2))
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        svc.hold_replacement("R-1", "W-001", "W-009", date(2026, 3, 12))
        # 没有空闲同型号设备且额度已满：直接暂存另一台也会被额度拦截
        svc.register_device(device("W-010", batch="B2026-09"))
        with self.assertRaises(RuleError):
            svc.hold_replacement("R-1", "W-002", "W-010", date(2026, 3, 12))

    def test_replay_does_not_double_deduct_quota_or_reissue(self):
        svc = self._ready()
        hold = svc.hold_replacement("R-1", "W-001", "W-009",
                                    date(2026, 3, 12), "cmd-hold")
        svc.deliver_replacement(hold["link_id"], date(2026, 3, 13),
                                "cmd-deliver")
        restored = RecallService.replay(svc.journal())
        self.assertEqual(restored._quota_used["F-1"], 1)
        # 进程恢复后重发暂占命令：不产生新链路、不重复扣额度
        events_before = len(restored.journal())
        restored.hold_replacement("R-1", "W-001", "W-009",
                                  date(2026, 3, 12), "cmd-hold")
        self.assertEqual(len(restored.journal()), events_before)
        self.assertEqual(restored._quota_used["F-1"], 1)

    def test_cancel_hold_releases_quota_before_delivery(self):
        svc = self._ready()
        hold = svc.hold_replacement("R-1", "W-001", "W-009",
                                    date(2026, 3, 12))
        svc.cancel_hold(hold["link_id"], date(2026, 3, 12))
        self.assertEqual(svc._quota_used["F-1"], 0)
        # 释放后同一替代设备可以被新链路暂占
        hold2 = svc.hold_replacement("R-1", "W-001", "W-009",
                                     date(2026, 3, 13))
        self.assertNotEqual(hold["link_id"], hold2["link_id"])
        self.assertEqual(svc._quota_used["F-1"], 1)

    def test_command_id_cannot_be_reused_for_other_command(self):
        svc = self._ready()
        svc.hold_replacement("R-1", "W-001", "W-009", date(2026, 3, 12),
                             command_id="same-id")
        with self.assertRaises(IdempotencyError):
            svc.return_replacement("R-1:W-001#1", date(2026, 3, 20),
                                   command_id="same-id")


class RepairGateTest(unittest.TestCase):
    def _returned(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        svc.accept_return("W-001", date(2026, 3, 18), "RC-1")
        return svc

    def test_both_conclusion_and_signed_recheck_required(self):
        svc = self._returned()
        # 未维修不能复检
        with self.assertRaises(RuleError):
            svc.sign_recheck("W-001", "李检", True, date(2026, 3, 23))
        svc.send_to_repair("W-001", date(2026, 3, 19))
        self.assertEqual(svc.trace_device("W-001").maintenance,
                         MaintenanceState.IN_REPAIR)
        # 只有维修结论、未签署复检：仍是待复检，不能恢复流转
        svc.record_repair_conclusion("W-001", RepairOutcome.REPAIRED,
                                     "更换刹车总成", "张工",
                                     date(2026, 3, 22))
        self.assertEqual(svc.trace_device("W-001").maintenance,
                         MaintenanceState.PENDING_RECHECK)
        sign = svc.sign_recheck("W-001", "李检", True, date(2026, 3, 23))
        self.assertFalse(sign["restored"])
        # 必须人工解除召回隔离后才恢复
        svc.release_quarantine("W-001", "recall", date(2026, 3, 23))
        t = svc.trace_device("W-001")
        self.assertEqual(t.maintenance, MaintenanceState.IN_SERVICE)
        self.assertEqual(t.custody, Custody.WAREHOUSE)

    def test_failed_recheck_goes_back_to_repair(self):
        svc = self._returned()
        svc.send_to_repair("W-001", date(2026, 3, 19))
        svc.record_repair_conclusion("W-001", RepairOutcome.REPAIRED,
                                     "修过", "张工", date(2026, 3, 22))
        svc.sign_recheck("W-001", "李检", False, date(2026, 3, 23))
        self.assertEqual(svc.trace_device("W-001").maintenance,
                         MaintenanceState.IN_REPAIR)

    def test_manual_quarantine_must_also_be_released(self):
        svc = self._returned()
        svc.manually_quarantine("W-001", "外壳裂痕", date(2026, 3, 18))
        svc.send_to_repair("W-001", date(2026, 3, 19))
        svc.record_repair_conclusion("W-001", RepairOutcome.REPAIRED,
                                     "完成", "张工", date(2026, 3, 22))
        svc.sign_recheck("W-001", "李检", True, date(2026, 3, 23))
        svc.release_quarantine("W-001", "recall", date(2026, 3, 23))
        # 召回理由解除了，但人工隔离仍在
        self.assertEqual(svc.trace_device("W-001").maintenance,
                         MaintenanceState.PENDING_RECHECK)
        svc.release_quarantine("W-001", "manual", date(2026, 3, 24))
        self.assertEqual(svc.trace_device("W-001").maintenance,
                         MaintenanceState.IN_SERVICE)

    def test_scrap_exits_circulation_without_recheck(self):
        svc = self._returned()
        svc.send_to_repair("W-001", date(2026, 3, 19))
        svc.record_repair_conclusion("W-001", RepairOutcome.SCRAP,
                                     "车架变形", "张工", date(2026, 3, 22))
        t = svc.trace_device("W-001")
        self.assertEqual(t.maintenance, MaintenanceState.SCRAPPED)
        with self.assertRaises(RuleError):
            svc.sign_recheck("W-001", "李检", True, date(2026, 3, 23))


class SettlementTest(unittest.TestCase):
    def test_settlement_uses_actual_available_days(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        # 可用区间 3/1 至 3/11 前一日 = 10 天；日补贴 800*0.5 = 400
        res = svc.settle_period("F-1", "W-001", "2026-03", 12000,
                                date(2026, 4, 1))
        self.assertEqual(res["available_days"], 10)
        self.assertEqual(res["actual_amount"], 4000)
        self.assertEqual(res["next_period"], "2026-04")
        # 多付 8000 分进入下一期调减
        self.assertEqual(res["next_period_adjustment"], -8000)

    def test_physical_return_after_notice_does_not_extend_days(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        svc.accept_return("W-001", date(2026, 3, 18), "RC-1")
        res = svc.settle_period("F-1", "W-001", "2026-03", 12000,
                                date(2026, 4, 1))
        # 通知日（3/11）而非归还日（3/18）为截止
        self.assertEqual(res["available_days"], 10)

    def test_ledger_is_append_only_with_next_period_adjustment(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        svc.settle_period("F-1", "W-001", "2026-03", 12000,
                          date(2026, 4, 1), command_id="cmd-settle")
        ledger = svc.ledger("F-1", "W-001")
        self.assertEqual([e["kind"] for e in ledger],
                         ["settlement", "adjustment"])
        self.assertEqual(ledger[0]["amount"], 4000)
        self.assertEqual(ledger[0]["period"], "2026-03")
        self.assertEqual(ledger[1]["amount"], -8000)
        self.assertEqual(ledger[1]["period"], "2026-04")
        self.assertEqual(ledger[1]["source_period"], "2026-03")
        # 原结算分录不被覆盖
        self.assertIn("已支付 12000", ledger[0]["memo"])

    def test_period_cannot_be_settled_twice(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        svc.settle_period("F-1", "W-001", "2026-03", 12000,
                          date(2026, 4, 1), command_id="cmd-settle")
        # 同 command_id 重放：返回相同结果，不加分录
        again = svc.settle_period("F-1", "W-001", "2026-03", 12000,
                                  date(2026, 4, 1), command_id="cmd-settle")
        self.assertEqual(again["actual_amount"], 4000)
        self.assertEqual(len(svc.ledger("F-1", "W-001")), 2)
        # 换 command_id 再来：业务层拒绝重复结算
        with self.assertRaises(RuleError):
            svc.settle_period("F-1", "W-001", "2026-03", 12000,
                              date(2026, 4, 1), command_id="cmd-settle-2")

    def test_settlement_survives_replay_without_double_entry(self):
        svc = seeded_service()
        svc.open_recall("R-1", "x", ["B2026-03"], date(2026, 3, 11))
        svc.settle_period("F-1", "W-001", "2026-03", 12000,
                          date(2026, 4, 1), command_id="cmd-settle")
        restored = RecallService.replay(svc.journal())
        self.assertEqual(len(restored.ledger("F-1", "W-001")), 2)
        again = restored.settle_period("F-1", "W-001", "2026-03", 12000,
                                       date(2026, 4, 1),
                                       command_id="cmd-settle")
        self.assertEqual(again["next_period_adjustment"], -8000)
        self.assertEqual(len(restored.ledger("F-1", "W-001")), 2)

    def test_underpayment_creates_positive_adjustment(self):
        svc = seeded_service()
        # 不发起召回：结算日 3/11，可用 10 天 = 4000；预付仅 3000
        res = svc.settle_period("F-1", "W-001", "2026-03", 3000,
                                date(2026, 3, 11))
        self.assertEqual(res["next_period_adjustment"], 1000)
        adj = svc.ledger("F-1", "W-001")[1]
        self.assertEqual(adj["amount"], 1000)
        self.assertEqual(adj["period"], "2026-04")


class TraceabilityTest(unittest.TestCase):
    def test_trace_collects_decisions_custody_contacts_money(self):
        svc = seeded_service()
        svc.open_recall("R-1", "刹车失效", ["B2026-03"], date(2026, 3, 11))
        svc.record_contact("W-001", True, date(2026, 3, 12), "电话确认")
        svc.hold_replacement("R-1", "W-001", "W-009", date(2026, 3, 12))
        svc.accept_return("W-001", date(2026, 3, 18), "RC-1001")
        svc.settle_period("F-1", "W-001", "2026-03", 12000,
                          date(2026, 4, 1))
        t = svc.trace_device("W-001")
        # 召回决定
        self.assertEqual(t.recalls[0]["recall_id"], "R-1")
        self.assertIn("B2026-03", t.recalls[0]["batches"])
        # 保管节点变化与联系结果
        self.assertEqual(t.custody, Custody.RECALLED)
        self.assertTrue(any(c["reached"] for c in t.contacts))
        self.assertTrue(t.returns[0]["done"])
        # 替代链路
        self.assertEqual(t.replacements[0]["replacement_serial"], "W-009")
        # 金额变化
        kinds = {e["kind"] for e in t.ledger}
        self.assertEqual(kinds, {"settlement", "adjustment"})
        # 事件时间线完整
        event_kinds = {e["kind"] for e in t.events}
        self.assertIn("device_quarantined", event_kinds)
        self.assertIn("contact_result", event_kinds)
        self.assertIn("return_received", event_kinds)


class FixtureScenarioTest(unittest.TestCase):
    """基于 fixtures/devices.json 的完整召回-替代-维修-结算闭环。"""

    FAMILY = "F-1024"
    ORIGINAL = "AW-WK-2603-0007"
    REPLACEMENT = "AW-WK-2609-0103"
    IN_REPAIR = "AW-WK-2602-0042"

    def test_end_to_end_with_real_fixture(self):
        svc = RecallService.from_fixture(FIXTURE)
        # 夹具状态：一台在家庭、一台在库、一台送修中、一台替代库存
        t0 = svc.trace_device(self.ORIGINAL)
        self.assertEqual(t0.custody, Custody.FAMILY)
        self.assertEqual(svc.trace_device(self.IN_REPAIR).maintenance,
                         MaintenanceState.IN_REPAIR)
        svc.set_family_quota(self.FAMILY, 1)

        res = svc.open_recall("R-2026-009", "刹车失效",
                              ["WK-A2-2026-03"], date(2026, 9, 25))
        self.assertEqual(set(res["affected"]),
                         {"AW-WK-2603-0007", "AW-WK-2603-0011"})
        # 通知未联系上 -> 自动升级电话
        c = svc.record_contact(self.ORIGINAL, False, date(2026, 9, 25))
        self.assertEqual(c["escalated_to"], ContactLevel.PHONE.value)
        svc.record_contact(self.ORIGINAL, True, date(2026, 9, 26),
                           "约定 9/30 归还")

        hold = svc.hold_replacement("R-2026-009", self.ORIGINAL,
                                    self.REPLACEMENT, date(2026, 9, 26))
        svc.deliver_replacement(hold["link_id"], date(2026, 9, 27))
        svc.accept_return(self.ORIGINAL, date(2026, 9, 30),
                          "RC-20260930-07")
        svc.return_replacement(hold["link_id"], date(2026, 10, 2))
        # 额度闭环
        self.assertEqual(svc._quota_used[self.FAMILY], 0)

        svc.send_to_repair(self.ORIGINAL, date(2026, 10, 3))
        svc.record_repair_conclusion(self.ORIGINAL, RepairOutcome.REPAIRED,
                                     "更换刹车总成", "张工",
                                     date(2026, 10, 6))
        svc.sign_recheck(self.ORIGINAL, "李检", True, date(2026, 10, 7))
        svc.release_quarantine(self.ORIGINAL, "recall", date(2026, 10, 7))
        self.assertEqual(svc.trace_device(self.ORIGINAL).maintenance,
                         MaintenanceState.IN_SERVICE)

        settlement = svc.settle_period(
            self.FAMILY, self.ORIGINAL, "2026-09", 12000, date(2026, 10, 1))
        # 交付 3/2 至召回通知 9/25：207 天 × 日补贴 400 分
        self.assertEqual(settlement["available_days"], 207)
        self.assertEqual(settlement["actual_amount"], 82800)
        self.assertEqual(settlement["next_period"], "2026-10")

        # 落盘-恢复后状态一致且命令幂等
        import json
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            svc.save_journal(path)
            with open(path, encoding="utf-8") as f:
                restored = RecallService.replay(json.load(f))
        finally:
            os.unlink(path)
        self.assertEqual(len(restored.journal()), len(svc.journal()))
        self.assertEqual(restored.ledger(self.FAMILY),
                         svc.ledger(self.FAMILY))
        # 恢复后重放归还回执：不产生新事件
        before = len(restored.journal())
        restored.accept_return(self.ORIGINAL, date(2026, 9, 30),
                               "RC-20260930-07", "cmd-rc-1")
        # 该回执首次没有 command_id，属于按回执号幂等
        self.assertEqual(len(restored.journal()), before)


if __name__ == "__main__":
    unittest.main()
