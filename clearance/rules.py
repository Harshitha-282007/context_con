"""Deterministic rules. Pure functions of RuleContext + policy; no I/O and no LLM.

Missing data yields UNVERIFIABLE, never PASS and never FLAG.
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .schemas import Claim, CompanyProfile, Finding, PersonCheck, Resolution, SenderDomainCheck


@dataclass
class RuleContext:
    claim: Claim
    as_of: date
    resolution: Optional[Resolution] = None
    company: Optional[CompanyProfile] = None
    person_check: Optional[PersonCheck] = None
    claim_evidence: dict[str, str] = field(default_factory=dict)  # Claim field -> evidence id
    sender_domain_check: Optional[SenderDomainCheck] = None


# --- helpers ---

def _ids(*groups) -> list[str]:
    out: list[str] = []
    for g in groups:
        for i in ([g] if isinstance(g, str) else (g or [])):
            if i and i not in out:
                out.append(i)
    return out


def _company_ev(ctx: RuleContext, *names: str) -> list[str]:
    ev = ctx.company.evidence if ctx.company else {}
    res = ctx.resolution.evidence_ids if ctx.resolution else []
    return _ids([ev[n] for n in names if n in ev], res)


def _person_ev(ctx: RuleContext) -> list[str]:
    return ctx.person_check.evidence_ids if ctx.person_check else []


def _claim_ev(ctx: RuleContext, *names: str) -> list[str]:
    return [ctx.claim_evidence[n] for n in names if n in ctx.claim_evidence]


def _unverifiable(rule: str, message: str, ev: list[str]) -> Finding:
    return Finding(rule_id=rule, status="UNVERIFIABLE", message=message, evidence_ids=ev)


def _flag(rule: str, severity: str, message: str, ev: list[str]) -> Finding:
    return Finding(rule_id=rule, status="FLAG", severity=severity, message=message, evidence_ids=ev)


def _pass(rule: str, message: str, ev: list[str]) -> Finding:
    return Finding(rule_id=rule, status="PASS", message=message, evidence_ids=ev)


def sender_domain(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower().rstrip(".") or None


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


_HOMOGLYPHS = str.maketrans({
    "0": "o", "1": "l", "3": "e", "5": "s", "|": "l", "i": "l",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "і": "l",  # Cyrillic
})


def homoglyph_skeleton(domain: str) -> str:
    d = unicodedata.normalize("NFKC", domain.lower()).replace("rn", "m").replace("vv", "w")
    return d.translate(_HOMOGLYPHS)


def amount_usd(claim: Claim, policy: dict) -> Optional[float]:
    if claim.amount is None:
        return None
    rate = policy["fx_to_usd"].get((claim.currency or "").upper())
    return None if rate is None else claim.amount * rate


def is_ambiguous_or_unresolved(ctx: RuleContext) -> bool:
    return ctx.resolution is None or ctx.resolution.status != "RESOLVED"


def _company_gap(ctx: RuleContext) -> str:
    if ctx.resolution is None:
        return "vendor was not resolved"
    return f"vendor resolution is {ctx.resolution.status}"


def coverage_adequate(ctx: RuleContext, policy: dict) -> tuple[bool, str]:
    """Is 'not found at this company' meaningful? Gate for a hard R2."""
    cov = policy["coverage"]
    res = ctx.resolution
    if res is None or res.status != "RESOLVED" or res.company is None:
        return False, _company_gap(ctx)
    conf = res.company.confidence
    if conf is None or conf < cov["min_resolve_confidence"]:
        return False, f"vendor match confidence {conf} is below {cov['min_resolve_confidence']}"
    headcount = ctx.company.headcount if ctx.company else None
    if headcount is None:
        return False, "vendor headcount is unknown"
    found = ctx.person_check.employees_found if ctx.person_check else None
    if found is None:
        return False, "the graph's employee count for the vendor is unknown"
    needed = min(cov["min_people"], math.ceil(cov["headcount_share"] * headcount))
    if found < needed:
        return False, f"graph lists {found} current employees; {needed} needed for a meaningful absence"
    return True, f"graph lists {found} current employees (needed {needed})"


# --- rules ---

def r1_domain(ctx: RuleContext, policy: dict) -> Finding:
    ev = _ids(_claim_ev(ctx, "sender_email"), _company_ev(ctx, "primary_domain"))
    sender = sender_domain(ctx.claim.sender_email)
    if sender is None:
        return _unverifiable("R1", "No sender email address in the packet.", ev)
    if sender in policy["r5"]["free_mail_domains"]:
        return _unverifiable("R1", f"Sender uses free mail ({sender}), so there is no company domain to compare; see R5.", ev)
    if is_ambiguous_or_unresolved(ctx):
        return _unverifiable("R1", f"Cannot compare sender domain: {_company_gap(ctx)}.", ev)
    primary = ctx.company.primary_domain if ctx.company else None
    if not primary:
        return _unverifiable("R1", "Vendor's primary domain is unknown.", ev)
    primary = primary.lower()
    known = {primary, *(d.lower() for d in ctx.company.all_domains)}
    if any(sender == d or sender.endswith("." + d) for d in known):
        return _pass("R1", f"Sender domain {sender} belongs to the vendor ({primary}).", ev)
    check = ctx.sender_domain_check
    if check and check.domain == sender and check.company_id is not None and check.company_id == ctx.company.crustdata_company_id:
        return _pass("R1", f"The graph lists {sender} under the vendor, alongside {primary}.", _ids(ev, check.evidence_ids))
    close = (
        edit_distance(sender, primary) <= policy["r1"]["max_edit_distance"]
        or homoglyph_skeleton(sender) == homoglyph_skeleton(primary)
    )
    kind = "is a lookalike of" if close else "differs from"
    owner = ""
    if check and check.domain == sender:
        ev = _ids(ev, check.evidence_ids)
        owner = f" The graph lists it under {check.company_name}, not the vendor." if check.company_name else " The graph lists no company at that domain."
    return _flag("R1", "hard", f"Sender domain {sender} {kind} the vendor's primary domain {primary}.{owner}", ev)


def _former_note(pc: PersonCheck) -> str:
    if not (pc.former_role or pc.former_until):
        return ""
    role = f" as {pc.former_role}" if pc.former_role else ""
    until = f" until {pc.former_until:%b %Y}" if pc.former_until else ""
    return f" A person of this name is listed as having worked there{role}{until}; profile data can lag."


def r2_person_found(ctx: RuleContext, policy: dict) -> Finding:
    ev = _person_ev(ctx)
    pc = ctx.person_check
    if pc is None or pc.status == "UNVERIFIABLE":
        return _unverifiable("R2", f"Sender check did not complete: {pc.reason if pc else 'not run'}.", ev)
    if pc.status == "FOUND":
        return _pass("R2", "Sender was found as a current employee of the vendor.", ev)
    if pc.status == "AMBIGUOUS":
        return _unverifiable("R2", "Several plausible people match the sender's name; no one was picked.", ev)
    email_ok = pc.email_lookup_ran or (pc.email_lookup_unsupported and not policy["r2"]["require_email_lookup"])
    if not (email_ok and pc.name_search_ran):
        return _unverifiable("R2", "Sender not found, but a lookup failed or did not run, so absence proves nothing.", ev)
    adequate, why = coverage_adequate(ctx, policy)
    if not adequate:
        return _unverifiable("R2", f"Sender not found, but coverage is too thin to conclude anything: {why}.{_former_note(pc)}", ev)
    former = _former_note(pc)
    how = "by email or name" if pc.email_lookup_ran else "by full name or surname (email lookup is not available on this API key)"
    return _flag("R2", "hard", f"Sender not found at this company {how}; {why}.{former}", _ids(ev, _company_ev(ctx, "headcount")))


def r3_recent_start(ctx: RuleContext, policy: dict) -> Finding:
    ev = _person_ev(ctx)
    p = ctx.person_check.person if ctx.person_check else None
    if p is None:
        return _unverifiable("R3", "No verified sender profile, so tenure is unknown.", ev)
    if p.recently_changed_jobs:
        return _flag("R3", "medium", "Sender recently changed jobs; a legitimate new hire can also trigger this.", ev)
    if p.start_date is None:
        return _unverifiable("R3", "Sender's start date is not in the graph.", ev)
    days = (ctx.as_of - p.start_date).days
    if days < policy["r3"]["recent_days"]:
        return _flag("R3", "medium", f"Sender started {days} days ago; a legitimate new hire can also trigger this.", ev)
    return _pass("R3", f"Sender has been in the role for {days} days.", ev)


def r4_function(ctx: RuleContext, policy: dict) -> Finding:
    ev = _person_ev(ctx)
    p = ctx.person_check.person if ctx.person_check else None
    if p is None:
        return _unverifiable("R4", "No verified sender profile, so function is unknown.", ev)
    # function_category is often empty in real data, so title and seniority count too.
    label = " ".join(x for x in (p.function, p.seniority, p.current_title) if x).lower()
    if not label:
        return _unverifiable("R4", "Sender's function is not in the graph.", ev)
    if any(a in label for a in policy["r4"]["allowed_functions"]):
        return _pass("R4", f"Sender's function ({label}) fits a payment request.", ev)
    return _flag("R4", "medium", f"Sender's function ({label}) is not finance, operations or leadership.", ev)


def r5_free_mail(ctx: RuleContext, policy: dict) -> Finding:
    ev = _claim_ev(ctx, "sender_email")
    sender = sender_domain(ctx.claim.sender_email)
    if sender is None:
        return _unverifiable("R5", "No sender email address in the packet.", ev)
    if sender in policy["r5"]["free_mail_domains"]:
        return _flag("R5", "medium", f"Company request sent from a free-mail domain ({sender}).", ev)
    return _pass("R5", f"Sender domain {sender} is not a free-mail provider.", ev)


def _size(revenue: Optional[float], headcount: Optional[int]) -> str:
    rev = f"revenue up to ${revenue:,.0f}" if revenue is not None else "no revenue estimate"
    return f"{rev}, headcount {headcount if headcount is not None else 'unknown'}"


def r6_amount_vs_size(ctx: RuleContext, policy: dict) -> Finding:
    ev = _ids(_claim_ev(ctx, "amount", "currency"), _company_ev(ctx, "revenue_upper_usd", "headcount"))
    amount = amount_usd(ctx.claim, policy)
    if amount is None:
        return _unverifiable("R6", "Invoice amount or a known currency is missing.", ev)
    if is_ambiguous_or_unresolved(ctx):
        return _unverifiable("R6", f"Cannot size the invoice: {_company_gap(ctx)}.", ev)
    revenue = ctx.company.revenue_upper_usd if ctx.company else None
    headcount = ctx.company.headcount if ctx.company else None
    if revenue is None and not headcount:
        return _unverifiable("R6", "Neither revenue nor headcount is available for the vendor.", ev)
    t = policy["r6"]
    if (revenue is not None and amount > t["hard_multiple"] * revenue) or (headcount and amount > t["hard_per_head"] * headcount):
        return _flag("R6", "hard", f"Invoice of ${amount:,.0f} is far above the vendor's size ({_size(revenue, headcount)}).", ev)
    if (revenue is not None and amount > t["soft_multiple"] * revenue) or (headcount and amount > t["soft_per_head"] * headcount):
        return _flag("R6", "medium", f"Invoice of ${amount:,.0f} is high for the vendor's size ({_size(revenue, headcount)}).", ev)
    return _pass("R6", f"Invoice of ${amount:,.0f} is plausible for the vendor's size.", ev)


def r7a_tiny_headcount(ctx: RuleContext, policy: dict) -> Finding:
    ev = _ids(_claim_ev(ctx, "amount"), _company_ev(ctx, "headcount"))
    amount = amount_usd(ctx.claim, policy)
    headcount = ctx.company.headcount if ctx.company else None
    if amount is None or headcount is None:
        return _unverifiable("R7a", "Amount or vendor headcount is missing.", ev)
    t = policy["r7"]
    if headcount <= t["tiny_headcount"] and amount >= t["min_amount_usd"]:
        return _flag("R7a", "medium", f"Vendor has {headcount} employee(s) against a ${amount:,.0f} invoice.", ev)
    return _pass("R7a", f"Headcount {headcount} is consistent with a ${amount:,.0f} invoice.", ev)


def r7b_web_traffic(ctx: RuleContext, policy: dict) -> Finding:
    ev = _ids(_claim_ev(ctx, "amount"), _company_ev(ctx, "monthly_visitors"))
    amount = amount_usd(ctx.claim, policy)
    visitors = ctx.company.monthly_visitors if ctx.company else None
    if amount is None or visitors is None:
        return _unverifiable("R7b", "Amount or vendor web traffic is missing.", ev)
    t = policy["r7"]
    if visitors <= t["near_zero_monthly_visitors"] and amount >= t["min_amount_usd"]:
        return _flag("R7b", "medium", f"Vendor has about {visitors:,.0f} monthly visitors against a ${amount:,.0f} invoice.", ev)
    return _pass("R7b", f"Web traffic of about {visitors:,.0f} monthly visitors is consistent with the invoice.", ev)


def r10_pressure(ctx: RuleContext, policy: dict) -> Finding:
    ev = _claim_ev(ctx, "urgency_phrases")
    phrases = [p.lower() for p in ctx.claim.urgency_phrases]
    hits = [k for k in policy["r10"]["phrases"] if any(k in p for p in phrases)]
    if hits:
        return _flag("R10", "low", f"Pressure language in the packet (text signal only): {', '.join(hits)}.", ev)
    return _pass("R10", "No pressure language was extracted from the packet.", ev)


def r12_insufficient(findings: list[Finding]) -> Optional[Finding]:
    gaps = [f for f in findings if f.status == "UNVERIFIABLE"]
    if not gaps:
        return None
    ev = _ids(*[f.evidence_ids for f in gaps])
    return _unverifiable("R12", "Insufficient data for: " + ", ".join(f.rule_id for f in gaps) + ".", ev)


RULES = [
    r1_domain, r2_person_found, r3_recent_start, r4_function, r5_free_mail,
    r6_amount_vs_size, r7a_tiny_headcount, r7b_web_traffic, r10_pressure,
]


def run_rules(ctx: RuleContext, policy: dict) -> list[Finding]:
    findings = [rule(ctx, policy) for rule in RULES]
    summary = r12_insufficient(findings)
    return findings + ([summary] if summary else [])
