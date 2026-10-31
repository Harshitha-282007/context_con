"""Sender verification and callback contacts. Contacts come only from the graph."""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

from .crustdata import ApiResult, CrustdataClient, describe_error
from .fieldmap import (
    FILTER_CURRENT_COMPANY, FILTER_CURRENT_TITLE, FILTER_NAME, FILTER_PAST_COMPANY, PERSON_PATHS, PERSON_SEARCH_FIELDS,
    match_rows, past_role, person_profile, search_profiles, search_total,
)
from .ledger import Ledger
from .rules import edit_distance
from .schemas import Claim, PersonCheck, PersonProfile

FINANCE_TITLES = ["accounts payable", "payable", "controller", "finance", "accounting", "treasury", "CFO"]
LEADER_TITLES = ["CEO", "founder", "owner", "president", "managing director"]
SMALL_COMPANY = 10


def name_tokens(name: Optional[str]) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", name or "")
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    ascii_only = re.sub(r"\b(mr|mrs|ms|dr|prof)\b\.?", " ", ascii_only)
    return re.findall(r"[a-z]+", ascii_only)


def names_match(claimed: Optional[str], actual: Optional[str]) -> bool:
    """Tolerates initials and small spelling variants, for example Rahul/Rahool."""
    a, b = name_tokens(claimed), name_tokens(actual)
    if not a or not b or edit_distance(a[-1], b[-1]) > 1:
        return False
    if len(a) == 1 or len(b) == 1:
        return True
    first_a, first_b = a[0], b[0]
    return first_a[0] == first_b[0] and (len(first_a) == 1 or len(first_b) == 1 or edit_distance(first_a, first_b) <= 2)


def _search(client: CrustdataClient, company_id: int, condition: dict, limit: int) -> ApiResult:
    return client.post("/person/search", {
        "filters": {"op": "and", "conditions": [
            {"field": FILTER_CURRENT_COMPANY, "type": "=", "value": company_id}, condition,
        ]},
        "limit": limit,
        "fields": PERSON_SEARCH_FIELDS,
    })


def _record_search(ledger: Ledger, result: ApiResult, company_id: int, label: str) -> str:
    value = {"lookup": label, "status": result.status, "error": result.error, "total_count": search_total(result.data)}
    return ledger.add(value, endpoint=result.endpoint or "/person/search", entity_id=company_id, field_path="total_count",
                      credits=result.credits, retrieved_at=result.retrieved_at or None)


def record_person(ledger: Ledger, person: PersonProfile, endpoint: str = "/person/search") -> PersonProfile:
    for field, path in PERSON_PATHS.items():
        value = getattr(person, field)
        if value is not None:
            person.evidence[field] = ledger.add(value, endpoint=endpoint, entity_id=person.crustdata_person_id, field_path=path)
    return person


def count_employees(client: CrustdataClient, ledger: Ledger, company_id: int) -> tuple[Optional[int], Optional[str]]:
    result = client.post("/person/search", {
        "filters": {"field": FILTER_CURRENT_COMPANY, "type": "=", "value": company_id},
        "limit": 1, "fields": ["basic_profile.name"],
    })
    ev = _record_search(ledger, result, company_id, "current employees of vendor")
    return (search_total(result.data) if result.ok else None), ev


def _email_lookup(client: CrustdataClient, ledger: Ledger, email: str) -> tuple[bool, bool, Optional[str], str]:
    """Returns (ran, unsupported, matched_name, evidence_id)."""
    result = client.post("/person/identify", {"emails": [email]})
    ev = ledger.add(
        {"lookup": "email reverse lookup", "status": result.status, "error": result.error, "matches": len(match_rows(result.data))},
        endpoint="/person/identify", field_path="[].matches", credits=result.credits, retrieved_at=result.retrieved_at or None,
    )
    if result.status == 403:
        return False, True, None, ev
    if not result.ok:
        return False, False, None, ev
    rows = match_rows(result.data)
    name = None
    if rows and (rows[0].get("confidence_score") or 0) >= 0.9:
        basic = (rows[0].get("person_data") or {}).get("basic_profile") or {}
        name = basic.get("name")
    return True, False, name, ev


def _former_role(client: CrustdataClient, ledger: Ledger, name: str, surname: str, company_id: int):
    """Did this person work here before? Returns (title, end_date, evidence_id).

    A failed or ambiguous search claims nothing: the sender simply stays not found.
    """
    result = client.post("/person/search", {
        "filters": {"op": "and", "conditions": [
            {"field": FILTER_PAST_COMPANY, "type": "=", "value": company_id},
            {"field": FILTER_NAME, "type": "(.)", "value": surname},
        ]},
        "limit": 5,
        "fields": ["basic_profile.name", "experience.employment_details.past"],
    })
    ev = _record_search(ledger, result, company_id, "sender past-employer search")
    hits = [p for p in search_profiles(result.data) if names_match(name, person_profile(p).name)] if result.ok else []
    if len(hits) != 1:
        return None, None, ev
    title, until = past_role(hits[0], company_id)
    if title or until:
        ev = ledger.add({"title": title, "until": until}, endpoint="/person/search", entity_id=company_id,
                        field_path="experience.employment_details.past[]")
    return title, until, ev


def verify_sender(client: CrustdataClient, ledger: Ledger, claim: Claim, company_id: int) -> PersonCheck:
    employees, count_ev = count_employees(client, ledger, company_id)
    evidence = [count_ev] if count_ev else []
    email_ran, unsupported = False, False
    matched_name = None
    if claim.sender_email:
        email_ran, unsupported, matched_name, ev = _email_lookup(client, ledger, claim.sender_email)
        evidence.append(ev)

    base = dict(email_lookup_ran=email_ran, email_lookup_unsupported=unsupported, employees_found=employees)
    name = matched_name or claim.sender_name or claim.signature_name
    tokens = name_tokens(name)
    if not tokens:
        return PersonCheck(status="UNVERIFIABLE", reason="the packet gives no sender name to search for", evidence_ids=evidence, **base)

    full = _search(client, company_id, {"field": FILTER_NAME, "type": "(.)", "value": name}, 5)
    evidence.append(_record_search(ledger, full, company_id, "sender full-name search"))
    if not full.ok:
        return PersonCheck(status="UNVERIFIABLE", reason=f"name search failed: {describe_error(full.error)}", evidence_ids=evidence, **base)
    hits = [p for p in search_profiles(full.data) if names_match(name, person_profile(p).name)]

    if not hits:  # Surname only, which also catches nicknames and transliteration.
        surname = _search(client, company_id, {"field": FILTER_NAME, "type": "(.)", "value": tokens[-1]}, 10)
        evidence.append(_record_search(ledger, surname, company_id, "sender surname search"))
        if not surname.ok:
            return PersonCheck(status="UNVERIFIABLE", reason=f"surname search failed: {describe_error(surname.error)}", evidence_ids=evidence, **base)
        hits = [p for p in search_profiles(surname.data) if names_match(name, person_profile(p).name)]

    base["name_search_ran"] = True
    if not hits:
        title, until, ev = _former_role(client, ledger, name, tokens[-1], company_id) if len(tokens) > 1 else (None, None, None)
        if ev:
            evidence.append(ev)
        reason = "no current employee matches the sender's name"
        if title or until:
            reason += "; a past role at this company matches it"
        return PersonCheck(status="NOT_FOUND", reason=reason, former_role=title, former_until=until, evidence_ids=evidence, **base)
    people = [person_profile(p, company_id) for p in hits]
    if len(people) > 1 or len(tokens) == 1:
        return PersonCheck(status="AMBIGUOUS", reason=f"{len(people)} plausible people match; none picked", candidates=people[:5], evidence_ids=evidence, **base)
    person = record_person(ledger, people[0])
    evidence.extend(person.evidence.values())
    return PersonCheck(status="FOUND", person=person, evidence_ids=evidence, **base)


def _rank(person: PersonProfile) -> tuple:
    title = (person.current_title or "").lower()
    role = 0 if "payable" in title else 1 if "controller" in title else 2 if ("finance" in title or "account" in title) else 3
    start = person.start_date.toordinal() if person.start_date else 10**7
    return (role, not person.business_email_verified, not person.has_business_email, start, person.name or "")


def _title_query(titles: list[str]) -> dict:
    return {"op": "or", "conditions": [{"field": FILTER_CURRENT_TITLE, "type": "(.)", "value": t} for t in titles]}


def find_contacts(
    client: CrustdataClient, ledger: Ledger, claim: Claim, company_id: int,
    headcount: Optional[int], packet_text: str,
) -> tuple[list[PersonProfile], Optional[str]]:
    """Ranked callback contacts, excluding the sender and anyone named in the thread.

    The second value is why a search could not run. "Nobody found" and "could not
    look" are different answers, and the report must not confuse them.
    """
    small = headcount is not None and headcount < SMALL_COMPANY
    queries = ([LEADER_TITLES] if small else [FINANCE_TITLES, LEADER_TITLES])
    lowered = packet_text.lower()
    excluded = [n for n in (claim.sender_name, claim.signature_name) if n]
    failure = None
    for titles in queries:
        result = _search(client, company_id, _title_query(titles), 10)
        _record_search(ledger, result, company_id, "callback contact search")
        if not result.ok:
            failure = describe_error(result.error)
        people = [person_profile(p, company_id) for p in search_profiles(result.data)] if result.ok else []
        clean = [
            p for p in people
            if p.name and p.name.lower() not in lowered and not any(names_match(n, p.name) for n in excluded)
        ]
        if clean:
            ranked = sorted(clean, key=_rank)
            picks = ranked[:1]
            senior = [p for p in ranked[1:] if p.seniority in {"CXO", "Vice President", "Director", "Owner / Partner"}]
            if senior and not small:
                picks.append(senior[0])
            return [record_person(ledger, p) for p in picks], None
    return [], failure
