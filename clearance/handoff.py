"""Simulated finance handoff for HOLD and ESCALATE. Writes files only: nothing is sent.

The card uses validated facts and rule findings, never packet prose. Domains are defanged
and addresses withheld so finance cannot click through to a suspicious sender by accident.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from .outreach import _safe_reference
from .schemas import CaseResult, FinanceCase, FinanceFlag

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+")
DOMAIN = re.compile(r"\b([a-z0-9\-]+(?:\.[a-z0-9\-]+)*)\.([a-z]{2,})\b", re.I)
URL = re.compile(r"https?://|www\.", re.I)
ACCUSATORY = ["fraud", "scam", "criminal", "attacker", "fake"]

BLOCKED = "BLOCKED (simulated)"
ACTIONS = ("Confirmed legitimate", "Confirmed fraud", "Can't reach vendor")


def defang(text: str) -> str:
    text = EMAIL.sub("[address withheld]", text)
    text = URL.sub("", text)
    return DOMAIN.sub(lambda m: f"{m.group(1)}[.]{m.group(2)}", text)


def _amount(result: CaseResult) -> Optional[str]:
    c = result.claim
    return f"{c.currency} {c.amount:,.2f}" if c.amount is not None and c.currency else None


def _vendor(result: CaseResult) -> str:
    if result.company and result.company.name:
        return result.company.name
    name = defang(result.claim.vendor_name or "") or "Not identified"
    status = result.resolution.status if result.resolution else "UNVERIFIABLE"
    return f"{name} (claimed, {status.lower().replace('_', ' ')})"


def build_case(result: CaseResult, policy: dict, case_id: str) -> Optional[FinanceCase]:
    outcome = result.decision.outcome
    if outcome == "APPROVE":
        return None
    route = policy["routing"][outcome.lower()]
    flags = [
        FinanceFlag(rule_id=f.rule_id, severity=f.severity, message=defang(f.message), evidence_ids=f.evidence_ids)
        for f in result.findings if f.status == "FLAG"
    ]
    unverified = [f.rule_id for f in result.findings if f.status == "UNVERIFIABLE" and f.rule_id != "R12"]
    digest = hashlib.sha256(f"{case_id}|{outcome}".encode()).hexdigest()[:10]
    claim = result.claim
    account = f"ending {claim.bank_last4}" if claim.bank_last4 else None
    return FinanceCase(
        id=f"case-{digest}",
        outcome=outcome,
        route_to=route["route_to"],
        respond_within_hours=route["respond_within_hours"],
        release_requires=route["release_requires"],
        approvals_needed=route["approvals_needed"],
        vendor=_vendor(result),
        amount=_amount(result),
        reference=_safe_reference(claim.reference),
        bank_change=claim.bank_change,
        account=account,
        flags=flags,
        unverified=unverified,
        call_list=[defang(s.text) for s in result.callback_pack],
        document_requests=result.document_requests,
        data_incomplete=result.offline_misses > 0,
        payment_status=BLOCKED,
    )


def verify_case(case: FinanceCase, packet_text: str) -> FinanceCase:
    text = _body_text(case)
    lowered = text.lower()
    digit_runs = re.sub(r"\d[\d,]*\.\d\d", " ", text)
    checks = [
        ("no email addresses", not EMAIL.search(text)),
        ("no links", not URL.search(text)),
        ("domains defanged", not DOMAIN.search(text)),
        ("account masked to last four digits", not re.search(r"\d{6,}", digit_runs)),
        ("risk language, not an accusation", not any(w in lowered for w in ACCUSATORY)),
        ("every flag has evidence", all(f.evidence_ids for f in case.flags)),
        ("payment is blocked", case.payment_status == BLOCKED),
    ]
    case.checks = [{"check": name, "passed": bool(ok)} for name, ok in checks]
    return case


def _body_text(case: FinanceCase) -> str:
    parts = [case.vendor, case.amount or "", case.reference or "", case.account or ""]
    parts += [f.message for f in case.flags] + case.call_list + case.document_requests
    return "\n".join(parts)


def apply_action(case: FinanceCase, action: str) -> FinanceCase:
    """A person's simulated decision. Anything short of confirmation keeps the payment blocked."""
    if action not in ACTIONS:
        raise ValueError(f"Unknown action: {action}")
    case.resolution = action
    if action == "Confirmed fraud":
        case.payment_status = "BLOCKED, reported to security (simulated)"
    elif action == "Can't reach vendor":
        case.payment_status = "BLOCKED, callback still needed (simulated)"
    elif case.approvals_needed > 1:
        case.payment_status = "BLOCKED, second approver needed (simulated)"
    else:
        case.payment_status = "RELEASED by AP after callback (simulated)"
    return case


def to_markdown(case: FinanceCase) -> str:
    lines = [
        f"# Payment hold: {case.outcome} (SIMULATED, not sent)", "",
        f"- **Route to:** {case.route_to}",
        f"- **Respond within:** {case.respond_within_hours} hours",
        f"- **Payment:** {case.payment_status}",
        f"- **Release requires:** {case.release_requires}",
        f"- **Vendor:** {case.vendor}",
    ]
    if case.amount:
        lines.append(f"- **Amount:** {case.amount}")
    if case.reference:
        lines.append(f"- **Reference:** {case.reference}")
    if case.bank_change:
        lines.append(f"- **Bank change requested:** yes{', account ' + case.account if case.account else ''}")
    if case.resolution:
        lines.append(f"- **Recorded action:** {case.resolution}")
    if case.data_incomplete:
        lines += ["", "**Data incomplete:** some lookups did not run, so this reflects missing data rather than a finding."]
    if case.flags:
        lines += ["", "## Why it was held", *[f"- {f.rule_id} ({f.severity}): {f.message} [{' '.join(f.evidence_ids[:4])}]" for f in case.flags]]
    if case.unverified:
        lines += ["", f"**Could not verify:** {', '.join(case.unverified)}"]
    if case.call_list:
        lines += ["", "## Callback", *[f"- {c}" for c in case.call_list]]
    if case.document_requests:
        lines += ["", "## Documents to request", *[f"- {d}" for d in case.document_requests]]
    lines += ["", "These are risk findings for review, not an accusation against the vendor or any person."]
    return "\n".join(lines) + "\n"


def write_case(case: FinanceCase, outbox: Path) -> list[Path]:
    outbox.mkdir(parents=True, exist_ok=True)
    js, md = outbox / f"{case.id}.json", outbox / f"{case.id}.md"
    js.write_text(json.dumps(case.model_dump(mode="json"), indent=1, ensure_ascii=False))
    md.write_text(to_markdown(case))
    return [js, md]
