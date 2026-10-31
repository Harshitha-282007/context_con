"""Markdown + JSON case report. Every line carries its evidence ids."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .schemas import CaseResult, Statement

def offline_warning(result: CaseResult) -> str:
    if not result.offline_misses:
        return ""
    return (
        f"Incomplete offline run: {result.offline_misses} lookup(s) had no saved response, so this decision "
        "reflects missing data, not a finding about the vendor or sender. Turn off offline replay to run it live."
    )


LIMITS = (
    "Limits: bank accounts are not verified, profile data can lag, and small-vendor coverage may be thin. "
    "These are risk findings, not accusations. The callback is a human step. No message was sent."
)


def _cite(ids: list[str]) -> str:
    return " ".join(f"[{i}]" for i in ids[:6]) + (f" +{len(ids) - 6} more" if len(ids) > 6 else "")


def _line(s: Statement) -> str:
    return f"- {s.text} {_cite(s.evidence_ids)}"


def person_names(result: CaseResult) -> list[str]:
    names = [c.name for c in result.contacts if c.name]
    pc = result.person_check
    if pc and pc.person and pc.person.name:
        names.append(pc.person.name)
    names += [n for n in (result.claim.sender_name, result.claim.signature_name) if n]
    names += [r.name for d in result.drafts for r in d.recipients]
    return sorted(set(names), key=len, reverse=True)


def mask(text: str, names: list[str]) -> str:
    for name in names:
        initials = " ".join(p[0] + "***" for p in name.split())
        text = re.sub(re.escape(name), initials, text, flags=re.I)
        first = name.split()[0]
        text = re.sub(rf"\bHello {re.escape(first)}\b", f"Hello {first[0]}***", text)
    return re.sub(r"[A-Za-z0-9._%+\-]+@", "***@", text)


def to_markdown(result: CaseResult, masked: bool = False) -> str:
    d = result.decision
    pack_texts = {s.text for s in result.callback_pack}
    lines = [f"# Clearance report: {d.outcome}", ""]
    if result.offline_misses:
        lines += [f"> **{offline_warning(result)}**", ""]
    lines += ["## Reasons", *[f"- {r}" for r in d.reasons], ""]
    lines += ["## Evidence", *[_line(s) for s in result.statements if s.text not in pack_texts], ""]
    lines += ["## Rule trace", *[
        f"- **{f.rule_id}** {f.status}{' (' + f.severity + ')' if f.status == 'FLAG' else ''}: {f.message} {_cite(f.evidence_ids)}"
        for f in result.findings
    ], ""]
    kept_pack = [s for s in result.statements if s.text in pack_texts]
    if kept_pack:
        lines += ["## Callback pack", *[_line(s) for s in kept_pack], ""]
    elif d.outcome != "APPROVE" and result.company:
        why = (
            f"The contact search could not run: {result.contact_search_error}."
            if result.contact_search_error else "No independent contact was found in the graph."
        )
        lines += ["## Callback pack", f"- {why} Use your own vendor records.", ""]
    if result.document_requests:
        lines += ["## Documents to request", *[f"- {r}" for r in result.document_requests], ""]
    for draft in result.drafts:
        lines += [f"## Draft ({draft.status}, never sent)", f"To: {', '.join(r.name for r in draft.recipients)} (address from your own vendor records)",
                  f"Subject: {draft.subject}", "", draft.body, "",
                  *[f"- {'pass' if c['passed'] else 'FAIL'}: {c['check']}" for c in draft.checks], ""]
    fc = result.finance_case
    if fc:
        lines += [f"## Finance handoff (simulated, not sent)", f"- Route to: {fc.route_to}, within {fc.respond_within_hours} hours",
                  f"- Payment: {fc.payment_status}", f"- Release requires: {fc.release_requires}",
                  f"- Case file: outbox/{fc.id}.md", ""]
    if d.outcome != "APPROVE" and not result.drafts and result.contacts:
        lines += ["## Draft", "- No draft: no contact has a business email on file. Use the callback pack instead.", ""]
    lines += ["## What evidence would change this decision?", f"- {d.what_would_change_this}", ""]
    if result.dropped_statements:
        lines += ["## Dropped for lack of evidence", *[f"- {t}" for t in result.dropped_statements], ""]
    lines += ["## Run", f"- Extraction: {result.extraction_method}",
              f"- Credits: {result.credits_used} spent live, {result.credits_replayed} replayed from cache",
              f"- Evidence ledger: {len(result.ledger)} records; hash chain {'verified' if result.chain_ok else 'BROKEN'}",
              f"- Messages sent: 0", "", LIMITS, ""]
    text = "\n".join(lines)
    return mask(text, person_names(result)) if masked else text


def write_report(result: CaseResult, outputs: Path, case_id: str, masked: bool = False) -> tuple[Path, Path]:
    outputs.mkdir(parents=True, exist_ok=True)
    md, js = outputs / f"{case_id}.md", outputs / f"{case_id}.json"
    md.write_text(to_markdown(result, masked))
    js.write_text(json.dumps(result.model_dump(mode="json"), indent=1, ensure_ascii=False))
    return md, js
