"""End-to-end replay on real saved responses, plus failure paths. No network."""
from datetime import date
from pathlib import Path

import pytest

from clearance.config import FIXTURES_DIR, ROOT, load_policy
from clearance.crustdata import CrustdataClient
from clearance.extract import extract_regex, validate_against_packet
from clearance.ledger import verify_chain
from clearance.pipeline import run_case
from clearance.report import mask, person_names, to_markdown
from clearance.schemas import Claim

POLICY = load_policy()
AS_OF = date(2026, 10, 31)


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def packet(case):
    return (ROOT / "packets" / f"case_{case}.txt").read_text()


def replay(text, tmp_path):
    client = CrustdataClient(None, FIXTURES_DIR, offline=True)
    return run_case(text, client, POLICY, AS_OF, outbox=tmp_path)


@pytest.mark.parametrize("case,outcome", [("a", "APPROVE"), ("b", "HOLD"), ("c", "ESCALATE"), ("d", "HOLD"), ("e", "HOLD")])
def test_demo_cases_offline(case, outcome, tmp_path):
    r = replay(packet(case), tmp_path)
    assert r.decision.outcome == outcome
    assert r.chain_ok and verify_chain(r.ledger)
    ids = {e.id for e in r.ledger}
    assert all(f.evidence_ids and set(f.evidence_ids) <= ids for f in r.findings)
    assert all(s.evidence_ids and set(s.evidence_ids) <= ids for s in r.statements)
    assert not r.dropped_statements


def test_injection_changes_nothing(tmp_path):
    b, e = replay(packet("b"), tmp_path), replay(packet("e"), tmp_path)
    assert b.decision.outcome == e.decision.outcome
    assert [(f.rule_id, f.status) for f in b.findings] == [(f.rule_id, f.status) for f in e.findings]


def test_hero_case_has_clean_callback_and_draft(tmp_path):
    r = replay(packet("b"), tmp_path)
    assert {f.rule_id for f in r.findings if f.status == "FLAG" and f.severity == "hard"} == {"R1", "R2"}
    assert r.contacts and all("Testerson" not in (c.name or "") for c in r.contacts)
    draft = r.drafts[0]
    assert draft.status == "DRAFT_ONLY" and all(c["passed"] for c in draft.checks)
    assert "serverobotlcs" not in draft.body and "123456789" not in draft.body
    assert (tmp_path / f"{draft.id}.eml").exists()


def test_ambiguous_vendor_lists_candidates_and_requests_documents(tmp_path):
    r = replay(packet("d"), tmp_path)
    assert r.resolution.status == "AMBIGUOUS" and r.company is None
    assert len(r.resolution.candidates) > 1 and r.document_requests and not r.drafts


@pytest.mark.parametrize("text", ["", "   ", "hi"])
def test_insufficient_packet(text, tmp_path):
    r = replay(text, tmp_path)
    assert r.decision.outcome == "HOLD" and r.chain_ok


@pytest.mark.parametrize("error_client", [
    lambda d: CrustdataClient(None, d),                       # missing key
    lambda d: CrustdataClient(None, d, offline=True),         # no fixtures
    lambda d: CrustdataClient("bad", d, credit_cap=0),        # credit cap hit before the first call
])
def test_api_failure_degrades_to_hold_without_crash(error_client, tmp_path):
    r = run_case(packet("b"), error_client(tmp_path / "fx"), POLICY, AS_OF, outbox=tmp_path)
    assert r.decision.outcome == "HOLD" and r.resolution.status == "UNVERIFIABLE"
    assert not any(f.status == "PASS" and f.rule_id in {"R1", "R2", "R6"} for f in r.findings)


def test_forwarded_chain_uses_innermost_sender():
    text = "From: Clerk <clerk@ourco.example>\nFwd:\n> From: Real Sender <real@vendor.example>\n> Vendor: Acme\n> Total: $5,000.00\n"
    c = extract_regex(text)
    assert c.sender_email == "real@vendor.example" and c.sender_name == "Real Sender" and c.amount == 5000


def test_llm_values_not_in_packet_are_dropped():
    invented = Claim(vendor_name="Invented Co", sender_email="a@b.example", amount=999, currency="USD")
    assert validate_against_packet(invented, "Vendor: Other Co").model_dump(exclude_defaults=True) == {}


def test_regex_keeps_only_last_four_of_account():
    c = extract_regex("New account number: 000123456789\n")
    assert c.bank_last4 == "6789" and "000123456789" not in c.model_dump_json()


def test_mask_hides_names_and_addresses(tmp_path):
    r = replay(packet("a"), tmp_path)
    md = mask(to_markdown(r), person_names(r))
    assert all(n not in md for n in person_names(r))


def test_zero_revenue_means_no_estimate():
    from clearance.fieldmap import company_profile
    p = company_profile({"revenue": {"estimated": {"lower_bound_usd": 0, "upper_bound_usd": 0}}})
    assert p.revenue_upper_usd is None


def test_legal_suffix_is_stripped_for_name_lookup():
    from clearance.resolve import strip_legal_suffix
    assert strip_legal_suffix("Chargebee, Inc.") == "Chargebee"
    assert strip_legal_suffix("Acme Pvt. Ltd.") == "Acme"
    assert strip_legal_suffix("Company") == "Company"


@pytest.mark.parametrize("name,outcome", [
    ("test_1_clean_renewal", "APPROVE"),
    ("test_2_real_name_spoofed_domain", "HOLD"),
    ("test_3_forwarded_gmail_inr", "HOLD"),
    ("test_4_oversized_invoice", "ESCALATE"),
    ("test_5_vendor_not_in_graph", "HOLD"),
    ("test_6_injection_unknown_currency", "HOLD"),
    ("test_7_almost_empty", "HOLD"),
])
def test_extra_packets_offline(name, outcome, tmp_path):
    r = replay((ROOT / "packets" / f"{name}.txt").read_text(), tmp_path)
    assert r.decision.outcome == outcome and r.chain_ok and not r.dropped_statements


def test_spoofed_real_name_is_caught_by_domain(tmp_path):
    r = replay((ROOT / "packets" / "test_2_real_name_spoofed_domain.txt").read_text(), tmp_path)
    by_rule = {f.rule_id: f for f in r.findings}
    assert by_rule["R2"].status == "PASS" and by_rule["R1"].severity == "hard"


def test_weak_name_overlaps_are_not_candidates(tmp_path):
    r = replay((ROOT / "packets" / "test_5_vendor_not_in_graph.txt").read_text(), tmp_path)
    assert r.resolution.status == "NOT_FOUND" and not r.resolution.candidates and r.document_requests


def test_offline_miss_is_reported_as_missing_data_not_a_finding(tmp_path):
    text = packet("a").replace(extract_regex(packet("a")).sender_name, "Zed Unseenname")
    r = replay(text, tmp_path)
    assert r.decision.outcome == "HOLD" and r.offline_misses > 0
    assert r.person_check.status == "UNVERIFIABLE"
    md = to_markdown(r)
    assert "Incomplete offline run" in md and "turn off offline replay" in md.lower()
    assert "offline_no_fixture" not in md
    assert r.contact_search_error and "No independent contact was found" not in md


def test_complete_replay_has_no_offline_warning(tmp_path):
    r = replay(packet("b"), tmp_path)
    assert r.offline_misses == 0 and r.contact_search_error is None
    assert "Incomplete offline run" not in to_markdown(r)
