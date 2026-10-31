"""Vendor verification drafts. Draft-only: nothing in this project can send a message.

The body is a fixed template, so no packet prose or LLM text can reach it. Only
validated facts are filled in, and a failed check blocks the draft.
"""
from __future__ import annotations

import hashlib
import json
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from .people import names_match
from .rules import sender_domain
from .schemas import Claim, CompanyProfile, Draft, PersonProfile, Recipient

ACCUSATORY = ["fraud", "scam", "suspicious", "suspect", "impersonat", "lookalike", "attacker", "fake", "phishing", "criminal"]
NO_ADDRESS = "address-from-your-vendor-records@invalid"


def _safe_reference(reference: Optional[str]) -> Optional[str]:
    return reference if reference and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/\-]{0,30}", reference) else None


def compose(claim: Claim, company: CompanyProfile, contacts: list[PersonProfile], case_id: str) -> Optional[Draft]:
    recipients = [c for c in contacts if c.has_business_email]
    if not recipients:
        return None
    vendor = company.name or claim.vendor_name or "your company"
    ref = _safe_reference(claim.reference)
    facts = []
    if ref:
        facts.append(f"Reference: {ref}")
    if claim.amount is not None and claim.currency:
        facts.append(f"Amount: {claim.currency} {claim.amount:,.2f}")
    if claim.bank_change:
        last4 = f"ending {claim.bank_last4}" if claim.bank_last4 else "(details withheld)"
        facts.append(f"Requested change: payment details to an account {last4}")
    primary = recipients[0]
    body = "\n".join([
        f"Hello {(primary.name or '').split()[0]},",
        "",
        f"We are confirming a recent payment request received in the name of {vendor}, as part of our standard vendor checks.",
        "",
        *facts,
        "",
        "Could you, or the right person on your team, confirm whether this request is accurate? "
        "We will also call your office using the phone number in our own vendor records.",
        "",
        "Please reply in a new message rather than forwarding any earlier correspondence.",
        "",
        "Thank you,",
        "Accounts Payable",
    ])
    draft_id = hashlib.sha256(f"{case_id}|{primary.crustdata_person_id}".encode()).hexdigest()[:10]
    return Draft(
        id=f"draft-{draft_id}",
        recipients=[
            Recipient(name=c.name or "", rationale=f"{c.current_title} at {vendor}, from the graph", evidence_ids=list(c.evidence.values()))
            for c in recipients
        ],
        subject=f"Confirming a payment request for {vendor}",
        body=body,
    )


def verify_draft(draft: Draft, claim: Claim, packet_text: str) -> Draft:
    text = f"{draft.subject}\n{draft.body}"
    lowered = text.lower()
    domain = sender_domain(claim.sender_email)
    checks = [
        ("recipients come from the graph, not the sender", all(
            not names_match(n, r.name) for r in draft.recipients for n in (claim.sender_name, claim.signature_name) if n)),
        ("every recipient has evidence", all(r.evidence_ids for r in draft.recipients)),
        ("no packet email address or domain in the draft", not (
            (claim.sender_email and claim.sender_email in lowered) or (domain and domain in lowered))),
        ("no links", not re.search(r"https?://|www\.", lowered)),
        ("no accusation or detection language", not any(w in lowered for w in ACCUSATORY)),
        ("bank details masked to last four digits", not re.search(r"\d{6,}", re.sub(r"\d[\d,]*\.\d\d", " ", text))),
        ("amount matches the claim", claim.amount is None or f"{claim.amount:,.2f}" in text),
    ]
    draft.checks = [{"check": name, "passed": bool(ok)} for name, ok in checks]
    draft.status = "DRAFT_ONLY" if all(c["passed"] for c in draft.checks) else "BLOCKED"
    return draft


def write_outbox(draft: Draft, outbox: Path) -> list[Path]:
    outbox.mkdir(parents=True, exist_ok=True)
    meta = outbox / f"{draft.id}.json"
    meta.write_text(json.dumps(draft.model_dump(), indent=1, ensure_ascii=False))
    if draft.status != "DRAFT_ONLY":
        return [meta]  # A blocked draft is kept for review but never rendered as an email.
    msg = EmailMessage()
    msg["To"] = ", ".join(f'"{r.name}" <{NO_ADDRESS}>' for r in draft.recipients)
    msg["Subject"] = draft.subject
    msg["X-Clearance-Status"] = "DRAFT_ONLY"
    msg.set_content(draft.body)
    eml = outbox / f"{draft.id}.eml"
    eml.write_bytes(bytes(msg))
    return [meta, eml]
