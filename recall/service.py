"""辅具安全召回与替代履约服务。

业务规则：
- 每台设备记录序列号、型号版本、维护状态、交付关系与补贴口径。
- 召回范围按批次扩大；缩小范围只解除召回锁定，不自动解除人工隔离。
- 已交付设备自动生成分级联系与归还任务；联系不到时任务升级。
- 替代设备暂占、交付、归还一一对应，不重复扣减家庭额度。
- 维修结论与复检签署齐全后才可恢复流转；无法修复的设备只能报废。
- 费用按实际可用天数结算；已入账（含已支付）期间的差额进入下一期
  调整，不覆盖旧账。
- 所有变更操作接受幂等键：离线回执、重复通知与进程恢复不会重复
  发放替代设备或多算补贴。
"""

from __future__ import annotations

import os
from datetime import date, timedelta

from catalog import subsidy as _subsidy

from .models import (
    GRADE_P1,
    GRADE_P2,
    GRADE_P3,
    AuditEvent,
    ContactResult,
    ContactTask,
    Device,
    DeviceStatus,
    EntryKind,
    LedgerEntry,
    Loan,
    Maintenance,
    Recall,
    RecallStatus,
    RepairConclusion,
    SubState,
    Substitution,
    TaskStatus,
    escalate,
)
from .store import Store

# 审计分类：召回决定 / 保管节点 / 联系结果 / 金额变化
RECALL, CUSTODY, CONTACT, MONEY = "RECALL", "CUSTODY", "CONTACT", "MONEY"


class RecallError(Exception):
    """业务规则校验失败。"""


def _month_range(period: str) -> tuple[date, date]:
    year, month = (int(part) for part in period.split("-"))
    start = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, nxt - timedelta(days=1)


def _next_period(period: str) -> str:
    year, month = (int(part) for part in period.split("-"))
    return f"{year + 1:04d}-01" if month == 12 else f"{year:04d}-{month + 1:02d}"


class RecallService:
    """召回、替代履约、维修复检与补贴结算的统一入口。"""

    def __init__(self, store: Store | None = None, path: str | None = None) -> None:
        self.store = store or Store()
        self.path = path

    @classmethod
    def open(cls, path: str) -> "RecallService":
        """打开（或新建）持久化服务；进程恢复后幂等键仍然有效。"""
        if os.path.exists(path):
            return cls(Store.load(path), path)
        return cls(Store(), path)

    # ------------------------------------------------------------------
    # 基础设施：持久化、幂等、审计
    # ------------------------------------------------------------------

    def _save(self) -> None:
        if self.path:
            self.store.save(self.path)

    def _replayed(self, key: str | None):
        """命中幂等键时返回 (True, 原结果)，否则 (False, None)。"""
        if key is None or key not in self.store.idempotency:
            return False, None
        kind, ref = self.store.idempotency[key]
        if kind == "entries":
            return True, [self.store.ledger[item] for item in ref]
        tables = {
            "device": self.store.devices,
            "loan": self.store.loans,
            "recall": self.store.recalls,
            "task": self.store.tasks,
            "substitution": self.store.substitutions,
            "entry": self.store.ledger,
        }
        return True, tables[kind][ref]

    def _mark(self, key: str | None, kind: str, ref) -> None:
        if key is not None:
            self.store.idempotency[key] = [kind, ref]

    def _audit(self, serial: str, category: str, action: str,
               on: date, operator: str, **detail) -> None:
        self.store.seq += 1
        self.store.audit.append(AuditEvent(
            seq=self.store.seq, on=on, device_serial=serial,
            category=category, action=action, detail=detail, operator=operator,
        ))

    # ------------------------------------------------------------------
    # 查找
    # ------------------------------------------------------------------

    def _device(self, serial: str) -> Device:
        try:
            return self.store.devices[serial]
        except KeyError:
            raise RecallError(f"设备 {serial} 未登记") from None

    def _loan(self, loan_id: str) -> Loan:
        try:
            return self.store.loans[loan_id]
        except KeyError:
            raise RecallError(f"履约 {loan_id} 不存在") from None

    def _recall(self, recall_id: str) -> Recall:
        try:
            return self.store.recalls[recall_id]
        except KeyError:
            raise RecallError(f"召回 {recall_id} 不存在") from None

    def _open_recall(self, recall_id: str) -> Recall:
        recall = self._recall(recall_id)
        if recall.status is not RecallStatus.OPEN:
            raise RecallError(f"召回 {recall_id} 已关闭")
        return recall

    def _task(self, task_id: str) -> ContactTask:
        try:
            return self.store.tasks[task_id]
        except KeyError:
            raise RecallError(f"任务 {task_id} 不存在") from None

    def _substitution(self, sub_id: str) -> Substitution:
        try:
            return self.store.substitutions[sub_id]
        except KeyError:
            raise RecallError(f"替代履约 {sub_id} 不存在") from None

    def _entry(self, entry_id: str) -> LedgerEntry:
        try:
            return self.store.ledger[entry_id]
        except KeyError:
            raise RecallError(f"账目 {entry_id} 不存在") from None

    def _loan_of_device(self, serial: str) -> Loan | None:
        for loan in self.store.loans.values():
            if loan.device_serial == serial and loan.closed_on is None:
                return loan
        return None

    def _substitutions_of(self, loan_id: str) -> list[Substitution]:
        return [s for s in self.store.substitutions.values() if s.loan_id == loan_id]

    def _active_substitution(self, loan_id: str) -> Substitution | None:
        for sub in self._substitutions_of(loan_id):
            if sub.state in (SubState.RESERVED, SubState.DELIVERED):
                return sub
        return None

    def _active_loans(self, family_id: str) -> list[Loan]:
        return [loan for loan in self.store.loans.values()
                if loan.family_id == family_id and loan.closed_on is None]

    def _devices_of(self, model: str, batches: set[str]) -> list[Device]:
        return [d for d in self.store.devices.values()
                if d.model == model and d.batch in batches]

    # ------------------------------------------------------------------
    # 设备台账与交付
    # ------------------------------------------------------------------

    def register_device(self, serial: str, model: str, model_version: str,
                        batch: str, daily_price: int, subsidy_rate: float,
                        subsidy_cap: int, on: date, operator: str,
                        idempotency_key: str | None = None) -> Device:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        if serial in self.store.devices:
            raise RecallError(f"设备 {serial} 已登记")
        if daily_price < 0 or not 0 <= subsidy_rate <= 1 or subsidy_cap < 0:
            raise RecallError("价格或补贴口径超出允许范围")
        device = Device(serial=serial, model=model, model_version=model_version,
                        batch=batch, daily_price=daily_price,
                        subsidy_rate=subsidy_rate, subsidy_cap=subsidy_cap)
        self.store.devices[serial] = device
        self._audit(serial, CUSTODY, "registered", on, operator,
                    model=model, model_version=model_version, batch=batch)
        self._mark(idempotency_key, "device", serial)
        self._save()
        return device

    def set_family_quota(self, family_id: str, quota: int) -> None:
        if quota < 1:
            raise RecallError("家庭额度至少为 1")
        self.store.family_quotas[family_id] = quota
        self._save()

    def deliver_device(self, serial: str, family_id: str, loan_id: str,
                       on: date, operator: str,
                       idempotency_key: str | None = None) -> Loan:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        device = self._device(serial)
        if loan_id in self.store.loans:
            raise RecallError(f"履约 {loan_id} 已存在")
        if device.status is not DeviceStatus.IN_STOCK:
            raise RecallError(f"设备 {serial} 不在库，不能交付")
        if device.blocked:
            raise RecallError(f"设备 {serial} 处于召回锁定或人工隔离，禁止交付")
        if device.maintenance is Maintenance.CONDEMNED:
            raise RecallError(f"设备 {serial} 已判定无法修复，禁止交付")
        quota = self.store.family_quotas.get(family_id)
        if quota is not None and len(self._active_loans(family_id)) >= quota:
            raise RecallError(f"家庭 {family_id} 额度已用尽")
        device.status = DeviceStatus.DELIVERED
        loan = Loan(loan_id=loan_id, device_serial=serial,
                    family_id=family_id, delivered_on=on)
        self.store.loans[loan_id] = loan
        self._audit(serial, CUSTODY, "delivered", on, operator,
                    family_id=family_id, loan_id=loan_id)
        self._mark(idempotency_key, "loan", loan_id)
        self._save()
        return loan

    def return_device(self, serial: str, on: date, operator: str,
                      idempotency_key: str | None = None) -> Loan:
        """家庭归还设备（正常归还或离线回执补录）。"""
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        device = self._device(serial)
        if device.status is not DeviceStatus.DELIVERED:
            raise RecallError(f"设备 {serial} 不在家庭手中")
        loan = self._loan_of_device(serial)
        if loan is None:
            raise RecallError(f"设备 {serial} 缺少交付关系")
        self._check_in_original(device, loan, on, operator)
        self._mark(idempotency_key, "loan", loan.loan_id)
        self._save()
        return loan

    def _check_in_original(self, device: Device, loan: Loan,
                           on: date, operator: str) -> None:
        loan.original_returned_on = on
        # 召回锁定期间归还：履约保持打开，等待替代或工作人员结案；
        # 不可用区间在锁定解除或替代设备交付时才关闭。
        if not device.blocked and self._active_substitution(loan.loan_id) is None:
            loan.closed_on = on
        device.status = DeviceStatus.IN_STOCK
        self._audit(device.serial, CUSTODY, "returned", on, operator,
                    loan_id=loan.loan_id, family_id=loan.family_id)

    def close_loan(self, loan_id: str, on: date, operator: str,
                   idempotency_key: str | None = None) -> Loan:
        """结案：设备均已归还且家庭不再接受替代。"""
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        loan = self._loan(loan_id)
        if loan.closed_on is not None:
            raise RecallError(f"履约 {loan_id} 已结案")
        if loan.original_returned_on is None:
            raise RecallError("原始设备未归还，不能结案")
        if self._active_substitution(loan_id) is not None:
            raise RecallError("替代设备未归还，不能结案")
        loan.closed_on = on
        self._close_unusable(loan, on)
        self._audit(loan.device_serial, CUSTODY, "loan_closed", on, operator,
                    loan_id=loan_id, family_id=loan.family_id)
        self._mark(idempotency_key, "loan", loan_id)
        self._save()
        return loan

    # ------------------------------------------------------------------
    # 召回范围
    # ------------------------------------------------------------------

    def open_recall(self, recall_id: str, model: str, batches,
                    reason: str, on: date, operator: str,
                    idempotency_key: str | None = None) -> Recall:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        if recall_id in self.store.recalls:
            raise RecallError(f"召回 {recall_id} 已存在")
        if not batches:
            raise RecallError("召回批次不能为空")
        recall = Recall(recall_id=recall_id, model=model, reason=reason,
                        batches=set(batches), created_on=on)
        self.store.recalls[recall_id] = recall
        for device in self._devices_of(model, recall.batches):
            self._apply_hold(device, recall, on, operator)
        self._mark(idempotency_key, "recall", recall_id)
        self._save()
        return recall

    def expand_recall(self, recall_id: str, batches, on: date, operator: str,
                      idempotency_key: str | None = None) -> Recall:
        """按批次扩大召回范围，新纳入设备立即锁定并生成任务。"""
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        recall = self._open_recall(recall_id)
        new_batches = set(batches) - recall.batches
        recall.batches |= set(batches)
        for device in self._devices_of(recall.model, new_batches):
            self._apply_hold(device, recall, on, operator)
        self._mark(idempotency_key, "recall", recall_id)
        self._save()
        return recall

    def narrow_recall(self, recall_id: str, batches, on: date, operator: str,
                      idempotency_key: str | None = None) -> Recall:
        """缩小召回范围：解除召回锁定并取消未完成任务，

        但人工隔离只能由工作人员显式解除，不随范围缩小自动放开。
        """
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        recall = self._open_recall(recall_id)
        removed = set(batches) & recall.batches
        recall.batches -= set(batches)
        for device in self._devices_of(recall.model, removed):
            if recall.recall_id in device.recall_holds:
                self._lift_hold(device, recall, on, operator, cause="scope_narrowed")
        self._mark(idempotency_key, "recall", recall_id)
        self._save()
        return recall

    def close_recall(self, recall_id: str, on: date, operator: str,
                     idempotency_key: str | None = None) -> Recall:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        recall = self._open_recall(recall_id)
        recall.status = RecallStatus.CLOSED
        for device in self.store.devices.values():
            if recall.recall_id in device.recall_holds:
                self._lift_hold(device, recall, on, operator, cause="recall_closed")
        self._mark(idempotency_key, "recall", recall_id)
        self._save()
        return recall

    def _apply_hold(self, device: Device, recall: Recall,
                    on: date, operator: str) -> None:
        if recall.recall_id in device.recall_holds:
            return
        was_blocked = device.blocked
        device.recall_holds.add(recall.recall_id)
        self._audit(device.serial, RECALL, "recall_hold_placed", on, operator,
                    recall_id=recall.recall_id, reason=recall.reason)
        if device.status is DeviceStatus.DELIVERED:
            loan = self._loan_of_device(device.serial)
            if not was_blocked:
                self._open_unusable(loan, on)
            self._ensure_task(recall, device, loan, on, operator)

    def _lift_hold(self, device: Device, recall: Recall, on: date,
                   operator: str, cause: str) -> None:
        device.recall_holds.discard(recall.recall_id)
        self._audit(device.serial, RECALL, "recall_hold_lifted", on, operator,
                    recall_id=recall.recall_id, cause=cause,
                    manual_quarantine_kept=device.manual_quarantine)
        task = self.store.tasks.get(f"{recall.recall_id}:{device.serial}")
        if task and task.status in (TaskStatus.PENDING, TaskStatus.CONTACTED):
            task.status = TaskStatus.CANCELLED
            self._audit(device.serial, CONTACT, "task_cancelled", on, operator,
                        task_id=task.task_id, cause=cause)
        if not device.blocked and device.status is DeviceStatus.DELIVERED:
            self._close_unusable(self._loan_of_device(device.serial), on)

    def quarantine_device(self, serial: str, reason: str, on: date, operator: str,
                          idempotency_key: str | None = None) -> Device:
        """人工隔离：不随召回范围缩小自动解除，只能 release_quarantine 放开。"""
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        device = self._device(serial)
        if device.manual_quarantine:
            raise RecallError(f"设备 {serial} 已处于人工隔离")
        was_blocked = device.blocked
        device.manual_quarantine = True
        self._audit(serial, RECALL, "manual_quarantine_placed", on, operator,
                    reason=reason)
        if not was_blocked and device.status is DeviceStatus.DELIVERED:
            self._open_unusable(self._loan_of_device(serial), on)
        self._mark(idempotency_key, "device", serial)
        self._save()
        return device

    def release_quarantine(self, serial: str, on: date, operator: str,
                           idempotency_key: str | None = None) -> Device:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        device = self._device(serial)
        if not device.manual_quarantine:
            raise RecallError(f"设备 {serial} 未处于人工隔离")
        device.manual_quarantine = False
        self._audit(serial, RECALL, "manual_quarantine_released", on, operator)
        if not device.blocked and device.status is DeviceStatus.DELIVERED:
            self._close_unusable(self._loan_of_device(serial), on)
        self._mark(idempotency_key, "device", serial)
        self._save()
        return device

    # ------------------------------------------------------------------
    # 分级联系与归还任务
    # ------------------------------------------------------------------

    def _grade(self, device: Device, loan: Loan, on: date) -> str:
        """维护状态异常或交付时间越长，联系等级越紧急。"""
        days = (on - loan.delivered_on).days
        if device.maintenance is not Maintenance.OK or days > 180:
            return GRADE_P1
        if days > 90:
            return GRADE_P2
        return GRADE_P3

    def _ensure_task(self, recall: Recall, device: Device, loan: Loan,
                     on: date, operator: str) -> ContactTask:
        task_id = f"{recall.recall_id}:{device.serial}"
        task = self.store.tasks.get(task_id)
        if task and task.status is not TaskStatus.CANCELLED:
            return task
        grade = self._grade(device, loan, on)
        if task is None:
            task = ContactTask(task_id=task_id, recall_id=recall.recall_id,
                               device_serial=device.serial,
                               family_id=loan.family_id, grade=grade)
            self.store.tasks[task_id] = task
            action = "task_created"
        else:
            # 范围再次扩大时重开已取消的任务
            task.status = TaskStatus.PENDING
            task.grade = grade
            task.contact_result = None
            task.contacted_on = None
            task.returned_on = None
            action = "task_reopened"
        self._audit(device.serial, CONTACT, action, on, operator,
                    task_id=task_id, recall_id=recall.recall_id, grade=grade)
        return task

    def record_contact(self, task_id: str, result, on: date, operator: str,
                       idempotency_key: str | None = None) -> ContactTask:
        """登记联系结果（支持离线回执补录）；联系不到时任务升级。"""
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        task = self._task(task_id)
        if task.status not in (TaskStatus.PENDING, TaskStatus.CONTACTED):
            raise RecallError(f"任务 {task_id} 已结束，不能登记联系结果")
        result = ContactResult(result)
        task.attempts += 1
        task.contact_result = result.value
        if result is ContactResult.REACHED:
            task.status = TaskStatus.CONTACTED
            task.contacted_on = on
        else:
            task.grade = escalate(task.grade)
        self._audit(task.device_serial, CONTACT, "contact_recorded", on, operator,
                    task_id=task_id, result=result.value, grade=task.grade,
                    attempts=task.attempts)
        self._mark(idempotency_key, "task", task_id)
        self._save()
        return task

    def record_return(self, task_id: str, on: date, operator: str,
                      idempotency_key: str | None = None) -> ContactTask:
        """登记设备归还：任务完成，设备回库（召回锁定仍保留）。"""
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        task = self._task(task_id)
        if task.status not in (TaskStatus.PENDING, TaskStatus.CONTACTED):
            raise RecallError(f"任务 {task_id} 已结束，不能登记归还")
        device = self._device(task.device_serial)
        if device.status is not DeviceStatus.DELIVERED:
            raise RecallError(f"设备 {device.serial} 不在家庭手中")
        loan = self._loan_of_device(device.serial)
        self._check_in_original(device, loan, on, operator)
        task.status = TaskStatus.COMPLETED
        task.returned_on = on
        self._audit(device.serial, CONTACT, "task_completed", on, operator,
                    task_id=task_id)
        self._mark(idempotency_key, "task", task_id)
        self._save()
        return task

    # ------------------------------------------------------------------
    # 替代履约：暂占 → 交付 → 归还，一一对应
    # ------------------------------------------------------------------

    def reserve_substitute(self, recall_id: str, loan_id: str,
                           substitute_serial: str, on: date, operator: str,
                           idempotency_key: str | None = None) -> Substitution:
        """暂占替代设备。不新增履约，因此不重复扣减家庭额度。"""
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        recall = self._open_recall(recall_id)
        loan = self._loan(loan_id)
        if loan.closed_on is not None:
            raise RecallError(f"履约 {loan_id} 已结案")
        original = self._device(loan.device_serial)
        if recall.recall_id not in original.recall_holds:
            raise RecallError("原始设备不在该召回范围内")
        if self._active_substitution(loan_id) is not None:
            raise RecallError("该履约已有替代设备，暂占/交付/归还必须一一对应")
        sub_id = f"{recall_id}:{loan_id}"
        if sub_id in self.store.substitutions:
            raise RecallError(f"替代履约 {sub_id} 已存在")
        substitute = self._device(substitute_serial)
        if substitute.status is not DeviceStatus.IN_STOCK:
            raise RecallError(f"替代设备 {substitute_serial} 不在库")
        if substitute.blocked:
            raise RecallError(f"替代设备 {substitute_serial} 处于召回锁定或人工隔离")
        if substitute.maintenance is Maintenance.CONDEMNED:
            raise RecallError(f"替代设备 {substitute_serial} 已判定无法修复")
        substitute.status = DeviceStatus.RESERVED
        sub = Substitution(sub_id=sub_id, recall_id=recall_id, loan_id=loan_id,
                           family_id=loan.family_id,
                           original_serial=original.serial,
                           substitute_serial=substitute_serial,
                           state=SubState.RESERVED, reserved_on=on)
        self.store.substitutions[sub_id] = sub
        self._audit(substitute_serial, CUSTODY, "substitute_reserved", on, operator,
                    recall_id=recall_id, loan_id=loan_id,
                    original_serial=original.serial, family_id=loan.family_id)
        self._mark(idempotency_key, "substitution", sub_id)
        self._save()
        return sub

    def deliver_substitute(self, sub_id: str, on: date, operator: str,
                           idempotency_key: str | None = None) -> Substitution:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        sub = self._substitution(sub_id)
        if sub.state is not SubState.RESERVED:
            raise RecallError(f"替代履约 {sub_id} 不在暂占状态")
        substitute = self._device(sub.substitute_serial)
        substitute.status = DeviceStatus.DELIVERED
        sub.state = SubState.DELIVERED
        sub.delivered_on = on
        # 家庭恢复可用，不可用区间结束
        self._close_unusable(self._loan(sub.loan_id), on)
        self._audit(substitute.serial, CUSTODY, "substitute_delivered", on, operator,
                    loan_id=sub.loan_id, family_id=sub.family_id)
        self._mark(idempotency_key, "substitution", sub_id)
        self._save()
        return sub

    def return_substitute(self, sub_id: str, on: date, operator: str,
                          idempotency_key: str | None = None) -> Substitution:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        sub = self._substitution(sub_id)
        if sub.state is not SubState.DELIVERED:
            raise RecallError(f"替代履约 {sub_id} 不在交付状态")
        substitute = self._device(sub.substitute_serial)
        substitute.status = DeviceStatus.IN_STOCK
        sub.state = SubState.RETURNED
        sub.returned_on = on
        loan = self._loan(sub.loan_id)
        if loan.original_returned_on is not None:
            loan.closed_on = on
        self._audit(substitute.serial, CUSTODY, "substitute_returned", on, operator,
                    loan_id=sub.loan_id, family_id=sub.family_id)
        self._mark(idempotency_key, "substitution", sub_id)
        self._save()
        return sub

    # ------------------------------------------------------------------
    # 维修与复检
    # ------------------------------------------------------------------

    def send_to_repair(self, serial: str, on: date, operator: str,
                       idempotency_key: str | None = None) -> Device:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        device = self._device(serial)
        if device.status is not DeviceStatus.IN_STOCK:
            raise RecallError(f"设备 {serial} 不在库，不能送修")
        device.status = DeviceStatus.IN_REPAIR
        device.repair_conclusion = None
        device.reinspection_signed = False
        self._audit(serial, CUSTODY, "sent_to_repair", on, operator)
        self._mark(idempotency_key, "device", serial)
        self._save()
        return device

    def record_repair_conclusion(self, serial: str, conclusion, notes: str,
                                 on: date, operator: str,
                                 idempotency_key: str | None = None) -> Device:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        device = self._device(serial)
        if device.status is not DeviceStatus.IN_REPAIR:
            raise RecallError(f"设备 {serial} 不在送修中")
        conclusion = RepairConclusion(conclusion)
        device.repair_conclusion = conclusion
        device.maintenance = (Maintenance.OK if conclusion is RepairConclusion.FIXED
                              else Maintenance.CONDEMNED)
        self._audit(serial, CUSTODY, "repair_conclusion_recorded", on, operator,
                    conclusion=conclusion.value, notes=notes)
        self._mark(idempotency_key, "device", serial)
        self._save()
        return device

    def sign_reinspection(self, serial: str, on: date, operator: str,
                          idempotency_key: str | None = None) -> Device:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        device = self._device(serial)
        if device.status is not DeviceStatus.IN_REPAIR:
            raise RecallError(f"设备 {serial} 不在送修中")
        if device.repair_conclusion is None:
            raise RecallError("先出具维修结论，才能复检签署")
        device.reinspection_signed = True
        self._audit(serial, CUSTODY, "reinspection_signed", on, operator)
        self._mark(idempotency_key, "device", serial)
        self._save()
        return device

    def release_to_stock(self, serial: str, on: date, operator: str,
                         idempotency_key: str | None = None) -> Device:
        """恢复流转：维修结论与复检签署齐全、锁定解除后才允许。"""
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        device = self._device(serial)
        if device.status is not DeviceStatus.IN_REPAIR:
            raise RecallError(f"设备 {serial} 不在送修中")
        if device.repair_conclusion is None or not device.reinspection_signed:
            raise RecallError("维修结论和复检签署后才可恢复流转")
        if device.repair_conclusion is not RepairConclusion.FIXED:
            raise RecallError("无法修复的设备应报废，不能恢复流转")
        if device.blocked:
            raise RecallError("召回锁定或人工隔离未解除，禁止恢复流转")
        device.status = DeviceStatus.IN_STOCK
        self._audit(serial, CUSTODY, "released_to_stock", on, operator)
        self._mark(idempotency_key, "device", serial)
        self._save()
        return device

    def retire_device(self, serial: str, on: date, operator: str,
                      idempotency_key: str | None = None) -> Device:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        device = self._device(serial)
        if device.status is not DeviceStatus.IN_REPAIR:
            raise RecallError(f"设备 {serial} 不在送修中")
        if device.repair_conclusion is not RepairConclusion.UNFIXABLE:
            raise RecallError("只有维修结论为无法修复的设备才能报废")
        if not device.reinspection_signed:
            raise RecallError("复检签署后才能报废")
        device.status = DeviceStatus.RETIRED
        self._audit(serial, CUSTODY, "retired", on, operator)
        self._mark(idempotency_key, "device", serial)
        self._save()
        return device

    # ------------------------------------------------------------------
    # 结算：按实际可用天数，差额进入下一期调整
    # ------------------------------------------------------------------

    @staticmethod
    def _open_unusable(loan: Loan | None, on: date) -> None:
        if loan is None:
            return
        if loan.unusable_intervals and loan.unusable_intervals[-1][1] is None:
            return
        loan.unusable_intervals.append((on, None))

    @staticmethod
    def _close_unusable(loan: Loan | None, on: date) -> None:
        if loan is None:
            return
        if loan.unusable_intervals and loan.unusable_intervals[-1][1] is None:
            start, _ = loan.unusable_intervals[-1]
            loan.unusable_intervals[-1] = (start, on)

    def _possession_end(self, loan: Loan) -> date | None:
        """家庭手中最后一台设备的归还日；仍有设备在手则为 None。"""
        if loan.original_returned_on is None:
            return None
        ends = [loan.original_returned_on]
        for sub in self._substitutions_of(loan.loan_id):
            if sub.state is SubState.DELIVERED:
                return None
            if sub.state is SubState.RETURNED:
                ends.append(sub.returned_on)
        return max(ends)

    def _usable_days(self, loan: Loan, start: date, end: date) -> int:
        """期间内家庭持有设备且设备未处于锁定/隔离的天数。

        不可用区间为半开区间 [起点, 终点)：锁定/隔离当日不可用，
        替代交付或锁定解除当日恢复可用。
        """
        lo = max(loan.delivered_on, start)
        hi = min(self._possession_end(loan) or end, end)
        if hi < lo:
            return 0
        days = (hi - lo).days + 1
        after_hi = hi + timedelta(days=1)
        for s, e in loan.unusable_intervals:
            es = max(s, lo)
            ee = min(e or after_hi, after_hi)
            if ee > es:
                days -= (ee - es).days
        return max(days, 0)

    def _charge_of(self, loan_id: str, period: str) -> LedgerEntry | None:
        for entry in self.store.ledger.values():
            if (entry.kind is EntryKind.CHARGE and entry.loan_id == loan_id
                    and entry.period == period):
                return entry
        return None

    def _adjustments_of(self, entry_id: str) -> list[LedgerEntry]:
        return [e for e in self.store.ledger.values() if e.adjusts == entry_id]

    def settle_period(self, period: str, on: date, operator: str = "system",
                      idempotency_key: str | None = None) -> list[LedgerEntry]:
        """结算一个期间（YYYY-MM）。

        首次入账生成 CHARGE；该期间已入账（含已支付）后数据发生变化的，
        差额以 ADJUSTMENT 计入下一期，不覆盖旧账。重复结算同一期间不会
        重复入账。
        """
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        start, month_end = _month_range(period)
        end = min(month_end, on)
        if end < start:
            raise RecallError(f"结算期间 {period} 尚未开始")
        created: list[LedgerEntry] = []
        for loan in sorted(self.store.loans.values(), key=lambda l: l.loan_id):
            possession_end = self._possession_end(loan)
            if loan.delivered_on > end:
                continue
            if possession_end is not None and possession_end < start:
                continue
            device = self._device(loan.device_serial)
            days = self._usable_days(loan, start, end)
            amount = days * device.daily_price
            subsidy_amount = _subsidy(amount, device.subsidy_rate, device.subsidy_cap)
            charge = self._charge_of(loan.loan_id, period)
            if charge is None:
                entry = LedgerEntry(
                    entry_id=f"{loan.loan_id}:{period}",
                    loan_id=loan.loan_id, family_id=loan.family_id,
                    device_serial=device.serial, period=period,
                    kind=EntryKind.CHARGE, days=days, amount=amount,
                    subsidy_amount=subsidy_amount, created_on=on,
                )
                self.store.ledger[entry.entry_id] = entry
                created.append(entry)
                self._audit(device.serial, MONEY, "charge_booked", on, operator,
                            entry_id=entry.entry_id, period=period, days=days,
                            amount=amount, subsidy_amount=subsidy_amount)
            else:
                adjustments = self._adjustments_of(charge.entry_id)
                delta_days = days - charge.days - sum(a.days for a in adjustments)
                delta_amount = amount - charge.amount - sum(a.amount for a in adjustments)
                delta_subsidy = (subsidy_amount - charge.subsidy_amount
                                 - sum(a.subsidy_amount for a in adjustments))
                if delta_days or delta_amount or delta_subsidy:
                    entry = LedgerEntry(
                        entry_id=f"{loan.loan_id}:{period}:adj{len(adjustments) + 1}",
                        loan_id=loan.loan_id, family_id=loan.family_id,
                        device_serial=device.serial, period=_next_period(period),
                        kind=EntryKind.ADJUSTMENT, days=delta_days,
                        amount=delta_amount, subsidy_amount=delta_subsidy,
                        created_on=on, adjusts=charge.entry_id,
                    )
                    self.store.ledger[entry.entry_id] = entry
                    created.append(entry)
                    self._audit(device.serial, MONEY, "adjustment_booked", on, operator,
                                entry_id=entry.entry_id, period=entry.period,
                                adjusts=charge.entry_id, days=delta_days,
                                amount=delta_amount, subsidy_amount=delta_subsidy)
        self._mark(idempotency_key, "entries", [e.entry_id for e in created])
        self._save()
        return created

    def record_payment(self, entry_id: str, amount: int, on: date, operator: str,
                       idempotency_key: str | None = None) -> LedgerEntry:
        hit, cached = self._replayed(idempotency_key)
        if hit:
            return cached
        entry = self._entry(entry_id)
        if entry.paid:
            raise RecallError(f"账目 {entry_id} 已支付，禁止重复支付")
        if amount != entry.amount:
            raise RecallError("支付金额与账目不符")
        entry.paid = True
        self._audit(entry.device_serial, MONEY, "payment_recorded", on, operator,
                    entry_id=entry_id, amount=amount)
        self._mark(idempotency_key, "entry", entry_id)
        self._save()
        return entry

    # ------------------------------------------------------------------
    # 追踪
    # ------------------------------------------------------------------

    def device_history(self, serial: str) -> list[AuditEvent]:
        """单台设备的召回决定、保管节点、联系结果与金额变化。"""
        return [e for e in self.store.audit if e.device_serial == serial]

    def device_snapshot(self, serial: str) -> dict:
        device = self._device(serial)
        return {
            "device": device,
            "loans": [l for l in self.store.loans.values()
                      if l.device_serial == serial],
            "substitutions": [s for s in self.store.substitutions.values()
                              if s.original_serial == serial
                              or s.substitute_serial == serial],
            "history": self.device_history(serial),
        }

    def family_usage(self, family_id: str) -> dict:
        active = self._active_loans(family_id)
        return {
            "family_id": family_id,
            "quota": self.store.family_quotas.get(family_id),
            "active_loans": len(active),
            "loan_ids": [loan.loan_id for loan in active],
        }
