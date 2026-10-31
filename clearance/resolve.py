"""Entity resolution: domain first, then name. Close candidates mean AMBIGUOUS, never a guess."""
from __future__ import annotations

import re
from typing import Optional

from .crustdata import ApiResult, CrustdataClient, describe_error
from .fieldmap import company_candidate, match_rows
from .ledger import Ledger
from .rules import sender_domain
from .schemas import Claim, CompanyCandidate, Resolution, SenderDomainCheck

SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "pvt", "private", "gmbh", "plc", "llp", "pte", "sa", "bv"}


def normalize_company(name: Optional[str]) -> str:
    text = re.sub(r"\(.*?\)", " ", (name or "").lower())
    tokens = re.sub(r"[^a-z0-9 ]+", " ", text).split()
    return " ".join(t for t in tokens if t not in SUFFIXES)


def strip_legal_suffix(name: str) -> str:
    """'Chargebee, Inc.' -> 'Chargebee'. The name lookup matches stored names, which often lack it."""
    words = re.sub(r"\s+", " ", name.replace(",", " ")).strip().split(" ")
    while len(words) > 1 and words[-1].strip(".").lower() in SUFFIXES:
        words.pop()
    return " ".join(words)


def _record(ledger: Ledger, result: ApiResult, entity: str) -> str:
    cands = [company_candidate(m).model_dump(exclude={"evidence_ids"}) for m in match_rows(result.data)]
    value = {"status": result.status, "error": result.error, "candidates": cands[:5], "candidate_count": len(cands)}
    return ledger.add(value, endpoint=result.endpoint or "/company/identify", entity_id=entity,
                      field_path="[].matches[].company_data.basic_info", credits=result.credits, retrieved_at=result.retrieved_at or None)


def _names_agree(vendor: str, candidate: Optional[str]) -> bool:
    a, b = normalize_company(vendor), normalize_company(candidate)
    return bool(a and b) and (a == b or a in b or b in a)


def resolve_vendor(client: CrustdataClient, ledger: Ledger, claim: Claim, policy: dict) -> Resolution:
    if not claim.vendor_name and not claim.claimed_domain and not claim.sender_email:
        return Resolution(status="UNVERIFIABLE", reason="packet names no vendor or domain")

    evidence: list[str] = []
    failures: list[str] = []
    min_conf = policy["coverage"]["min_resolve_confidence"]
    sender = sender_domain(claim.sender_email)
    domains = [claim.claimed_domain]
    if sender and sender not in policy["r5"]["free_mail_domains"]:
        domains.append(sender)

    # A domain in the packet is attacker-controllable, so it only counts when its
    # company agrees with the vendor name the packet claims.
    for domain in dict.fromkeys(d for d in domains if d):
        result = client.post("/company/identify", {"domains": [domain], "exact_match": True})
        evidence.append(_record(ledger, result, domain))
        if not result.ok and result.status != 404:
            failures.append(f"domain lookup: {describe_error(result.error)}")
            continue
        cands = _with_evidence([company_candidate(m) for m in match_rows(result.data)], evidence[-1])
        agreeing = [c for c in cands if not claim.vendor_name or _names_agree(claim.vendor_name, c.name)]
        if len(agreeing) == 1 and (agreeing[0].confidence or 0) >= min_conf:
            return Resolution(status="RESOLVED", reason=f"matched by domain {domain}", company=agreeing[0], candidates=cands, evidence_ids=evidence)
        if len(agreeing) > 1:
            return Resolution(status="AMBIGUOUS", reason=f"{len(agreeing)} companies share domain {domain}", candidates=agreeing[:5], evidence_ids=evidence)

    if not claim.vendor_name:
        reason = "; ".join(failures) or "no company matched the packet's domains and no vendor name was given"
        return Resolution(status="UNVERIFIABLE" if failures else "NOT_FOUND", reason=reason, evidence_ids=evidence)

    query = strip_legal_suffix(claim.vendor_name)
    result = client.post("/company/identify", {"names": [query]})
    evidence.append(_record(ledger, result, query))
    if not result.ok and result.status != 404:
        return Resolution(status="UNVERIFIABLE", reason=f"name lookup: {describe_error(result.error)}", evidence_ids=evidence)
    cands = _with_evidence([company_candidate(m) for m in match_rows(result.data)], evidence[-1])
    if not cands:
        return Resolution(status="NOT_FOUND", reason=f"no company named {claim.vendor_name!r} in the graph", evidence_ids=evidence)
    target = normalize_company(claim.vendor_name)
    exact = [c for c in cands if normalize_company(c.name) == target]
    if len(exact) == 1 and (exact[0].confidence or 0) >= min_conf:
        return Resolution(status="RESOLVED", reason="unique exact name match", company=exact[0], candidates=cands[:5], evidence_ids=evidence)
    # Identify pads results with partial word overlaps, for example "Zyphorqa Dynamics"
    # returning General Dynamics at 0.6. Those are not candidates for this vendor.
    pool = exact or [c for c in cands if (c.confidence or 0) >= min_conf]
    if not pool:
        return Resolution(status="NOT_FOUND", reason=f"no close match for {claim.vendor_name!r} in the graph", candidates=[], evidence_ids=evidence)
    return Resolution(status="AMBIGUOUS", reason=f"{len(pool)} plausible match(es) for {claim.vendor_name!r}, none certain; none picked", candidates=pool[:5], evidence_ids=evidence)


def check_sender_domain(client: CrustdataClient, ledger: Ledger, claim: Claim, known_domains: list[str], policy: dict) -> SenderDomainCheck | None:
    """Independent evidence for R1: who does the graph say owns the sender's domain?

    Returns None when there is nothing to ask or the lookup failed, so a failure claims nothing.
    """
    domain = sender_domain(claim.sender_email)
    known = [d.lower() for d in known_domains]
    if not domain or domain in policy["r5"]["free_mail_domains"] or any(domain == d or domain.endswith("." + d) for d in known):
        return None
    result = client.post("/company/identify", {"domains": [domain], "exact_match": True})
    ev = _record(ledger, result, domain)
    if not result.ok and result.status != 404:
        return None
    cands = [company_candidate(m) for m in match_rows(result.data)]
    owner = cands[0] if len(cands) == 1 else None
    return SenderDomainCheck(domain=domain, company_id=owner.crustdata_company_id if owner else None,
                             company_name=owner.name if owner else None, evidence_ids=[ev])


def _with_evidence(cands: list[CompanyCandidate], evidence_id: str) -> list[CompanyCandidate]:
    for c in cands:
        c.evidence_ids = [evidence_id]
    return cands
