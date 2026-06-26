from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PLAN_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_run_id(prefix: str) -> str:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H-%M-%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class Operation:
    type: str
    dest: str | None = None
    src: str | None = None
    reason: str | None = None
    expected_size: int | None = None
    expected_sha256: str | None = None
    text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    run_id: str
    description: str
    created_at: str
    operations: list[Operation]
    version: int = PLAN_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "description": self.description,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "operations": [asdict(operation) for operation in self.operations],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Plan":
        operations = [Operation(**operation) for operation in payload.get("operations", [])]
        return cls(
            version=payload["version"],
            run_id=payload["run_id"],
            description=payload["description"],
            created_at=payload["created_at"],
            metadata=payload.get("metadata", {}),
            operations=operations,
        )

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for operation in self.operations:
            counts[operation.type] = counts.get(operation.type, 0) + 1
        parts = ", ".join(f"{kind}: {count}" for kind, count in sorted(counts.items()))
        return f"{self.description} ({self.run_id}) - {parts or 'no operations'}"


def make_plan(
    *,
    run_id: str,
    description: str,
    operations: Iterable[Operation],
    metadata: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> Plan:
    return Plan(
        run_id=run_id,
        description=description,
        created_at=utc_now_iso(),
        metadata=metadata or {},
        operations=list(operations),
        runtime=runtime or {},
    )


def write_plan(plan: Plan, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_plan(path: Path) -> Plan:
    return Plan.from_dict(json.loads(path.read_text(encoding="utf-8")))
