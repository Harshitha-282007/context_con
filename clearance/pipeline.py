"""Packet -> decision + action package. Each stage degrades to UNVERIFIABLE; none can crash the run."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable, Optional

from .config import OUTBOX_DIR
from .crustdata import CrustdataClient
from .decide import decide
from .draft_llm import polish
from .extract import extract, extract_regex
from .handoff import build_case, verify_case, write_case
from .fieldmap import COMPANY_ENRICH_FIELDS, COMPANY_PATHS, company_profile, match_rows
from .ledger import Ledger
from .outreach import compose, verify_draft, write_outbox
from .people import find_contacts, verify_sender
from .resolve import check_sender_domain, resolve_vendor
from .rules import RuleContext, coverage_adequate, run_rules
from .schemas import CaseResult, Claim, CompanyProfile, Finding, PersonProfile, Resolution, Statement
from .verify import cite_or_drop

Emit = Callable[[str, str], None]
MIN_PACKET_CHARS = 20
CLAIM_FIELDS = ("vendor_name", "claimed_domain", "sender_name", "sender_email", "signature_name", "signature_title",
                "amount", "currency", "services", "reference", "bank_change", "bank_last4", "urgency_phrases")


def _record_claim(ledger: Ledger, claim: Claim) -> dict[str, str]:
    ids = {}
    for field in CLAIM_FIELDS:
        value = getattr(claim, field)
        if value not in (None, [], False):
            ids[field] = ledger.add(value, source="packet", field_path=f"claim.{field}")
    return ids


def _enrich(client: CrustdataClient, ledger: Ledger, resolution: Resolution) -> Optional[CompanyProfile]:
    candidate = resolution.company
    result = client.post("/company/enrich", {"crustdata_company_ids": [candidate.crustdata_company_id], "fields": COMPANY_ENRICH_FIELDS})
    ledger.add({"status": result.status, "error": result.error}, endpoint="/company/enrich", entity_id=candidate.crustdata_company_id,
               field_path="[].matches[].company_data", credits=result.credits, retrieved_at=result.retrieved_at or None, raw_ref=result.fixture_key)
    rows = match_rows(result.data) if result.ok else []
    if rows:
        profile = company_profile(rows[0].get("company_data"))
        source = "/company/enrich"
    else:  # Identify still gave the identity, so the domain check can run on that.
        profile = CompanyProfile(
            crustdata_company_id=candidate.crustdata_company_id, name=candidate.name, primary_domain=candidate.primary_domain,
            all_domains=[candidate.primary_domain] if candidate.primary_domain else [],
        )
        source = "/company/identify"
    profile.crustdata_company_id = profile.crustdata_company_id or candidate.crustdata_company_id
    for field, path in COMPANY_PATHS.items():
        value = getattr(profile, field)
        if value not in (None, []):
            profile.evidence[field] = ledger.add(value, endpoint=source, entity_id=profile.crustdata_company_id, field_path=path,
                                                 retrieved_at=result.retrieved_at or None, raw_ref=result.fixture_key)
    return profile


def _ensure_cited(findings: list[Finding], ledger: Ledger) -> None:
    for f in findings:
        if not f.evidence_ids:
            f.evidence_ids = [ledger.add({"rule": f.rule_id, "note": f.message}, source="system", field_path="no_data")]


def _callback_pack(contacts: list[PersonProfile], company: CompanyProfile, ledger: Ledger) -> list[Statement]:
    pack = []
    for i, c in enumerate(contacts):
        label = "Call first" if i == 0 else "Second contact"
        since = f", in the role since {c.start_date:%b %Y}" if c.start_date else ""
        email = "has a verified business email" if c.business_email_verified else "has a business email on file" if c.has_business_email else "no business email on file"
        pack.append(Statement(text=f"{label}: {c.name}, {c.current_title} at {company.name}{since}; {email}.", evidence_ids=list(c.evidence.values())))
    if pack:
        cited = pack[0].evidence_ids
        pack.append(Statement(text="Use a phone number from your own vendor records, never one from this packet or thread. Do not reply on the thread.", evidence_ids=cited))
        pack.append(Statement(text='Ask: "Did your company send this request, and who sent it?"', evidence_ids=cited))
        pack.append(Statement(text='Ask: "Please read back the account number you want us to pay."', evidence_ids=cited))
        pack.append(Statement(text='Ask: "Which invoice and amount does this relate to, and who approved the change?"', evidence_ids=cited))
    return pack


def _document_requests(result: CaseResult) -> list[str]:
    res = result.resolution
    requests = []
    if res is None or res.status != "RESOLVED":
        requests += ["Vendor's legal entity name and registered website domain", "Vendor master record or W-9/tax form for this vendor"]
    if result.claim.amount is None:
        requests.append("The invoice itself, showing amount and currency")
    if not result.claim.sender_email:
        requests.append("The original email with full headers, showing the actual sender")
    if not result.claim.reference:
        requests.append("Purchase order or contract reference")
    return requests


def _statements(result: CaseResult, ledger: Ledger, policy: dict) -> list[Statement]:
    out: list[Statement] = []
    finding_ids = [i for f in result.findings for i in f.evidence_ids]
    d = result.decision
    out.append(Statement(text=f"Decision: {d.outcome}.", evidence_ids=sorted(set(finding_ids))))
    res = result.resolution
    if res:
        out.append(Statement(text=f"Vendor resolution: {res.status} ({res.reason}).", evidence_ids=res.evidence_ids))
        if res.status == "AMBIGUOUS":
            for c in res.candidates:
                out.append(Statement(text=f"Candidate: {c.name}, {c.primary_domain or 'no domain on file'}, {c.employee_count_range or 'size unknown'}, confidence {c.confidence}.", evidence_ids=c.evidence_ids))
    co = result.company
    if co:
        ev = co.evidence
        if "headcount" in ev:
            out.append(Statement(text=f"{co.name} has {co.headcount} employees.", evidence_ids=[ev["headcount"]]))
        if d.outcome == "APPROVE" and "headcount" in ev and co.headcount:
            limit = policy["credit_limit"]
            usd = min(limit["per_head_usd"] * co.headcount, limit["max_usd"])
            out.append(Statement(text=f"Suggested credit limit (illustrative policy, not a finding): ${usd:,.0f}.", evidence_ids=[ev["headcount"]]))
        if "primary_domain" in ev:
            out.append(Statement(text=f"Primary domain: {co.primary_domain}.", evidence_ids=[ev["primary_domain"]]))
        if "revenue_upper_usd" in ev:
            out.append(Statement(text=f"Estimated annual revenue up to ${co.revenue_upper_usd:,.0f}.", evidence_ids=[ev["revenue_upper_usd"]]))
        if "monthly_visitors" in ev:
            out.append(Statement(text=f"About {co.monthly_visitors:,.0f} monthly web visitors.", evidence_ids=[ev["monthly_visitors"]]))
    pc = result.person_check
    if pc:
        ctx = RuleContext(claim=result.claim, as_of=date.today(), resolution=res, company=co, person_check=pc)
        _, why = coverage_adequate(ctx, policy)
        out.append(Statement(text=f"Sender check: {pc.status}{' (' + pc.reason + ')' if pc.reason else ''}. Coverage: {why}.", evidence_ids=pc.evidence_ids))
        if pc.email_lookup_unsupported:
            out.append(Statement(text="Email reverse lookup is not enabled on this API key (403), so this check used full-name and surname searches.", evidence_ids=pc.evidence_ids))
        if pc.person:
            p = pc.person
            out.append(Statement(text=f"Sender in the graph: {p.name}, {p.current_title}.", evidence_ids=list(p.evidence.values())))
    return out


def run_case(
    packet_text: str, client: CrustdataClient, policy: dict, as_of: date, case_id: str = "case",
    emit: Emit = lambda stage, message: None, outbox: Path = OUTBOX_DIR,
) -> CaseResult:
    """Never raises. If anything unexpected breaks mid-run, the payment stays on HOLD."""
    ledger = Ledger()
    try:
        return _run(ledger, packet_text, client, policy, as_of, case_id, emit, outbox)
    except Exception as error:  # The type alone is recorded, because a message can echo private data.
        emit("error", f"the check did not finish ({type(error).__name__}); failing closed")
        return _fail_closed(ledger, packet_text, client, policy, as_of, case_id, outbox, type(error).__name__)


def _fail_closed(
    ledger: Ledger, packet_text: str, client: CrustdataClient, policy: dict, as_of: date, case_id: str, outbox: Path, error: str,
) -> CaseResult:
    """Keeps the evidence gathered so far and returns HOLD with every unfinished check UNVERIFIABLE."""
    try:
        claim = extract_regex(packet_text or "")
    except Exception:
        claim = Claim()
    ev = ledger.add({"error": error, "note": "the check did not finish"}, source="system", field_path="run")
    resolution = Resolution(status="UNVERIFIABLE", reason=f"the check did not finish ({error})", evidence_ids=[ev])
    findings = run_rules(RuleContext(claim=claim, as_of=as_of, resolution=resolution), policy)
    _ensure_cited(findings, ledger)
    result = CaseResult(claim=claim, resolution=resolution, findings=findings, extraction_method="regex (run did not finish)")
    result.decision = decide(findings, resolution, policy)
    result.document_requests = _document_requests(result)
    result.offline_misses = client.offline_misses
    result.credits_used = round(client.credits_spent, 2)
    result.credits_replayed = round(client.credits_replayed, 2)
    case = build_case(result, policy, case_id)
    if case:
        result.finance_case = verify_case(case, packet_text or "")
        write_case(result.finance_case, outbox)
    kept, dropped, chain_ok = cite_or_drop(_statements(result, ledger, policy), ledger.records)
    result.statements, result.dropped_statements, result.chain_ok = kept, dropped, chain_ok
    result.ledger = ledger.records
    return result


def _run(
    ledger: Ledger, packet_text: str, client: CrustdataClient, policy: dict, as_of: date, case_id: str, emit: Emit, outbox: Path,
) -> CaseResult:
    if len((packet_text or "").strip()) < MIN_PACKET_CHARS:
        emit("extract", "packet is empty or too short")
        claim, method = Claim(), "none (insufficient packet)"
    else:
        claim, method = extract(packet_text, use_llm=not client.offline)
        emit("extract", f"{method}: vendor={claim.vendor_name!r} amount={claim.amount} {claim.currency or ''} bank_change={claim.bank_change}")
    claim_ev = _record_claim(ledger, claim)
    result = CaseResult(claim=claim, extraction_method=method)

    resolution = resolve_vendor(client, ledger, claim, policy)
    if not resolution.evidence_ids:  # No lookup ran, so the gap itself is what gets recorded.
        resolution.evidence_ids = [ledger.add({"status": resolution.status, "reason": resolution.reason}, source="system", field_path="resolution")]
    result.resolution = resolution
    emit("resolve", f"{resolution.status}: {resolution.reason}")

    company, person_check, domain_check = None, None, None
    if resolution.status == "RESOLVED":
        company = _enrich(client, ledger, resolution)
        emit("enrich", f"{company.name}: headcount={company.headcount} visitors={company.monthly_visitors} revenue_upper={company.revenue_upper_usd}")
        known = [d for d in [company.primary_domain, *company.all_domains] if d]
        domain_check = check_sender_domain(client, ledger, claim, known, policy)
        if domain_check:
            emit("domain", f"{domain_check.domain} in the graph: {domain_check.company_name or 'no company'}")
        person_check = verify_sender(client, ledger, claim, company.crustdata_company_id)
        emit("person", f"sender {person_check.status}; graph lists {person_check.employees_found} current employees")
    result.company, result.person_check = company, person_check

    ctx = RuleContext(claim=claim, as_of=as_of, resolution=resolution, company=company, person_check=person_check, claim_evidence=claim_ev, sender_domain_check=domain_check)
    findings = run_rules(ctx, policy)
    _ensure_cited(findings, ledger)
    result.findings = findings
    for f in findings:
        emit("rule", f"{f.rule_id} {f.status} {f.severity if f.status == 'FLAG' else ''}: {f.message}")

    decision = decide(findings, resolution, policy)
    result.decision = decision
    emit("decide", decision.outcome)

    if decision.outcome != "APPROVE":
        result.document_requests = _document_requests(result)
        if resolution.status == "RESOLVED" and company:
            result.contacts, result.contact_search_error = find_contacts(client, ledger, claim, company.crustdata_company_id, company.headcount, packet_text or "")
            result.callback_pack = _callback_pack(result.contacts, company, ledger)
            emit("contacts", f"{len(result.contacts)} graph-sourced callback contact(s)")
            draft = compose(claim, company, result.contacts, case_id)
            if draft:
                # Offline replay stays fully deterministic, so it keeps the template wording.
                worded = draft if client.offline else polish(draft, claim, company)
                checked = verify_draft(worded, claim, packet_text or "")
                draft = checked if checked.status == "DRAFT_ONLY" else verify_draft(draft, claim, packet_text or "")
                write_outbox(draft, outbox)
                result.drafts = [draft]
                emit("draft", f"{draft.status}: {sum(c['passed'] for c in draft.checks)}/{len(draft.checks)} checks passed")

    result.offline_misses = client.offline_misses
    case = build_case(result, policy, case_id)
    if case:
        case = verify_case(case, packet_text or "")
        write_case(case, outbox)
        result.finance_case = case
        emit("handoff", f"{case.status}: {case.route_to}; payment {case.payment_status}")
    result.credits_used = round(client.credits_spent, 2)
    result.credits_replayed = round(client.credits_replayed, 2)
    statements = _statements(result, ledger, policy) + result.callback_pack
    kept, dropped, chain_ok = cite_or_drop(statements, ledger.records)
    result.statements, result.dropped_statements, result.chain_ok = kept, dropped, chain_ok
    result.ledger = ledger.records
    emit("verify", f"chain_ok={chain_ok}; {len(kept)} statements kept, {len(dropped)} dropped")
    return result
