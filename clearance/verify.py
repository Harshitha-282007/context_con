"""Cite-or-drop: a statement without valid evidence never reaches the report."""
from __future__ import annotations

from .ledger import verify_chain
from .schemas import Evidence, Statement


def cite_or_drop(statements: list[Statement], ledger: list[Evidence]) -> tuple[list[Statement], list[str], bool]:
    known = {e.id for e in ledger}
    kept, dropped = [], []
    for s in statements:
        if s.evidence_ids and all(i in known for i in s.evidence_ids):
            kept.append(s)
        else:
            dropped.append(s.text)
    return kept, dropped, verify_chain(ledger)
