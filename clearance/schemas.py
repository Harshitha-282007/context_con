"""Data contracts. Rules only ever see these normalized objects, never vendor JSON."""
from __future__ import annotations

from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["PASS", "FLAG", "UNVERIFIABLE"]
Severity = Literal["hard", "medium", "low", "none"]
Outcome = Literal["APPROVE", "HOLD", "ESCALATE"]


class Claim(BaseModel):
    vendor_name: Optional[str] = None
    claimed_domain: Optional[str] = None
    claimed_address: Optional[str] = None
    sender_name: Optional[str] = None
    sender_email: Optional[str] = None
    signature_name: Optional[str] = None
    signature_title: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    services: Optional[str] = None
    reference: Optional[str] = None
    bank_change: bool = False
    urgency_phrases: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    id: str
    source: Literal["crustdata", "packet", "system"] = "crustdata"
    endpoint: Optional[str] = None
    entity_id: Optional[str] = None
    field_path: Optional[str] = None
    value: Any = None
    retrieved_at: str
    credits: float = 0.0
    prev_hash: str
    hash: str


class Finding(BaseModel):
    rule_id: str
    status: Status
    severity: Severity = "none"
    message: str
    evidence_ids: list[str] = Field(default_factory=list)


class CompanyCandidate(BaseModel):
    crustdata_company_id: Optional[int] = None
    name: Optional[str] = None
    primary_domain: Optional[str] = None
    employee_count_range: Optional[str] = None
    confidence: Optional[float] = None
    evidence_ids: list[str] = Field(default_factory=list)


class Resolution(BaseModel):
    status: Literal["RESOLVED", "AMBIGUOUS", "NOT_FOUND", "UNVERIFIABLE"]
    reason: str = ""
    company: Optional[CompanyCandidate] = None
    candidates: list[CompanyCandidate] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class CompanyProfile(BaseModel):
    crustdata_company_id: Optional[int] = None
    name: Optional[str] = None
    primary_domain: Optional[str] = None
    all_domains: list[str] = Field(default_factory=list)
    headcount: Optional[int] = None
    headcount_6mo_growth_pct: Optional[float] = None
    monthly_visitors: Optional[float] = None
    revenue_lower_usd: Optional[float] = None
    revenue_upper_usd: Optional[float] = None
    headquarters: Optional[str] = None
    # Normalized field name -> ledger evidence id
    evidence: dict[str, str] = Field(default_factory=dict)


class PersonProfile(BaseModel):
    crustdata_person_id: Optional[int] = None
    name: Optional[str] = None
    current_title: Optional[str] = None
    function: Optional[str] = None
    seniority: Optional[str] = None
    start_date: Optional[date] = None
    recently_changed_jobs: Optional[bool] = None
    current_company_id: Optional[int] = None
    has_business_email: Optional[bool] = None
    business_email_verified: Optional[bool] = None
    profile_url: Optional[str] = None
    evidence: dict[str, str] = Field(default_factory=dict)


class PersonCheck(BaseModel):
    status: Literal["FOUND", "NOT_FOUND", "AMBIGUOUS", "UNVERIFIABLE"]
    reason: str = ""
    email_lookup_ran: bool = False
    name_search_ran: bool = False
    person: Optional[PersonProfile] = None
    candidates: list[PersonProfile] = Field(default_factory=list)
    employees_found: Optional[int] = None  # graph count of current employees at the vendor
    evidence_ids: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    outcome: Outcome
    reasons: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    unverifiable: list[str] = Field(default_factory=list)
    what_would_change_this: str = ""


class Recipient(BaseModel):
    name: str
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)


class Draft(BaseModel):
    id: str
    type: str = "vendor_verification"
    recipients: list[Recipient] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    checks: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["DRAFT_ONLY", "BLOCKED"] = "DRAFT_ONLY"


class CaseResult(BaseModel):
    claim: Claim
    resolution: Optional[Resolution] = None
    company: Optional[CompanyProfile] = None
    person_check: Optional[PersonCheck] = None
    findings: list[Finding] = Field(default_factory=list)
    decision: Optional[Decision] = None
    contacts: list[PersonProfile] = Field(default_factory=list)
    ledger: list[Evidence] = Field(default_factory=list)
    drafts: list[Draft] = Field(default_factory=list)
    credits_used: float = 0.0
