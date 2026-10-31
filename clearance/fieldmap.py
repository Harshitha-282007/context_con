"""Vendor JSON stops here. Paths below come from real responses saved in fixtures/."""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from .schemas import CompanyCandidate, CompanyProfile, PersonProfile

# Normalized field -> response path, recorded in the ledger as the citation.
COMPANY_PATHS = {
    "name": "basic_info.name",
    "primary_domain": "basic_info.primary_domain",
    "all_domains": "basic_info.all_domains",
    "headcount": "headcount.total",
    "headcount_6mo_growth_pct": "headcount.growth_percent.six_months",
    "monthly_visitors": "web_traffic.domain_traffic.<domain>.monthly_visitors",
    "revenue_lower_usd": "revenue.estimated.lower_bound_usd",
    "revenue_upper_usd": "revenue.estimated.upper_bound_usd",
    "headquarters": "locations.headquarters",
}
PERSON_PATHS = {
    "name": "basic_profile.name",
    "current_title": "experience.employment_details.current[].title",
    "function": "experience.employment_details.current[].function_category",
    "seniority": "experience.employment_details.current[].seniority_level",
    "start_date": "experience.employment_details.current[].start_date",
    "recently_changed_jobs": "experience.recently_changed_jobs",
    "has_business_email": "contact.has_business_email",
    "business_email_verified": "experience.employment_details.current[].business_email_verified",
}
PERSON_SEARCH_FIELDS = [
    "basic_profile.name", "basic_profile.current_title", "contact.has_business_email",
    "experience.employment_details.current", "experience.recently_changed_jobs",
    "social_handles.professional_network_identifier.profile_url",
]
COMPANY_ENRICH_FIELDS = ["basic_info", "headcount", "revenue", "web_traffic", "locations"]
FILTER_CURRENT_COMPANY = "experience.employment_details.current.company_id"
FILTER_PAST_COMPANY = "experience.employment_details.past.company_id"
FILTER_CURRENT_TITLE = "experience.employment_details.current.title"
FILTER_NAME = "basic_profile.name"


def dig(obj: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _text(value: Any) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _positive(value: Any) -> Optional[float]:
    """Revenue of 0 in the data means no estimate, not a company that earns nothing."""
    n = _number(value)
    return n if n else None


def _int(value: Any) -> Optional[int]:
    n = _number(value)
    return None if n is None else int(n)


def _date(value: Any) -> Optional[date]:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def match_rows(data: Any) -> list[dict]:
    """Identify/Enrich: top-level array of {matched_on, match_type, matches: [...]}."""
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return []
    matches = data[0].get("matches")
    return [m for m in matches if isinstance(m, dict)] if isinstance(matches, list) else []


def company_candidate(match: dict) -> CompanyCandidate:
    data = match.get("company_data") or {}
    info = data.get("basic_info") or {}
    return CompanyCandidate(
        crustdata_company_id=_int(data.get("crustdata_company_id") or info.get("crustdata_company_id")),
        name=_text(info.get("name")),
        primary_domain=_text(info.get("primary_domain")),
        employee_count_range=_text(info.get("employee_count_range")),
        confidence=_number(match.get("confidence_score")),
    )


def _monthly_visitors(traffic: Any, primary: Optional[str]) -> Optional[float]:
    domains = dig(traffic, "domain_traffic")
    if not isinstance(domains, dict):
        return None
    if primary and isinstance(domains.get(primary), dict):
        return _number(domains[primary].get("monthly_visitors"))
    values = [_number(v.get("monthly_visitors")) for v in domains.values() if isinstance(v, dict)]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def company_profile(data: Any) -> CompanyProfile:
    data = data if isinstance(data, dict) else {}
    primary = _text(dig(data, "basic_info", "primary_domain"))
    domains = dig(data, "basic_info", "all_domains")
    return CompanyProfile(
        crustdata_company_id=_int(data.get("crustdata_company_id")),
        name=_text(dig(data, "basic_info", "name")),
        primary_domain=primary,
        all_domains=[d for d in domains if isinstance(d, str)] if isinstance(domains, list) else [],
        headcount=_int(dig(data, "headcount", "total")),
        headcount_6mo_growth_pct=_number(dig(data, "headcount", "growth_percent", "six_months")),
        monthly_visitors=_monthly_visitors(data.get("web_traffic"), primary),
        revenue_lower_usd=_positive(dig(data, "revenue", "estimated", "lower_bound_usd")),
        revenue_upper_usd=_positive(dig(data, "revenue", "estimated", "upper_bound_usd")),
        headquarters=_text(dig(data, "locations", "headquarters")),
    )


def _current_role(profile: dict, company_id: Optional[int]) -> dict:
    roles = dig(profile, "experience", "employment_details", "current")
    roles = [r for r in roles if isinstance(r, dict)] if isinstance(roles, list) else []
    at_company = [r for r in roles if company_id is not None and r.get("crustdata_company_id") == company_id]
    pool = at_company or roles
    return next((r for r in pool if r.get("is_default")), pool[0] if pool else {})


def person_profile(profile: Any, company_id: Optional[int] = None) -> PersonProfile:
    profile = profile if isinstance(profile, dict) else {}
    role = _current_role(profile, company_id)
    changed = dig(profile, "experience", "recently_changed_jobs")
    has_email = dig(profile, "contact", "has_business_email")
    verified = role.get("business_email_verified")
    return PersonProfile(
        crustdata_person_id=_int(profile.get("crustdata_person_id")),
        name=_text(dig(profile, "basic_profile", "name")),
        current_title=_text(role.get("title")) or _text(dig(profile, "basic_profile", "current_title")),
        function=_text(role.get("function_category")),  # Often an empty string in real data.
        seniority=_text(role.get("seniority_level")),
        start_date=_date(role.get("start_date")),
        recently_changed_jobs=changed if isinstance(changed, bool) else None,
        current_company_id=_int(role.get("crustdata_company_id")),
        has_business_email=has_email if isinstance(has_email, bool) else None,
        business_email_verified=verified if isinstance(verified, bool) else None,
        profile_url=_text(dig(profile, "social_handles", "professional_network_identifier", "profile_url")),
    )


def past_role(profile: Any, company_id: int) -> tuple[Optional[str], Optional[date]]:
    """The most recent past role at this company, as (title, end date)."""
    roles = dig(profile, "experience", "employment_details", "past")
    roles = [r for r in roles if isinstance(r, dict) and r.get("crustdata_company_id") == company_id] if isinstance(roles, list) else []
    if not roles:
        return None, None
    latest = max(roles, key=lambda r: _date(r.get("end_date")) or date.min)
    return _text(latest.get("title")), _date(latest.get("end_date"))


def search_profiles(data: Any) -> list[dict]:
    profiles = data.get("profiles") if isinstance(data, dict) else None
    return [p for p in profiles if isinstance(p, dict)] if isinstance(profiles, list) else []


def search_total(data: Any) -> Optional[int]:
    return _int(data.get("total_count")) if isinstance(data, dict) else None
