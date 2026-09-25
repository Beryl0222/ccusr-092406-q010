"""辅具安全召回、替代履约与补贴结算领域服务。

设计要点
--------
* 事件溯源：召回决定、保管节点、联系结果、金额变化全部是不可变事件，
  ``Service.replay`` 可从日志重建全部进程状态（进程恢复）。
* 命令幂等：每条带 ``command_id`` 的命令只生效一次；重复通知重放结果而
  不重复派单；离线回执按回执号去重，同一物理归还不会重复触发结算。
* 人工隔离优先：召回范围按批次可扩大，但缩围**不**自动解除隔离，必须
  维修结论 + 复检签署两道人工门禁后设备才能恢复流转。
* 替代一一对应：替代链路暂占/交付/归还全程绑定，家庭额度只在替代实际
  占用期间计一次，任何重放都不会重复扣减。
* 追加式台账：费用按设备实际可用天数结算；已支付金额不改写，差异生成
  下一期调整分录，旧账永远保留。

金额单位为分（整数），日期为 ``datetime.date``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Callable, Iterable


# ---------------------------------------------------------------------------
# 枚举与异常
# ---------------------------------------------------------------------------


class MaintenanceState(str, Enum):
    """设备维护（流转）状态。"""

    IN_SERVICE = "in_service"        # 正常流转中（在库或已交付家庭）
    QUARANTINED = "quarantined"      # 人工隔离：等待维修结论
    IN_REPAIR = "in_repair"          # 维修中
    PENDING_RECHECK = "pending_recheck"  # 维修完成，等待复检签署
    SCRAPPED = "scrapped"            # 报废，永久退出流转


class Custody(str, Enum):
    """保管节点：设备物理上在哪里。"""

    WAREHOUSE = "warehouse"          # 中心库房
    FAMILY = "family"                # 已交付家庭
    REPAIR_SHOP = "repair_shop"      # 送修中
    RECALLED = "recalled"            # 已召回入库（隔离保管）


class ContactLevel(str, Enum):
    """分级联系：等级越高触达方式越强制。"""

    NOTICE = "notice"                # 一级：App/短信通知
    PHONE = "phone"                  # 二级：电话联系
    ONSITE = "onsite"                # 三级：上门


class RepairOutcome(str, Enum):
    REPAIRED = "repaired"
    SCRAP = "scrap"


class QuarantineReason(str, Enum):
    RECALL = "recall"                # 召回隔离
    MANUAL = "manual"                # 人工隔离（与召回范围无关）


class IdempotencyError(Exception):
    """命令携带的 command_id 已被另一条不同的命令使用。"""


class RuleError(Exception):
    """操作违反召回/履约/结算业务规则。"""


# ---------------------------------------------------------------------------
# 档案值对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubsidyPolicy:
    """补贴口径：日租金（分）与补贴比例（0~1），家庭承担其余部分。"""

    daily_rate: int
    rate: float

    def __post_init__(self) -> None:
        if self.daily_rate < 0 or not 0 <= self.rate <= 1:
            raise ValueError("补贴口径超出允许范围")

    def daily_subsidy(self) -> int:
        return round(self.daily_rate * self.rate)


@dataclass
class DeviceRecord:
    """单台设备档案：序列号、型号版本、批次、维护状态、交付关系、补贴口径。"""

    serial: str
    model: str
    version: str
    batch: str
    custody: Custody = Custody.WAREHOUSE
    maintenance: MaintenanceState = MaintenanceState.IN_SERVICE
    family_id: str | None = None
    delivered_on: date | None = None
    daily_rate: int = 0
    subsidy_rate: float = 0.0


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    on: str
    data: dict
    command_id: str | None = None
    command_kind: str | None = None

    def to_dict(self) -> dict:
        return {"seq": self.seq, "kind": self.kind, "on": self.on,
                "data": self.data, "command_id": self.command_id,
                "command_kind": self.command_kind}


# ---------------------------------------------------------------------------
# 内部读模型（由事件投影得到）
# ---------------------------------------------------------------------------


@dataclass
class _Loan:
    family_id: str
    started: date
    ended: date | None = None
    interrupted_on: date | None = None  # 召回通知日：计费实际可用截止日


@dataclass
class _ReplacementLink:
    link_id: str
    recall_id: str
    family_id: str
    replacement_serial: str
    original_serial: str
    held_on: date | None = None
    delivered_on: date | None = None
    returned_on: date | None = None
    cancelled_on: date | None = None

    @property
    def active(self) -> bool:
        return self.returned_on is None and self.cancelled_on is None


@dataclass
class _ContactTask:
    device_serial: str
    family_id: str
    level: ContactLevel
    due: date
    done: bool = False
    reached: bool = False
    result_note: str = ""
    on: date | None = None


@dataclass
class _ReturnTask:
    device_serial: str
    family_id: str
    due: date
    done: bool = False
    on: date | None = None
    receipt_no: str | None = None


# ---------------------------------------------------------------------------
# 对外读模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerEntry:
    period: str
    family_id: str
    device_serial: str
    amount: int          # 正=应发补贴，负=调减/追回
    kind: str            # settlement / adjustment
    source_period: str | None
    memo: str

    def to_dict(self) -> dict:
        return {
            "period": self.period,
            "family_id": self.family_id,
            "device_serial": self.device_serial,
            "amount": self.amount,
            "kind": self.kind,
            "source_period": self.source_period,
            "memo": self.memo,
        }


@dataclass
class DeviceTrace:
    """工作人员追踪单台设备的完整视图。"""

    serial: str
    model: str
    version: str
    batch: str
    custody: Custody
    maintenance: MaintenanceState
    family_id: str | None
    quarantine_reasons: list[str]
    recalls: list[dict]
    contacts: list[dict]
    returns: list[dict]
    replacements: list[dict]
    ledger: list[dict]
    events: list[dict]


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------


class RecallService:
    """召回处置 + 替代履约 + 补贴结算。

    所有变更只通过 ``_append`` 落事件；构造投影状态后即可在任意时点重放恢复。
    """

    # 每级联系的宽限天数：上一级未取得联系后升级
    ESCALATION_DAYS = {
        ContactLevel.NOTICE: 2,
        ContactLevel.PHONE: 3,
    }
    NEXT_LEVEL = {
        ContactLevel.NOTICE: ContactLevel.PHONE,
        ContactLevel.PHONE: ContactLevel.ONSITE,
    }

    def __init__(self, devices: Iterable[dict] | None = None) -> None:
        self._events: list[Event] = []
        self._devices: dict[str, DeviceRecord] = {}
        self._policies: dict[str, SubsidyPolicy] = {}
        # 命令幂等表：command_id -> 命令类型
        self._commands: dict[str, str] = {}
        # 同进程内命令的首次返回值（重放后改为按状态重算）
        self._command_results: dict[str, object] = {}
        # 已消费的离线回执号
        self._receipts: set[str] = set()
        # 召回范围：recall_id -> {批次集合}，及是否关闭
        self._recall_batches: dict[str, set[str]] = {}
        self._recall_closed: dict[str, bool] = {}
        # 设备级状态
        self._quarantine: dict[str, set[str]] = {}
        self._loans: dict[str, list[_Loan]] = {}
        self._links: dict[str, _ReplacementLink] = {}
        # 一台被召回设备至多一条活跃替代链路
        self._link_by_original: dict[str, str] = {}
        # 序列号 -> 占用它的活跃链路（替代设备本身不能被重复暂占）
        self._link_by_replacement: dict[str, str] = {}
        self._contacts: dict[str, list[_ContactTask]] = {}
        self._returns: dict[str, list[_ReturnTask]] = {}
        # 家庭额度：family_id -> 当期已占用替代设备数（任一时刻在占即计数）
        self._quota_used: dict[str, int] = {}
        # 家庭额度上限
        self._quotas: dict[str, int] = {}
        # 已结算的借用段（按事件 id 幂等）：loan key -> settled period
        self._settled: set[tuple[str, str, str]] = set()
        # 追加式补贴台账
        self._ledger: list[LedgerEntry] = []
        # 当前正在执行的命令（供 _append 标记幂等归属）
        self._current_command: tuple[str | None, str | None] = (None, None)

        for d in devices or []:
            self.register_device(d)

    # ------------------------------------------------------------------
    # 档案登记
    # ------------------------------------------------------------------

    def register_device(self, d: dict, command_id: str | None = None) -> None:
        """登记设备档案。

        至少含 serial/model/version/batch；可含 custody/family_id/
        delivered_on/daily_rate/subsidy_rate（描述已在交付关系中的设备）。
        同时接受夹具使用的驼峰键：familyId/deliveredOn/dailyRate/
        subsidyRate。
        """
        d = {
            "serial": d["serial"], "model": d["model"],
            "version": d["version"], "batch": d["batch"],
            "custody": d.get("custody", Custody.WAREHOUSE.value),
            "family_id": d.get("family_id", d.get("familyId")),
            "delivered_on": d.get("delivered_on", d.get("deliveredOn")),
            "daily_rate": int(d.get("daily_rate",
                                    d.get("dailyRate", 0))),
            "subsidy_rate": float(d.get("subsidy_rate",
                                        d.get("subsidyRate", 0.0))),
        }
        return self._run(
            "register_device", command_id, d,
            lambda: self._append(
                "device_registered",
                serial=d["serial"],
                model=d["model"],
                version=d["version"],
                batch=d["batch"],
                custody=d["custody"],
                family_id=d["family_id"],
                delivered_on=_iso(d["delivered_on"]),
                daily_rate=d["daily_rate"],
                subsidy_rate=d["subsidy_rate"],
            ),
        )

    @classmethod
    def from_fixture(cls, path: str) -> "RecallService":
        """从 JSON 夹具（驼峰或蛇形键均可）加载设备档案。"""
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def set_family_quota(self, family_id: str, quota: int,
                         command_id: str | None = None) -> None:
        """设定家庭同期可占用替代设备的额度上限（事件化，可随日志恢复）。"""
        def do() -> None:
            self._append("family_quota_set", family_id=family_id, quota=quota)
        self._run("set_family_quota", command_id,
                  {"family": family_id, "quota": quota}, do)

    # ------------------------------------------------------------------
    # 召回范围：可按批次扩大；缩小不解除人工隔离
    # ------------------------------------------------------------------

    def open_recall(self, recall_id: str, reason: str, batches: list[str],
                    on: date, command_id: str | None = None) -> dict:
        """发起召回，圈定初始批次。返回受影响的设备序列号与派生任务数。"""
        def do() -> dict:
            self._append("recall_opened", recall_id=recall_id,
                         reason=reason, batches=list(batches), on=_iso(on))
            for b in batches:
                self._append("recall_batch_added", recall_id=recall_id,
                             batch=b, on=_iso(on))
            affected = self._affected_serials(recall_id)
            tasks = 0
            for serial in affected:
                rec = self._devices[serial]
                self._append("device_quarantined", serial=serial,
                             reason=QuarantineReason.RECALL.value,
                             recall_id=recall_id, on=_iso(on))
                if rec.custody is Custody.FAMILY:
                    # 通知送达即视为设备不再可安全使用：借用段计费中断
                    self._append("loan_interrupted", serial=serial,
                                 family_id=rec.family_id, on=_iso(on))
                    self._append("contact_task_created", serial=serial,
                                 family_id=rec.family_id,
                                 level=ContactLevel.NOTICE.value,
                                 due=_iso(on), recall_id=recall_id,
                                 on=_iso(on))
                    self._append("return_task_created", serial=serial,
                                 family_id=rec.family_id,
                                 due=_iso(on), recall_id=recall_id,
                                 on=_iso(on))
                    tasks += 2
            return {"recall_id": recall_id, "affected": sorted(affected),
                    "tasks_created": tasks}
        return self._run("open_recall", command_id,
                         {"recall_id": recall_id, "batches": batches}, do,
                         repeat=lambda: self._recall_summary(recall_id))

    def expand_recall(self, recall_id: str, batches: list[str], on: date,
                      command_id: str | None = None) -> dict:
        """把更多批次纳入召回（只能扩大）。"""
        def do() -> dict:
            self._require_open_recall(recall_id)
            added = [b for b in batches
                     if b not in self._recall_batches[recall_id]]
            for b in added:
                self._append("recall_batch_added", recall_id=recall_id,
                             batch=b, on=_iso(on))
            newly = [s for s in self._affected_serials(recall_id)
                     if QuarantineReason.RECALL.value
                     not in self._quarantine.get(s, set())]
            for serial in sorted(newly):
                rec = self._devices[serial]
                self._append("device_quarantined", serial=serial,
                             reason=QuarantineReason.RECALL.value,
                             recall_id=recall_id, on=_iso(on))
                if rec.custody is Custody.FAMILY:
                    self._append("loan_interrupted", serial=serial,
                                 family_id=rec.family_id, on=_iso(on))
                    self._append("contact_task_created", serial=serial,
                                 family_id=rec.family_id,
                                 level=ContactLevel.NOTICE.value,
                                 due=_iso(on), recall_id=recall_id,
                                 on=_iso(on))
                    self._append("return_task_created", serial=serial,
                                 family_id=rec.family_id,
                                 due=_iso(on), recall_id=recall_id,
                                 on=_iso(on))
            return {"recall_id": recall_id, "added_batches": added,
                    "newly_quarantined": newly}
        return self._run("expand_recall", command_id,
                         {"recall_id": recall_id, "batches": batches}, do,
                         repeat=lambda: self._recall_summary(recall_id))

    def narrow_recall(self, recall_id: str, remove_batches: list[str],
                      on: date, command_id: str | None = None) -> dict:
        """缩小召回范围。

        关键规则：仅更新范围登记，**不**产生任何解除隔离事件；范围内曾被
        人工隔离的设备仍保持隔离，直至维修结论与复检签署。
        """
        def do() -> dict:
            self._require_open_recall(recall_id)
            removed = []
            for b in remove_batches:
                if b in self._recall_batches[recall_id]:
                    self._append("recall_batch_removed", recall_id=recall_id,
                                 batch=b, on=_iso(on))
                    removed.append(b)
            still = sorted(s for s in self._devices
                           if self._quarantine.get(s)
                           and QuarantineReason.RECALL.value
                           in self._quarantine[s])
            return {"recall_id": recall_id, "removed_batches": removed,
                    "quarantined_devices_unchanged": still,
                    "note": "缩围不自动解除人工隔离"}
        return self._run("narrow_recall", command_id,
                         {"recall_id": recall_id, "remove": remove_batches}, do,
                         repeat=lambda: self._recall_summary(recall_id))

    def manually_quarantine(self, serial: str, reason: str, on: date,
                            command_id: str | None = None) -> None:
        """人工隔离（例如抽检异常），独立于任何召回范围。"""
        def do() -> None:
            self._require_device(serial)
            if QuarantineReason.MANUAL.value in self._quarantine.get(serial, set()):
                return
            self._append("device_quarantined", serial=serial,
                         reason=QuarantineReason.MANUAL.value,
                         detail=reason, on=_iso(on))
        self._run("manual_quarantine", command_id,
                 {"serial": serial, "reason": reason}, do)

    def deliver_to_family(self, serial: str, family_id: str, on: date,
                          command_id: str | None = None) -> None:
        """把在库设备交付家庭，建立借用段（补贴按实际可用天数从交付日起算）。"""
        def do() -> None:
            rec = self._require_device(serial)
            if rec.custody is not Custody.WAREHOUSE:
                raise RuleError("只有在库设备可以交付")
            if rec.maintenance is not MaintenanceState.IN_SERVICE:
                raise RuleError("设备处于隔离/维修状态，禁止交付")
            self._append("delivered_to_family", serial=serial,
                         family_id=family_id, on=_iso(on))
            self._append("custody_changed", serial=serial,
                         frm=Custody.WAREHOUSE.value,
                         to=Custody.FAMILY.value, on=_iso(on))
        self._run("deliver_to_family", command_id,
                  {"serial": serial, "family": family_id}, do)

    # ------------------------------------------------------------------
    # 分级联系
    # ------------------------------------------------------------------

    def record_contact(self, serial: str, reached: bool, on: date,
                       note: str = "", command_id: str | None = None) -> dict:
        """登记当前待办联系任务的结果；未联系上则自动升级到下一级。

        重复通知（同一 command_id 重放）不会再次升级或重复派单。
        """
        def do() -> dict:
            self._require_device(serial)
            task = self._open_contact_task(serial)
            if task is None:
                raise RuleError(f"设备 {serial} 没有待办的联系任务")
            self._append("contact_result", serial=serial,
                         family_id=task.family_id,
                         level=task.level.value, reached=reached,
                         note=note, on=_iso(on))
            escalated: str | None = None
            if not reached and task.level in self.NEXT_LEVEL:
                nxt = self.NEXT_LEVEL[task.level]
                due = _add_days(on, {
                    ContactLevel.NOTICE: self.ESCALATION_DAYS[ContactLevel.NOTICE],
                    ContactLevel.PHONE: self.ESCALATION_DAYS[ContactLevel.PHONE],
                }[task.level])
                self._append("contact_task_created", serial=serial,
                             family_id=task.family_id, level=nxt.value,
                             due=_iso(due), escalated_from=task.level.value,
                             on=_iso(on))
                escalated = nxt.value
            return {"serial": serial, "family_id": task.family_id,
                    "level": task.level.value, "reached": reached,
                    "escalated_to": escalated}
        return self._run("record_contact", command_id,
                         {"serial": serial, "reached": reached}, do,
                         repeat=lambda: self._contact_view(serial))

    def _contact_view(self, serial: str) -> dict:
        tasks = self._contacts.get(serial, [])
        done = [t for t in tasks if t.done]
        latest = done[-1]
        open_task = next((t for t in reversed(tasks) if not t.done), None)
        return {"serial": serial, "family_id": latest.family_id,
                "level": latest.level.value, "reached": latest.reached,
                "escalated_to": open_task.level.value if open_task else None}

    def pending_contact(self, serial: str) -> dict | None:
        task = self._open_contact_task(serial)
        if task is None:
            return None
        return {"serial": serial, "family_id": task.family_id,
                "level": task.level.value, "due": task.due.isoformat()}

    # ------------------------------------------------------------------
    # 归还（含离线回执去重）
    # ------------------------------------------------------------------

    def accept_return(self, serial: str, on: date, receipt_no: str,
                      command_id: str | None = None) -> dict:
        """接收家庭归还的召回设备，离线回执号 receipt_no 全局去重。

        同一张离线回执因网络问题重复上传，只入账一次。
        """
        def do() -> dict:
            rec = self._require_device(serial)
            # 离线回执号本身即幂等键：重传同号回执返回首次结果，不重复入账；
            # 但同号回执挪用于其他设备属于伪造，必须拒绝。
            if receipt_no in self._receipts:
                first = next(e for e in self._events
                             if e.kind == "return_received"
                             and e.data["receipt_no"] == receipt_no)
                if first.data["serial"] != serial:
                    raise RuleError(
                        f"回执 {receipt_no} 已属于设备 "
                        f"{first.data['serial']}，禁止挪用于 {serial}")
                return {"serial": serial, "receipt_no": receipt_no,
                        "custody": rec.custody.value}
            if rec.custody is not Custody.FAMILY:
                raise RuleError(f"设备 {serial} 不在家庭手中，无需归还")
            task = self._open_return_task(serial)
            self._append("return_received", serial=serial,
                         family_id=rec.family_id, receipt_no=receipt_no,
                         on=_iso(on))
            if task is not None:
                self._append("return_task_closed", serial=serial,
                             family_id=rec.family_id, receipt_no=receipt_no,
                             on=_iso(on))
            # 召回设备回到中心即进入隔离保管，待维修
            self._append("custody_changed", serial=serial,
                         frm=Custody.FAMILY.value, to=Custody.RECALLED.value,
                         on=_iso(on))
            return {"serial": serial, "receipt_no": receipt_no,
                    "custody": Custody.RECALLED.value}
        return self._run("accept_return", command_id,
                         {"serial": serial, "receipt": receipt_no}, do,
                         repeat=lambda: {"serial": serial,
                                         "receipt_no": receipt_no,
                                         "custody":
                                             self._devices[serial].custody.value})

    # ------------------------------------------------------------------
    # 替代设备：暂占 / 交付 / 归还，全程 1:1
    # ------------------------------------------------------------------

    def hold_replacement(self, recall_id: str, original_serial: str,
                         replacement_serial: str, on: date,
                         command_id: str | None = None) -> dict:
        """为被召回设备暂占一台同型号替代设备（1:1 绑定）。"""
        def do() -> dict:
            orig = self._require_device(original_serial)
            repl = self._require_device(replacement_serial)
            self._require_open_recall(recall_id)
            if orig.family_id is None:
                raise RuleError("设备尚未交付家庭，无需替代履约")
            if (repl.model, repl.version) != (orig.model, orig.version):
                raise RuleError("替代设备必须与原设备型号版本一致")
            if repl.maintenance is not MaintenanceState.IN_SERVICE:
                raise RuleError("替代设备不可用：未处于正常流转状态")
            if replacement_serial in self._link_by_replacement:
                raise RuleError("替代设备已被其他召回链路暂占，禁止重复发放")
            if original_serial in self._link_by_original:
                link = self._links[self._link_by_original[original_serial]]
                if link.active:
                    raise RuleError("该召回设备已存在活跃替代链路")
            quota = self._quotas.get(orig.family_id)
            if quota is not None and self._quota_used.get(orig.family_id, 0) >= quota:
                raise RuleError(f"家庭 {orig.family_id} 替代设备额度已满")
            link_id = (f"{recall_id}:{original_serial}"
                       f"#{self._link_seq(original_serial) + 1}")
            self._append("replacement_held", link_id=link_id,
                         recall_id=recall_id, family_id=orig.family_id,
                         original_serial=original_serial,
                         replacement_serial=replacement_serial, on=_iso(on))
            return {"link_id": link_id,
                    "replacement_serial": replacement_serial,
                    "quota_used": self._quota_used.get(orig.family_id, 0)}
        return self._run("hold_replacement", command_id,
                         {"recall_id": recall_id,
                          "original": original_serial,
                          "replacement": replacement_serial}, do,
                         repeat=lambda: self._link_view(
                             self._links[self._link_by_original[
                                 original_serial]]))

    def cancel_hold(self, link_id: str, on: date,
                    command_id: str | None = None) -> None:
        """取消尚未交付的暂占，释放家庭额度与替代设备占用（1:1 解绑）。"""
        def do() -> None:
            link = self._require_link(link_id)
            if link.delivered_on is not None:
                raise RuleError("替代设备已交付，不能取消暂占，须走归还")
            self._append("replacement_hold_cancelled", link_id=link_id,
                         serial=link.replacement_serial,
                         family_id=link.family_id, on=_iso(on))
        self._run("cancel_hold", command_id, {"link_id": link_id}, do)

    def deliver_replacement(self, link_id: str, on: date,
                            command_id: str | None = None) -> dict:
        """把已暂占的替代设备交付家庭。"""
        def do() -> dict:
            link = self._require_link(link_id)
            repl = self._devices[link.replacement_serial]
            if link.held_on is None:
                raise RuleError("替代设备尚未暂占")
            if link.delivered_on is not None:
                raise RuleError("替代设备已交付，禁止重复交付")
            if repl.maintenance is not MaintenanceState.IN_SERVICE:
                raise RuleError("替代设备在交付前失去可用状态")
            self._append("replacement_delivered", link_id=link_id,
                         serial=link.replacement_serial,
                         family_id=link.family_id, on=_iso(on))
            self._append("custody_changed", serial=link.replacement_serial,
                         frm=Custody.WAREHOUSE.value, to=Custody.FAMILY.value,
                         on=_iso(on))
            return {"link_id": link_id, "delivered_on": on.isoformat()}
        return self._run("deliver_replacement", command_id,
                         {"link_id": link_id}, do,
                         repeat=lambda: {
                             "link_id": link_id,
                             "delivered_on":
                                 _iso(self._links[link_id].delivered_on)})

    def return_replacement(self, link_id: str, on: date,
                           command_id: str | None = None) -> dict:
        """家庭归还替代设备，链路闭环并释放额度（仅一次）。"""
        def do() -> dict:
            link = self._require_link(link_id)
            if link.returned_on is not None:
                raise RuleError("替代链路已闭环，禁止重复归还")
            if link.delivered_on is None:
                raise RuleError("替代设备尚未交付，不能归还")
            self._append("replacement_returned", link_id=link_id,
                         serial=link.replacement_serial,
                         family_id=link.family_id, on=_iso(on))
            self._append("custody_changed", serial=link.replacement_serial,
                         frm=Custody.FAMILY.value, to=Custody.WAREHOUSE.value,
                         on=_iso(on))
            return {"link_id": link_id, "returned_on": on.isoformat(),
                    "quota_used": self._quota_used.get(link.family_id, 0)}
        return self._run("return_replacement", command_id,
                         {"link_id": link_id}, do,
                         repeat=lambda: {
                             "link_id": link_id,
                             "returned_on":
                                 _iso(self._links[link_id].returned_on),
                             "quota_used":
                                 self._quota_used.get(
                                     self._links[link_id].family_id, 0)})

    def replacement_link(self, serial: str) -> dict | None:
        """查询某台被召回设备当前的替代链路（一一对应）。"""
        link_id = self._link_by_original.get(serial)
        if link_id is None:
            return None
        return self._link_view(self._links[link_id])

    # ------------------------------------------------------------------
    # 维修结论 + 复检签署：双门禁后方可恢复流转
    # ------------------------------------------------------------------

    def send_to_repair(self, serial: str, on: date,
                       command_id: str | None = None) -> None:
        def do() -> None:
            rec = self._require_device(serial)
            if rec.maintenance not in (MaintenanceState.QUARANTINED,):
                raise RuleError("只有隔离中的设备可以送修")
            self._append("sent_to_repair", serial=serial, on=_iso(on))
            self._append("custody_changed", serial=serial,
                         frm=rec.custody.value, to=Custody.REPAIR_SHOP.value,
                         on=_iso(on))
        self._run("send_to_repair", command_id, {"serial": serial}, do)

    def record_repair_conclusion(self, serial: str, outcome: RepairOutcome,
                                 note: str, inspector: str, on: date,
                                 command_id: str | None = None) -> None:
        """第一道门禁：维修结论。修复 -> 待复检；报废 -> 永久退出。"""
        def do() -> None:
            rec = self._require_device(serial)
            if rec.maintenance is not MaintenanceState.IN_REPAIR:
                raise RuleError("设备不在维修中，不能登记维修结论")
            self._append("repair_concluded", serial=serial,
                         outcome=outcome.value, note=note,
                         inspector=inspector, on=_iso(on))
            if outcome is RepairOutcome.SCRAP:
                # 报废：回库封存并永久退出流转，不再走复检门禁
                self._append("custody_changed", serial=serial,
                             frm=Custody.REPAIR_SHOP.value,
                             to=Custody.WAREHOUSE.value, on=_iso(on))
        self._run("repair_conclusion", command_id,
                 {"serial": serial, "outcome": outcome.value}, do)

    def sign_recheck(self, serial: str, signer: str, passed: bool, on: date,
                     command_id: str | None = None) -> dict:
        """第二道门禁：复检签署。

        通过且不存在其他隔离理由（如人工隔离）时才解除隔离、恢复流转；
        缩围后残留的召回隔离同样在此处人工放行。复检不通过则退回维修。
        """
        def do() -> dict:
            rec = self._require_device(serial)
            if rec.maintenance is not MaintenanceState.PENDING_RECHECK:
                raise RuleError("设备未完成维修，不能复检签署")
            if not passed:
                self._append("recheck_rejected", serial=serial,
                             signer=signer, on=_iso(on))
                return {"serial": serial, "restored": False,
                        "maintenance": MaintenanceState.IN_REPAIR.value}
            self._append("recheck_signed", serial=serial, signer=signer,
                         on=_iso(on))
            remaining = sorted(self._quarantine.get(serial, set()))
            restored = not remaining
            return {"serial": serial, "restored": restored,
                    "maintenance": rec.maintenance.value,
                    "remaining_quarantine": remaining}
        return self._run("sign_recheck", command_id,
                         {"serial": serial, "passed": passed}, do)

    def release_quarantine(self, serial: str, reason: str, on: date,
                           command_id: str | None = None) -> None:
        """复检通过后，人工解除指定的隔离理由（召回/人工），恢复流转。"""
        def do() -> None:
            rec = self._require_device(serial)
            if rec.maintenance is not MaintenanceState.PENDING_RECHECK:
                raise RuleError("尚未完成维修与复检，不能解除隔离")
            reasons = self._quarantine.get(serial, set())
            if reason not in reasons:
                raise RuleError(f"设备不存在隔离理由 {reason}")
            self._append("quarantine_released", serial=serial, reason=reason,
                         on=_iso(on))
            if not self._quarantine.get(serial):
                # 双门禁完成且无其他隔离理由：回库并恢复流转
                if rec.custody is not Custody.WAREHOUSE:
                    self._append("custody_changed", serial=serial,
                                 frm=rec.custody.value,
                                 to=Custody.WAREHOUSE.value, on=_iso(on))
                self._append("device_restored", serial=serial,
                             custody=Custody.WAREHOUSE.value, on=_iso(on))
        self._run("release_quarantine", command_id,
                 {"serial": serial, "reason": reason}, do)

    # ------------------------------------------------------------------
    # 补贴结算：按实际可用天数；差异进下一期调整，不覆盖旧账
    # ------------------------------------------------------------------

    def settle_period(self, family_id: str, device_serial: str,
                      period: str, paid_amount: int, on: date,
                      command_id: str | None = None) -> dict:
        """结算某家庭某台设备在一个计费期的补贴。

        ``paid_amount`` 是该期已按整月预付/已支付的补贴（分）。实际应得按
        借用段的“实际可用天数”（交付日起至归还前一日或召回通知前一日）
        计算；原支付金额保留在结算事件中，差额生成为**下一期**的调整分录
        （少补为正、多退为负），绝不覆盖旧账。
        """
        def do() -> dict:
            self._require_device(device_serial)
            key = (family_id, device_serial, period)
            if key in self._settled:
                raise RuleError("该期已结算，不能重复结算")
            loans = [l for l in self._loans.get(device_serial, [])
                     if l.family_id == family_id]
            if not loans:
                raise RuleError("该设备与家庭不存在交付关系")
            policy = self._policies[device_serial]
            available_days = 0
            for loan in loans:
                # 召回通知送达即不可安全使用：截止日取“归还”与“中断”较早者
                ends = [e for e in (loan.ended, loan.interrupted_on) if e]
                end = min(ends) if ends else on
                available_days += max((end - loan.started).days, 0)
            actual = available_days * policy.daily_subsidy()
            diff = actual - paid_amount
            next_period = _next_period(period)
            idx = len(self._ledger)
            self._append("subsidy_settled", family_id=family_id,
                         serial=device_serial, period=period,
                         available_days=available_days,
                         actual_amount=actual, paid_amount=paid_amount,
                         entry_index=idx, on=_iso(on))
            if diff != 0:
                adj_idx = len(self._ledger)
                self._append("subsidy_adjusted", family_id=family_id,
                             serial=device_serial, period=next_period,
                             source_period=period, amount=diff,
                             entry_index=adj_idx, on=_iso(on))
            return {"family_id": family_id, "serial": device_serial,
                    "period": period, "available_days": available_days,
                    "actual_amount": actual, "paid_amount": paid_amount,
                    "next_period": next_period,
                    "next_period_adjustment": diff}
        return self._run("settle_period", command_id,
                         {"family": family_id, "serial": device_serial,
                          "period": period}, do,
                         repeat=lambda: self._settlement_view(
                             family_id, device_serial, period, on))

    def _settlement_view(self, family_id: str, serial: str, period: str,
                         on: date) -> dict:
        """进程恢复/重发后重算结算结果（台账已存在，不重复入账）。"""
        e = next(ev for ev in self._events
                 if ev.kind == "subsidy_settled"
                 and ev.data["family_id"] == family_id
                 and ev.data["serial"] == serial
                 and ev.data["period"] == period)
        d = e.data
        return {"family_id": family_id, "serial": serial, "period": period,
                "available_days": d["available_days"],
                "actual_amount": d["actual_amount"],
                "paid_amount": d["paid_amount"],
                "next_period": _next_period(period),
                "next_period_adjustment":
                    d["actual_amount"] - d["paid_amount"]}

    def ledger(self, family_id: str | None = None,
               device_serial: str | None = None) -> list[dict]:
        rows = [e for e in self._ledger]
        if family_id is not None:
            rows = [e for e in rows if e.family_id == family_id]
        if device_serial is not None:
            rows = [e for e in rows if e.device_serial == device_serial]
        return [e.to_dict() for e in rows]

    # ------------------------------------------------------------------
    # 追踪与持久化
    # ------------------------------------------------------------------

    def trace_device(self, serial: str) -> DeviceTrace:
        """汇总一台设备的召回决定、保管节点、联系结果与金额变化。"""
        rec = self._require_device(serial)
        return DeviceTrace(
            serial=serial, model=rec.model, version=rec.version,
            batch=rec.batch, custody=rec.custody,
            maintenance=rec.maintenance, family_id=rec.family_id,
            quarantine_reasons=sorted(self._quarantine.get(serial, set())),
            recalls=[
                {"recall_id": rid, "batches": sorted(batches),
                 "closed": self._recall_closed.get(rid, False)}
                for rid, batches in self._recall_batches.items()
                if rec.batch in batches
                or any(ev.kind == "device_quarantined"
                       and ev.data.get("serial") == serial
                       and ev.data.get("recall_id") == rid
                       for ev in self._events)
            ],
            contacts=[
                {"level": t.level.value, "due": t.due.isoformat(),
                 "done": t.done, "reached": t.reached,
                 "on": t.on.isoformat() if t.on else None,
                 "note": t.result_note}
                for t in self._contacts.get(serial, [])
            ],
            returns=[
                {"due": t.due.isoformat(), "done": t.done,
                 "on": t.on.isoformat() if t.on else None,
                 "receipt_no": t.receipt_no}
                for t in self._returns.get(serial, [])
            ],
            replacements=[
                self._link_view(l)
                for l in self._links.values()
                if l.original_serial == serial
                or (l.replacement_serial == serial)
            ],
            ledger=[e.to_dict() for e in self._ledger
                    if e.device_serial == serial],
            events=[e.to_dict() for e in self._events
                    if e.data.get("serial") == serial
                    or e.data.get("original_serial") == serial
                    or e.data.get("replacement_serial") == serial],
        )

    def journal(self) -> list[dict]:
        return [e.to_dict() for e in self._events]

    def save_journal(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump([e.to_dict() for e in self._events], f,
                      ensure_ascii=False, indent=2)

    @classmethod
    def replay(cls, journal: list[dict]) -> "RecallService":
        """进程恢复：从事件日志重建服务，幂等表与台账一并还原。"""
        svc = cls()
        for e in journal:
            ev = Event(e["seq"], e["kind"], e["on"], e["data"],
                       e.get("command_id"), e.get("command_kind"))
            svc._events.append(ev)
            svc._project(ev)
            if ev.command_id is not None:
                svc._commands[ev.command_id] = ev.command_kind
        return svc

    # ------------------------------------------------------------------
    # 内部：命令执行与幂等
    # ------------------------------------------------------------------

    def _run(self, kind: str, command_id: str | None, payload: object,
             do: Callable[[], object], repeat: Callable[[], object] | None = None):
        if command_id is not None and command_id in self._commands:
            seen = self._commands[command_id]
            if seen != kind:
                raise IdempotencyError(
                    f"command_id {command_id} 已用于 {seen}，不能冒充 {kind}")
            # 重复通知/离线重传/进程恢复：不产生新事件
            if command_id in self._command_results:
                return self._command_results[command_id]
            return repeat() if repeat is not None else None
        self._current_command = (command_id, kind)
        try:
            result = do()
        finally:
            self._current_command = (None, None)
        if command_id is not None:
            self._commands[command_id] = kind
            self._command_results[command_id] = result
        return result

    def _append(self, kind: str, /, on: str | None = None, **data) -> Event:
        cid, ckind = self._current_command
        ev = Event(seq=len(self._events), kind=kind, on=on or "", data=data,
                   command_id=cid, command_kind=ckind)
        self._events.append(ev)
        self._project(ev)
        return ev

    # ------------------------------------------------------------------
    # 内部：事件投影（纯函数式状态推进，重放与实时执行走同一条路径）
    # ------------------------------------------------------------------

    def _project(self, ev: Event) -> None:
        d = ev.data
        kind = ev.kind
        if kind == "family_quota_set":
            self._quotas[d["family_id"]] = d["quota"]
        elif kind == "device_registered":
            custody = Custody(d["custody"])
            rec = DeviceRecord(
                serial=d["serial"], model=d["model"], version=d["version"],
                batch=d["batch"], custody=custody,
                maintenance=(MaintenanceState.IN_REPAIR
                             if custody is Custody.REPAIR_SHOP
                             else MaintenanceState.IN_SERVICE),
                family_id=d.get("family_id"),
                delivered_on=_parse(d.get("delivered_on")),
                daily_rate=d["daily_rate"], subsidy_rate=d["subsidy_rate"])
            self._devices[rec.serial] = rec
            self._policies[rec.serial] = SubsidyPolicy(
                rec.daily_rate, rec.subsidy_rate)
            if rec.family_id and rec.delivered_on is not None:
                self._loans.setdefault(rec.serial, []).append(
                    _Loan(rec.family_id, rec.delivered_on))
        elif kind == "recall_opened":
            self._recall_batches[d["recall_id"]] = set()
            self._recall_closed[d["recall_id"]] = False
        elif kind == "recall_batch_added":
            self._recall_batches[d["recall_id"]].add(d["batch"])
        elif kind == "recall_batch_removed":
            self._recall_batches[d["recall_id"]].discard(d["batch"])
            # 注意：这里故意不触碰 _quarantine —— 缩围不解除人工隔离
        elif kind == "device_quarantined":
            s = d["serial"]
            self._quarantine.setdefault(s, set()).add(d["reason"])
            rec = self._devices.get(s)
            if rec and rec.maintenance is MaintenanceState.IN_SERVICE:
                rec.maintenance = MaintenanceState.QUARANTINED
        elif kind == "delivered_to_family":
            rec = self._devices[d["serial"]]
            rec.family_id = d["family_id"]
            rec.delivered_on = _parse(ev.on)
            self._loans.setdefault(d["serial"], []).append(
                _Loan(d["family_id"], _parse(ev.on)))
        elif kind == "loan_interrupted":
            for loan in self._loans.get(d["serial"], []):
                if loan.family_id == d["family_id"] and loan.ended is None \
                        and loan.interrupted_on is None:
                    loan.interrupted_on = _parse(ev.on)
        elif kind == "contact_task_created":
            self._contacts.setdefault(d["serial"], []).append(_ContactTask(
                d["serial"], d["family_id"], ContactLevel(d["level"]),
                _parse(d["due"])))
        elif kind == "contact_result":
            task = self._open_contact_task(d["serial"])
            if task is not None:
                task.done = True
                task.reached = d["reached"]
                task.result_note = d.get("note", "")
                task.on = _parse(ev.on)
        elif kind == "return_task_created":
            self._returns.setdefault(d["serial"], []).append(_ReturnTask(
                d["serial"], d["family_id"], _parse(d["due"])))
        elif kind == "return_received":
            rec = self._devices[d["serial"]]
            for loan in self._loans.get(d["serial"], []):
                if loan.family_id == d["family_id"] and loan.ended is None:
                    loan.ended = _parse(ev.on)
            self._receipts.add(d["receipt_no"])
        elif kind == "return_task_closed":
            task = self._open_return_task(d["serial"])
            if task is not None:
                task.done = True
                task.on = _parse(ev.on)
                task.receipt_no = d.get("receipt_no")
        elif kind == "custody_changed":
            rec = self._devices[d["serial"]]
            rec.custody = Custody(d["to"])
            if d["to"] != Custody.FAMILY.value:
                rec.family_id = None
        elif kind == "replacement_held":
            link = _ReplacementLink(
                d["link_id"], d["recall_id"], d["family_id"],
                d["replacement_serial"], d["original_serial"],
                held_on=_parse(ev.on))
            self._links[d["link_id"]] = link
            self._link_by_original[d["original_serial"]] = d["link_id"]
            self._link_by_replacement[d["replacement_serial"]] = d["link_id"]
            self._quota_used[d["family_id"]] = \
                self._quota_used.get(d["family_id"], 0) + 1
        elif kind == "replacement_hold_cancelled":
            link = self._links[d["link_id"]]
            link.cancelled_on = _parse(ev.on)
            self._link_by_replacement.pop(link.replacement_serial, None)
            self._quota_used[link.family_id] = \
                max(0, self._quota_used.get(link.family_id, 0) - 1)
        elif kind == "replacement_delivered":
            link = self._links[d["link_id"]]
            link.delivered_on = _parse(ev.on)
            rec = self._devices[link.replacement_serial]
            rec.family_id = link.family_id
        elif kind == "replacement_returned":
            link = self._links[d["link_id"]]
            link.returned_on = _parse(ev.on)
            self._link_by_replacement.pop(link.replacement_serial, None)
            self._quota_used[link.family_id] = \
                max(0, self._quota_used.get(link.family_id, 0) - 1)
        elif kind == "sent_to_repair":
            self._devices[d["serial"]].maintenance = MaintenanceState.IN_REPAIR
        elif kind == "repair_concluded":
            rec = self._devices[d["serial"]]
            if d["outcome"] == RepairOutcome.SCRAP.value:
                rec.maintenance = MaintenanceState.SCRAPPED
            else:
                rec.maintenance = MaintenanceState.PENDING_RECHECK
        elif kind == "recheck_rejected":
            self._devices[d["serial"]].maintenance = MaintenanceState.IN_REPAIR
        elif kind == "recheck_signed":
            # 仍保持 PENDING_RECHECK，直到隔离理由逐一人工解除
            pass
        elif kind == "quarantine_released":
            self._quarantine.get(d["serial"], set()).discard(d["reason"])
        elif kind == "device_restored":
            rec = self._devices[d["serial"]]
            rec.maintenance = MaintenanceState.IN_SERVICE
        elif kind == "subsidy_settled":
            entry = LedgerEntry(
                period=d["period"], family_id=d["family_id"],
                device_serial=d["serial"], amount=d["actual_amount"],
                kind="settlement", source_period=None,
                memo=f"实际可用 {d['available_days']} 天；"
                     f"已支付 {d['paid_amount']} 分（旧账保留）")
            self._ledger.insert(d["entry_index"], entry)
            self._settled.add((d["family_id"], d["serial"], d["period"]))
        elif kind == "subsidy_adjusted":
            entry = LedgerEntry(
                period=d["period"], family_id=d["family_id"],
                device_serial=d["serial"], amount=d["amount"],
                kind="adjustment", source_period=d["source_period"],
                memo="差异计入下一期调整，不覆盖原支付")
            self._ledger.insert(d["entry_index"], entry)

    # ------------------------------------------------------------------
    # 内部：小工具
    # ------------------------------------------------------------------

    def _require_device(self, serial: str) -> DeviceRecord:
        if serial not in self._devices:
            raise RuleError(f"未知设备 {serial}")
        return self._devices[serial]

    def _require_open_recall(self, recall_id: str) -> None:
        if recall_id not in self._recall_batches:
            raise RuleError(f"召回单 {recall_id} 不存在")
        if self._recall_closed.get(recall_id):
            raise RuleError(f"召回单 {recall_id} 已关闭")

    def _require_link(self, link_id: str) -> _ReplacementLink:
        if link_id not in self._links:
            raise RuleError(f"替代链路 {link_id} 不存在")
        return self._links[link_id]

    def _affected_serials(self, recall_id: str) -> list[str]:
        batches = self._recall_batches.get(recall_id, set())
        return sorted(s for s, r in self._devices.items() if r.batch in batches)

    def _open_contact_task(self, serial: str) -> _ContactTask | None:
        for t in reversed(self._contacts.get(serial, [])):
            if not t.done:
                return t
        return None

    def _open_return_task(self, serial: str) -> _ReturnTask | None:
        for t in reversed(self._returns.get(serial, [])):
            if not t.done:
                return t
        return None

    def _recall_summary(self, recall_id: str) -> dict:
        return {"recall_id": recall_id,
                "batches": sorted(self._recall_batches.get(recall_id, set())),
                "affected": self._affected_serials(recall_id)}

    def _link_seq(self, original_serial: str) -> int:
        """该被召回设备历史上已建立的替代链路数。"""
        return sum(1 for l in self._links.values()
                   if l.original_serial == original_serial)

    def _link_view(self, link: _ReplacementLink) -> dict:
        return {
            "link_id": link.link_id,
            "recall_id": link.recall_id, "family_id": link.family_id,
            "original_serial": link.original_serial,
            "replacement_serial": link.replacement_serial,
            "held_on": _iso(link.held_on),
            "delivered_on": _iso(link.delivered_on),
            "returned_on": _iso(link.returned_on),
            "cancelled_on": _iso(link.cancelled_on),
            "active": link.active,
        }


# ---------------------------------------------------------------------------
# 日期工具
# ---------------------------------------------------------------------------


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _parse(v: str | None) -> date | None:
    return date.fromisoformat(v) if v else None


def _next_period(period: str) -> str:
    """``YYYY-MM`` 的下一个计费期，跨年回绕到 1 月。"""
    year, month = (int(x) for x in period.split("-"))
    month += 1
    if month == 13:
        year, month = year + 1, 1
    return f"{year:04d}-{month:02d}"


def _add_days(d: date, n: int) -> date:
    from datetime import timedelta
    return d + timedelta(days=n)
