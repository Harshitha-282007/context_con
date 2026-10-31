"""Simulated finance handoff, replayed on saved responses. No network."""
from datetime import date

import pytest

from clearance.config import FIXTURES_DIR, ROOT, load_policy
from clearance.crustdata import CrustdataClient
from clearance.handoff import apply_action, defang, to_markdown
from clearance.pipeline import run_case
from clearance.report import to_markdown as report_markdown

POLICY = load_policy()


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def replay(name, tmp_path):
    text = (ROOT / "packets" / f"{name}.txt").read_text()
    client = CrustdataClient(None, FIXTURES_DIR, offline=True)
    return run_case(text, client, POLICY, date(2026, 10, 31), case_id=name, outbox=tmp_path)


def test_approve_creates_no_case(tmp_path):
    r = replay("case_a", tmp_path)
    assert r.finance_case is None and not list(tmp_path.glob("case-*"))


def test_hold_routes_to_ap_and_blocks_payment(tmp_path):
    c = replay("case_b", tmp_path).finance_case
    assert c.outcome == "HOLD" and c.route_to == "AP review queue" and c.approvals_needed == 1
    assert "BLOCKED" in c.payment_status and c.status == "SIMULATED_NOT_SENT"
    assert all(check["passed"] for check in c.checks)
    assert (tmp_path / f"{c.id}.md").exists() and (tmp_path / f"{c.id}.json").exists()


def test_escalate_routes_to_controller_and_needs_two_people(tmp_path):
    c = replay("case_c", tmp_path).finance_case
    assert c.outcome == "ESCALATE" and "controller" in c.route_to.lower() and c.approvals_needed == 2
    assert c.respond_within_hours == 4


def test_card_cannot_expose_a_clickable_sender_or_full_account(tmp_path):
    c = replay("case_b", tmp_path).finance_case
    md = to_markdown(c)
    assert "serverobotlcs.com" not in md and "serverobotlcs[.]com" in md
    assert "@" not in md and "123456789" not in md and "ending 6789" in md


def test_injection_text_never_reaches_the_card(tmp_path):
    md = to_markdown(replay("case_e", tmp_path).finance_case)
    assert "SYSTEM NOTE" not in md and "pre-verified" not in md


def test_ambiguous_vendor_case_requests_documents(tmp_path):
    c = replay("case_d", tmp_path).finance_case
    assert "ambiguous" in c.vendor and c.document_requests and not c.call_list


@pytest.mark.parametrize("name", ["case_b", "case_c", "case_d", "test_3_forwarded_gmail_inr", "test_7_almost_empty"])
def test_every_card_passes_its_checks(name, tmp_path):
    c = replay(name, tmp_path).finance_case
    assert all(check["passed"] for check in c.checks), c.checks


def test_one_approval_releases_a_hold_but_nothing_else_does(tmp_path):
    for action, released in [("Confirmed legitimate", True), ("Confirmed fraud", False), ("Can't reach vendor", False)]:
        c = apply_action(replay("case_b", tmp_path).finance_case, action, "Asha")
        assert ("RELEASED" in c.payment_status) is released and c.resolution == action


def test_escalation_needs_two_different_people(tmp_path):
    c = replay("case_c", tmp_path).finance_case
    apply_action(c, "Confirmed legitimate", "Asha")
    assert "second approver" in c.payment_status and not c.closed
    apply_action(c, "Confirmed legitimate", "Ravi")
    assert "RELEASED by two approvers" in c.payment_status and c.closed
    assert [a["name"] for a in c.approvals] == ["Asha", "Ravi"]


@pytest.mark.parametrize("same", ["Asha", "asha", "  ASHA ", "As\u0020ha".replace(" ", "")])
def test_the_same_person_cannot_approve_twice(same, tmp_path):
    c = replay("case_c", tmp_path).finance_case
    apply_action(c, "Confirmed legitimate", "Asha")
    with pytest.raises(ValueError, match="different person"):
        apply_action(c, "Confirmed legitimate", same)
    assert "BLOCKED" in c.payment_status and len(c.approvals) == 1


@pytest.mark.parametrize("name", ["", "   ", None])
def test_an_action_needs_a_name(name, tmp_path):
    c = replay("case_b", tmp_path).finance_case
    with pytest.raises(ValueError, match="name"):
        apply_action(c, "Confirmed legitimate", name)
    assert c.approvals == [] and "BLOCKED" in c.payment_status


def test_fraud_overrides_a_first_approval_and_closes_the_case(tmp_path):
    c = replay("case_c", tmp_path).finance_case
    apply_action(c, "Confirmed legitimate", "Asha")
    apply_action(c, "Confirmed fraud", "Ravi")
    assert "reported" in c.payment_status and c.closed
    with pytest.raises(ValueError, match="closed"):
        apply_action(c, "Confirmed legitimate", "Meera")
    assert "RELEASED" not in c.payment_status


def test_cant_reach_keeps_it_blocked_and_open(tmp_path):
    c = replay("case_c", tmp_path).finance_case
    apply_action(c, "Confirmed legitimate", "Asha")
    apply_action(c, "Can't reach vendor", "Ravi")
    assert "BLOCKED" in c.payment_status and not c.closed
    apply_action(c, "Confirmed legitimate", "Ravi")
    assert "RELEASED" in c.payment_status


def test_approvals_survive_saving_and_appear_on_the_case_file(tmp_path):
    from clearance.schemas import CaseResult
    r = replay("case_c", tmp_path)
    apply_action(r.finance_case, "Confirmed legitimate", "Asha")
    again = CaseResult.model_validate_json(r.model_dump_json())
    assert again.finance_case.approvals[0]["name"] == "Asha"
    assert "by Asha" in to_markdown(again.finance_case)


def test_unknown_action_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        apply_action(replay("case_b", tmp_path).finance_case, "Pay it", "Asha")


def test_offline_miss_marks_the_case_incomplete_and_still_blocks(tmp_path):
    client = CrustdataClient(None, tmp_path / "empty", offline=True)
    r = run_case((ROOT / "packets" / "case_b.txt").read_text(), client, POLICY, date(2026, 10, 31), outbox=tmp_path)
    assert r.finance_case.data_incomplete and "BLOCKED" in r.finance_case.payment_status
    assert "Data incomplete" in to_markdown(r.finance_case)


def test_report_labels_the_handoff_as_simulated(tmp_path):
    assert "simulated, not sent" in report_markdown(replay("case_b", tmp_path))


def test_defang():
    assert defang("see a@b.com at https://x.co or www.y.org") == "see [address withheld] at x[.]co or y[.]org"
