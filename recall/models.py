"""召回与替代履约的领域模型。

每台设备记录序列号、型号版本、维护状态与补贴口径；交付关系由 Loan 表达，
替代履约由 Substitution 表达。所有模型均可序列化为 JSON，支撑进程恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class DeviceStatus(str, Enum):
    IN_STOCK = "IN_STOCK"    # 在库可流转
    RESERVED = "RESERVED"    # 暂占：替代履约已锁定，尚未交付家庭
    DELIVERED = "DELIVERED"  # 已交付家庭
    IN_REPAIR = "IN_REPAIR"  # 送修中
    RETIRED = "RETIRED"      # 报废


class Maintenance(str, Enum):
    OK = "OK"
    DUE = "DUE"              # 到期待维护
    CONDEMNED = "CONDEMNED"  # 维修结论为无法修复


class RecallStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"      # 待联系
    CONTACTED = "CONTACTED"  # 已联系，待归还
    COMPLETED = "COMPLETED"  # 已归还
    CANCELLED = "CANCELLED"  # 范围缩小或召回关闭后取消


class ContactResult(str, Enum):
    REACHED = "REACHED"          # 已联系到
    UNREACHABLE = "UNREACHABLE"  # 未联系到，任务自动升级


class SubState(str, Enum):
    RESERVED = "RESERVED"    # 暂占
    DELIVERED = "DELIVERED"  # 已交付家庭
    RETURNED = "RETURNED"    # 已归还


class RepairConclusion(str, Enum):
    FIXED = "FIXED"
    UNFIXABLE = "UNFIXABLE"


class EntryKind(str, Enum):
    CHARGE = "CHARGE"          # 当期结算
    ADJUSTMENT = "ADJUSTMENT"  # 已入账差额的调整，计入下一期，不覆盖旧账


# 联系任务分级：P1 最紧急。
GRADE_P1 = "P1"
GRADE_P2 = "P2"
GRADE_P3 = "P3"

_ESCALATION = {GRADE_P3: GRADE_P2, GRADE_P2: GRADE_P1, GRADE_P1: GRADE_P1}


def escalate(grade: str) -> str:
    """联系不到家庭时任务升一级。"""
    return _ESCALATION[grade]


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


@dataclass
class Device:
    serial: str
    model: str
    model_version: str
    batch: str
    daily_price: int          # 每日租金（元）
    subsidy_rate: float       # 补贴口径：比例
    subsidy_cap: int          # 补贴口径：每期上限（元）
    status: DeviceStatus = DeviceStatus.IN_STOCK
    maintenance: Maintenance = Maintenance.OK
    recall_holds: set[str] = field(default_factory=set)  # 生效中的召回编号
    manual_quarantine: bool = False                      # 人工隔离：只能人工解除
    repair_conclusion: RepairConclusion | None = None
    reinspection_signed: bool = False

    @property
    def blocked(self) -> bool:
        """召回锁定或人工隔离期间禁止交付与恢复流转。"""
        return bool(self.recall_holds) or self.manual_quarantine

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "model": self.model,
            "model_version": self.model_version,
            "batch": self.batch,
            "daily_price": self.daily_price,
            "subsidy_rate": self.subsidy_rate,
            "subsidy_cap": self.subsidy_cap,
            "status": self.status.value,
            "maintenance": self.maintenance.value,
            "recall_holds": sorted(self.recall_holds),
            "manual_quarantine": self.manual_quarantine,
            "repair_conclusion": self.repair_conclusion.value if self.repair_conclusion else None,
            "reinspection_signed": self.reinspection_signed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Device":
        return cls(
            serial=data["serial"],
            model=data["model"],
            model_version=data["model_version"],
            batch=data["batch"],
            daily_price=data["daily_price"],
            subsidy_rate=data["subsidy_rate"],
            subsidy_cap=data["subsidy_cap"],
            status=DeviceStatus(data["status"]),
            maintenance=Maintenance(data["maintenance"]),
            recall_holds=set(data["recall_holds"]),
            manual_quarantine=data["manual_quarantine"],
            repair_conclusion=RepairConclusion(data["repair_conclusion"]) if data["repair_conclusion"] else None,
            reinspection_signed=data["reinspection_signed"],
        )


@dataclass
class Loan:
    """交付关系：一台原始设备交付给一个家庭的履约。

    unusable_intervals 记录召回锁定/人工隔离导致的不可用区间（半开区间：
    锁定当日不可用，替代交付或解除当日恢复可用），结算时按实际可用天数计费。
    """

    loan_id: str
    device_serial: str
    family_id: str
    delivered_on: date
    original_returned_on: date | None = None
    closed_on: date | None = None  # 履约结案（家庭手中已无设备）
    unusable_intervals: list[tuple[date, date | None]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "loan_id": self.loan_id,
            "device_serial": self.device_serial,
            "family_id": self.family_id,
            "delivered_on": self.delivered_on.isoformat(),
            "original_returned_on": _iso(self.original_returned_on),
            "closed_on": _iso(self.closed_on),
            "unusable_intervals": [[s.isoformat(), _iso(e)] for s, e in self.unusable_intervals],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Loan":
        return cls(
            loan_id=data["loan_id"],
            device_serial=data["device_serial"],
            family_id=data["family_id"],
            delivered_on=date.fromisoformat(data["delivered_on"]),
            original_returned_on=_day(data["original_returned_on"]),
            closed_on=_day(data["closed_on"]),
            unusable_intervals=[(date.fromisoformat(s), _day(e)) for s, e in data["unusable_intervals"]],
        )


@dataclass
class Recall:
    recall_id: str
    model: str
    reason: str
    batches: set[str]
    created_on: date
    status: RecallStatus = RecallStatus.OPEN

    def to_dict(self) -> dict:
        return {
            "recall_id": self.recall_id,
            "model": self.model,
            "reason": self.reason,
            "batches": sorted(self.batches),
            "created_on": self.created_on.isoformat(),
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Recall":
        return cls(
            recall_id=data["recall_id"],
            model=data["model"],
            reason=data["reason"],
            batches=set(data["batches"]),
            created_on=date.fromisoformat(data["created_on"]),
            status=RecallStatus(data["status"]),
        )


@dataclass
class ContactTask:
    """已交付设备的分级联系与归还任务。"""

    task_id: str
    recall_id: str
    device_serial: str
    family_id: str
    grade: str
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    contact_result: str | None = None
    contacted_on: date | None = None
    returned_on: date | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "recall_id": self.recall_id,
            "device_serial": self.device_serial,
            "family_id": self.family_id,
            "grade": self.grade,
            "status": self.status.value,
            "attempts": self.attempts,
            "contact_result": self.contact_result,
            "contacted_on": _iso(self.contacted_on),
            "returned_on": _iso(self.returned_on),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContactTask":
        return cls(
            task_id=data["task_id"],
            recall_id=data["recall_id"],
            device_serial=data["device_serial"],
            family_id=data["family_id"],
            grade=data["grade"],
            status=TaskStatus(data["status"]),
            attempts=data["attempts"],
            contact_result=data["contact_result"],
            contacted_on=_day(data["contacted_on"]),
            returned_on=_day(data["returned_on"]),
        )


@dataclass
class Substitution:
    """替代履约：暂占 → 交付 → 归还，与原始履约一一对应。"""

    sub_id: str
    recall_id: str
    loan_id: str
    family_id: str
    original_serial: str
    substitute_serial: str
    state: SubState
    reserved_on: date
    delivered_on: date | None = None
    returned_on: date | None = None

    def to_dict(self) -> dict:
        return {
            "sub_id": self.sub_id,
            "recall_id": self.recall_id,
            "loan_id": self.loan_id,
            "family_id": self.family_id,
            "original_serial": self.original_serial,
            "substitute_serial": self.substitute_serial,
            "state": self.state.value,
            "reserved_on": self.reserved_on.isoformat(),
            "delivered_on": _iso(self.delivered_on),
            "returned_on": _iso(self.returned_on),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Substitution":
        return cls(
            sub_id=data["sub_id"],
            recall_id=data["recall_id"],
            loan_id=data["loan_id"],
            family_id=data["family_id"],
            original_serial=data["original_serial"],
            substitute_serial=data["substitute_serial"],
            state=SubState(data["state"]),
            reserved_on=date.fromisoformat(data["reserved_on"]),
            delivered_on=_day(data["delivered_on"]),
            returned_on=_day(data["returned_on"]),
        )


@dataclass
class LedgerEntry:
    """结算账目。CHARGE 入账后不再修改；差额以 ADJUSTMENT 计入下一期。"""

    entry_id: str
    loan_id: str
    family_id: str
    device_serial: str
    period: str  # YYYY-MM；ADJUSTMENT 计入被调整期间的下一期
    kind: EntryKind
    days: int
    amount: int
    subsidy_amount: int
    created_on: date
    adjusts: str | None = None  # 被调整的 CHARGE 账目
    paid: bool = False

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "loan_id": self.loan_id,
            "family_id": self.family_id,
            "device_serial": self.device_serial,
            "period": self.period,
            "kind": self.kind.value,
            "days": self.days,
            "amount": self.amount,
            "subsidy_amount": self.subsidy_amount,
            "created_on": self.created_on.isoformat(),
            "adjusts": self.adjusts,
            "paid": self.paid,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LedgerEntry":
        return cls(
            entry_id=data["entry_id"],
            loan_id=data["loan_id"],
            family_id=data["family_id"],
            device_serial=data["device_serial"],
            period=data["period"],
            kind=EntryKind(data["kind"]),
            days=data["days"],
            amount=data["amount"],
            subsidy_amount=data["subsidy_amount"],
            created_on=date.fromisoformat(data["created_on"]),
            adjusts=data["adjusts"],
            paid=data["paid"],
        )


@dataclass
class AuditEvent:
    """审计事件：召回决定、保管节点、联系结果与金额变化的追踪依据。"""

    seq: int
    on: date
    device_serial: str
    category: str  # RECALL / CUSTODY / CONTACT / MONEY
    action: str
    detail: dict
    operator: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "on": self.on.isoformat(),
            "device_serial": self.device_serial,
            "category": self.category,
            "action": self.action,
            "detail": self.detail,
            "operator": self.operator,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuditEvent":
        return cls(
            seq=data["seq"],
            on=date.fromisoformat(data["on"]),
            device_serial=data["device_serial"],
            category=data["category"],
            action=data["action"],
            detail=dict(data["detail"]),
            operator=data["operator"],
        )
