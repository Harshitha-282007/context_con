"""Rule, decision and ledger tests on hand-made fixtures. No network."""
from datetime import date

import pytest

from clearance.config import load_policy
from clearance.decide import decide
from clearance.ledger import Ledger, verify_chain
from clearance.rules import RuleContext, coverage_adequate, r1_domain, r2_person_found, r6_amount_vs_size, run_rules
from clearance.schemas import Claim, CompanyCandidate, CompanyProfile, Finding, PersonCheck, PersonProfile, Resolution, SenderDomainCheck

POLICY = load_policy()
AS_OF = date(2026, 10, 31)


def resolved(conf=1.0):
    return Resolution(
        status="RESOLVED",
        company=CompanyCandidate(crustdata_company_id=1, name="Acme", primary_domain="acme.com", confidence=conf),
        evidence_ids=["E1"],
    )


def company(**kw):
    base = dict(
        crustdata_company_id=1, name="Acme", primary_domain="acme.com", all_domains=["acme.com"],
        headcount=300, monthly_visitors=50000, revenue_upper_usd=50_000_000,
        evidence={"primary_domain": "E2", "headcount": "E3"},
    )
    return CompanyProfile(**{**base, **kw})


def found(**kw):
    person = PersonProfile(
        name="Ana", function="Finance", start_date=date(2022, 1, 1),
        recently_changed_jobs=False, **kw,
    )
    return PersonCheck(status="FOUND", email_lookup_ran=True, name_search_ran=True, person=person, employees_found=200, evidence_ids=["E4"])


def not_found(employees=200, email=True, name=True):
    return PersonCheck(status="NOT_FOUND", email_lookup_ran=email, name_search_ran=name, employees_found=employees, evidence_ids=["E4"])


def claim(**kw):
    base = dict(vendor_name="Acme", sender_email="ana@acme.com", amount=20000, currency="USD")
    return Claim(**{**base, **kw})


def ctx(**kw):
    base = dict(claim=claim(), as_of=AS_OF, resolution=resolved(), company=company(), person_check=found())
    return RuleContext(**{**base, **kw})


def outcome(c):
    return decide(run_rules(c, POLICY), c.resolution, POLICY).outcome


@pytest.mark.parametrize("email,status", [
    ("ana@acme.com", "PASS"),
    ("ana@eu.acme.com", "PASS"),
    ("ana@acrne.com", "FLAG"),
    ("ana@acme-billing.com", "FLAG"),
    ("ana@аcme.com", "FLAG"),  # Cyrillic a
    ("ana@gmail.com", "UNVERIFIABLE"),
])
def test_r1_domains(email, status):
    assert r1_domain(ctx(claim=claim(sender_email=email)), POLICY).status == status


def test_r1_lookalike_is_named_as_such():
    f = r1_domain(ctx(claim=claim(sender_email="ana@acrne.com")), POLICY)
    assert f.severity == "hard" and "lookalike" in f.message


def test_r2_hard_only_when_gate_passes():
    assert r2_person_found(ctx(person_check=not_found()), POLICY).status == "FLAG"


@pytest.mark.parametrize("kwargs", [
    dict(person_check=not_found(employees=1)),                 # thin graph
    dict(person_check=not_found(employees=None)),              # count unknown
    dict(company=company(headcount=None), person_check=not_found()),
    dict(resolution=resolved(conf=0.4), person_check=not_found()),
    dict(resolution=Resolution(status="AMBIGUOUS"), person_check=not_found()),
    dict(person_check=not_found(email=False)),                 # a lookup failed
    dict(person_check=not_found(name=False)),
])
def test_r2_cannot_be_hard_when_gate_fails(kwargs):
    f = r2_person_found(ctx(**kwargs), POLICY)
    assert f.status == "UNVERIFIABLE" and f.severity == "none"


def test_r2_denied_email_lookup_uses_name_searches():
    pc = not_found(email=False)
    pc.email_lookup_unsupported = True
    f = r2_person_found(ctx(person_check=pc), POLICY)
    assert f.status == "FLAG" and "not available on this API key" in f.message


def test_r2_denied_email_lookup_can_be_made_strict():
    pc = not_found(email=False)
    pc.email_lookup_unsupported = True
    strict = {**POLICY, "r2": {"require_email_lookup": True}}
    assert r2_person_found(ctx(person_check=pc), strict).status == "UNVERIFIABLE"


def test_r2_ambiguous_person_is_unverifiable():
    pc = PersonCheck(status="AMBIGUOUS", email_lookup_ran=True, name_search_ran=True, employees_found=200)
    assert r2_person_found(ctx(person_check=pc), POLICY).status == "UNVERIFIABLE"


def test_coverage_threshold_scales_with_small_companies():
    c = ctx(company=company(headcount=4), person_check=not_found(employees=2))
    assert coverage_adequate(c, POLICY)[0] is True


def test_missing_data_is_never_pass():
    c = RuleContext(claim=Claim(), as_of=AS_OF)
    findings = run_rules(c, POLICY)
    graph_rules = [f for f in findings if f.rule_id != "R10"]
    assert all(f.status == "UNVERIFIABLE" for f in graph_rules)


def test_clean_vendor_approves():
    assert outcome(ctx()) == "APPROVE"


def test_ambiguous_vendor_never_approves():
    c = ctx(resolution=Resolution(status="AMBIGUOUS"), company=None, person_check=None)
    assert outcome(c) == "HOLD"


def test_unverifiable_key_rule_blocks_approve():
    assert outcome(ctx(person_check=PersonCheck(status="UNVERIFIABLE", reason="403"))) == "HOLD"


def test_bec_lookalike_and_not_found_holds():
    c = ctx(claim=claim(sender_email="ana@acrne.com"), person_check=not_found())
    assert outcome(c) == "HOLD"


def test_bec_with_two_mediums_escalates():
    c = ctx(claim=claim(sender_email="ana@acrne.com", amount=6_000_000), person_check=not_found(),
            company=company(monthly_visitors=10, headcount=300, revenue_upper_usd=50_000_000))
    findings = run_rules(c, POLICY)
    findings.append(Finding(rule_id="X", status="FLAG", severity="medium", message="m", evidence_ids=["E1"]))
    findings.append(Finding(rule_id="Y", status="FLAG", severity="medium", message="m", evidence_ids=["E1"]))
    assert decide(findings, c.resolution, POLICY).outcome == "ESCALATE"


def test_ghost_vendor_escalates():
    c = ctx(claim=claim(amount=900_000), company=company(headcount=2, monthly_visitors=5, revenue_upper_usd=100_000))
    assert r6_amount_vs_size(c, POLICY).severity == "hard"
    assert outcome(c) == "ESCALATE"


def test_medium_flag_alone_holds():
    c = ctx(claim=claim(sender_email="ana@gmail.com"))
    assert outcome(c) == "HOLD"


def test_unknown_currency_is_unverifiable():
    assert r6_amount_vs_size(ctx(claim=claim(currency="XYZ")), POLICY).status == "UNVERIFIABLE"


def test_decisions_are_deterministic():
    c = ctx(claim=claim(sender_email="ana@acrne.com"), person_check=not_found())
    first = decide(run_rules(c, POLICY), c.resolution, POLICY).model_dump()
    assert all(decide(run_rules(c, POLICY), c.resolution, POLICY).model_dump() == first for _ in range(5))


def test_ledger_chain_detects_tampering():
    led = Ledger()
    led.add({"a": 1}, endpoint="/x", retrieved_at="t")
    led.add({"b": 2}, endpoint="/y", retrieved_at="t")
    assert verify_chain(led.records)
    led.records[0].value = {"a": 999}
    assert not verify_chain(led.records)


def domain_check(**kw):
    return SenderDomainCheck(domain="acrne.com", evidence_ids=["E9"], **kw)


def test_r1_says_when_graph_has_no_company_at_sender_domain():
    f = r1_domain(ctx(claim=claim(sender_email="ana@acrne.com"), sender_domain_check=domain_check()), POLICY)
    assert f.severity == "hard" and "no company at that domain" in f.message and "E9" in f.evidence_ids


def test_r1_names_a_different_owner_of_sender_domain():
    f = r1_domain(ctx(claim=claim(sender_email="ana@acrne.com"), sender_domain_check=domain_check(company_id=2, company_name="Other Co")), POLICY)
    assert f.severity == "hard" and "Other Co" in f.message


def test_r1_passes_when_graph_maps_sender_domain_to_the_vendor():
    f = r1_domain(ctx(claim=claim(sender_email="ana@acrne.com"), sender_domain_check=domain_check(company_id=1, company_name="Acme")), POLICY)
    assert f.status == "PASS"


def test_r2_mentions_a_former_role_without_changing_the_outcome():
    pc = not_found()
    pc.former_role, pc.former_until = "Accountant", date(2024, 3, 1)
    f = r2_person_found(ctx(person_check=pc), POLICY)
    assert f.status == "FLAG" and f.severity == "hard" and "Accountant" in f.message and "can lag" in f.message


def test_former_role_does_not_bypass_the_coverage_gate():
    pc = not_found(employees=1)
    pc.former_role = "Accountant"
    f = r2_person_found(ctx(person_check=pc), POLICY)
    assert f.status == "UNVERIFIABLE" and "Accountant" in f.message
