"""Hash-chained evidence ledger: a tamper-evident trail of what the agent relied on."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from .schemas import Evidence

GENESIS = "0" * 64


def _digest(fields: dict) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _body(ev: Evidence) -> dict:
    return ev.model_dump(exclude={"hash"})


class Ledger:
    def __init__(self) -> None:
        self.records: list[Evidence] = []

    def add(
        self,
        value: Any,
        source: str = "crustdata",
        endpoint: Optional[str] = None,
        entity_id: Any = None,
        field_path: Optional[str] = None,
        credits: float = 0.0,
        retrieved_at: Optional[str] = None,
    ) -> str:
        prev = self.records[-1].hash if self.records else GENESIS
        ev = Evidence(
            id=f"E{len(self.records) + 1:03d}",
            source=source,
            endpoint=endpoint,
            entity_id=None if entity_id is None else str(entity_id),
            field_path=field_path,
            value=value,
            retrieved_at=retrieved_at or datetime.now(timezone.utc).isoformat(),
            credits=credits,
            prev_hash=prev,
            hash="",
        )
        ev.hash = _digest(_body(ev))
        self.records.append(ev)
        return ev.id

    def ids(self) -> set[str]:
        return {r.id for r in self.records}


def verify_chain(records: list[Evidence]) -> bool:
    prev = GENESIS
    for ev in records:
        if ev.prev_hash != prev or ev.hash != _digest(_body(ev)):
            return False
        prev = ev.hash
    return True
