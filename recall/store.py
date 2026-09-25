"""服务状态的 JSON 持久化。

每次变更后原子写入（临时文件 + 替换）。进程崩溃或重启后通过
RecallService.open(path) 恢复；幂等键一并持久化，恢复后重放同一
操作不会重复发放替代设备或多算补贴。
"""

from __future__ import annotations

import json
import os

from .models import (
    AuditEvent,
    ContactTask,
    Device,
    LedgerEntry,
    Loan,
    Recall,
    Substitution,
)


class Store:
    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self.loans: dict[str, Loan] = {}
        self.recalls: dict[str, Recall] = {}
        self.tasks: dict[str, ContactTask] = {}
        self.substitutions: dict[str, Substitution] = {}
        self.ledger: dict[str, LedgerEntry] = {}
        self.audit: list[AuditEvent] = []
        self.family_quotas: dict[str, int] = {}
        self.idempotency: dict[str, list] = {}  # 幂等键 -> [类型, 引用]
        self.seq = 0

    def to_dict(self) -> dict:
        return {
            "devices": {k: v.to_dict() for k, v in self.devices.items()},
            "loans": {k: v.to_dict() for k, v in self.loans.items()},
            "recalls": {k: v.to_dict() for k, v in self.recalls.items()},
            "tasks": {k: v.to_dict() for k, v in self.tasks.items()},
            "substitutions": {k: v.to_dict() for k, v in self.substitutions.items()},
            "ledger": {k: v.to_dict() for k, v in self.ledger.items()},
            "audit": [e.to_dict() for e in self.audit],
            "family_quotas": dict(self.family_quotas),
            "idempotency": dict(self.idempotency),
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Store":
        store = cls()
        store.devices = {k: Device.from_dict(v) for k, v in data["devices"].items()}
        store.loans = {k: Loan.from_dict(v) for k, v in data["loans"].items()}
        store.recalls = {k: Recall.from_dict(v) for k, v in data["recalls"].items()}
        store.tasks = {k: ContactTask.from_dict(v) for k, v in data["tasks"].items()}
        store.substitutions = {k: Substitution.from_dict(v) for k, v in data["substitutions"].items()}
        store.ledger = {k: LedgerEntry.from_dict(v) for k, v in data["ledger"].items()}
        store.audit = [AuditEvent.from_dict(e) for e in data["audit"]]
        store.family_quotas = dict(data["family_quotas"])
        store.idempotency = {k: list(v) for k, v in data["idempotency"].items()}
        store.seq = data["seq"]
        return store

    def save(self, path: str) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Store":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))
