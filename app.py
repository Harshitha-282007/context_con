"""Streamlit UI: python -m streamlit run app.py"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import streamlit as st

from clearance.config import FIXTURES_DIR, OUTBOX_DIR, ROOT, credit_cap, crustdata_api_key, load_policy
from clearance.crustdata import CrustdataClient
from clearance.extract import extract_regex
from clearance.handoff import ACTIONS, apply_action, write_case
from clearance.pipeline import run_case
from clearance.report import mask, offline_warning, person_names, to_markdown

STATUS_ICON = {"PASS": "✅", "FLAG": "🚩", "UNVERIFIABLE": "❔"}
OUTCOME_STYLE = {"APPROVE": st.success, "HOLD": st.warning, "ESCALATE": st.error}

st.set_page_config(page_title="Clearance", page_icon="🛡️", layout="wide")
st.title("Clearance")
st.caption("Is the person asking for this payment real, and who should you call instead?")

packets = sorted((ROOT / "packets").glob("*.txt"))
with st.sidebar:
    choice = st.selectbox("Sample packet", [p.name for p in packets] + ["Paste my own"])
    # A pasted packet has no saved responses, so it defaults to a live run.
    offline = st.toggle("Offline replay", value=choice != "Paste my own", help="Serve saved responses and make no network calls. New packets need this off.")
    masked = st.toggle("Mask people's names", value=True)

default = "" if choice == "Paste my own" else (ROOT / "packets" / choice).read_text()
label = "Packet (invoice, email thread or vendor form)"
if masked and default:  # The box shows a masked view for recordings; checks run on the real text.
    claim = extract_regex(default)
    st.text_area(label, value=mask(default, [n for n in (claim.sender_name, claim.signature_name) if n]), height=240, disabled=True, key=f"masked-{choice}")
    text = default
else:
    text = st.text_area(label, value=default, height=240, key=f"packet-{choice}")

if st.button("Run checks", type="primary"):
    policy = load_policy()
    as_of = policy["offline_as_of"] if offline else date.today()
    client = CrustdataClient(crustdata_api_key(), FIXTURES_DIR, offline=offline, credit_cap=credit_cap())
    log: list[str] = []
    with st.status("Checking the request…", expanded=True) as status:
        def emit(stage: str, message: str) -> None:
            log.append(f"{stage}: {message}")
            if stage != "rule":
                st.write(f"**{stage}**: {mask(message, []) if masked else message}")

        case_id = Path(choice).stem if choice != "Paste my own" else "pasted"
        result = run_case(text, client, policy, as_of, case_id=case_id, emit=emit)
        status.update(label=f"Decision: {result.decision.outcome}", state="complete", expanded=False)
    st.session_state["result"] = result

result = st.session_state.get("result")
if result:
    names = person_names(result) if masked else []
    show = (lambda s: mask(s, names)) if masked else (lambda s: s)
    d = result.decision
    if result.offline_misses:
        st.error(offline_warning(result))
    OUTCOME_STYLE[d.outcome](f"### {d.outcome}\n\n" + "\n\n".join(show(r) for r in d.reasons))

    cols = st.columns(4)
    cols[0].metric("Credits spent", f"{result.credits_used:g}")
    cols[1].metric("Evidence records", len(result.ledger))
    cols[2].metric("Hash chain", "verified" if result.chain_ok else "BROKEN")
    cols[3].metric("Messages sent", 0)

    case = result.finance_case
    if case:
        st.subheader("Finance handoff (simulated, not sent)")
        with st.container(border=True):
            top = st.columns(3)
            top[0].markdown(f"**Route to**\n\n{case.route_to}")
            top[1].markdown(f"**Respond within**\n\n{case.respond_within_hours} hours")
            top[2].markdown(f"**Payment**\n\n{case.payment_status}")
            st.caption(f"Release requires: {case.release_requires}")
            if case.bank_change:
                st.write(f"Bank change requested{', account ' + case.account if case.account else ''}.")
            for f in case.flags:
                st.markdown(f"- **{f.rule_id}** ({f.severity}): {show(f.message)}")
            if case.unverified:
                st.caption(f"Could not verify: {', '.join(case.unverified)}")
            st.write("Record the outcome of the callback:")
            buttons = st.columns(len(ACTIONS))
            for column, action in zip(buttons, ACTIONS):
                if column.button(action, key=f"{case.id}-{action}", disabled=case.resolution is not None):
                    apply_action(case, action)
                    write_case(case, OUTBOX_DIR)
                    st.rerun()
            if case.resolution:
                st.info(f"Recorded: {case.resolution}. Payment: {case.payment_status}.")
            st.caption(f"No message was sent and no payment system was touched. Saved to outbox/{case.id}.md.")
    elif d.outcome == "APPROVE":
        st.caption("No finance handoff: the payment continues, with this evidence attached (simulated).")

    ledger = {e.id: e for e in result.ledger}
    left, right = st.columns(2)
    with left:
        st.subheader("Findings")
        for f in result.findings:
            label = f"{STATUS_ICON[f.status]} {f.rule_id} {f.severity if f.status == 'FLAG' else f.status.lower()}"
            with st.expander(label, expanded=f.status == "FLAG"):
                st.write(show(f.message))
                rows = [
                    {"id": i, "source": ledger[i].source, "endpoint": ledger[i].endpoint, "field": ledger[i].field_path,
                     "value": show(str(ledger[i].value))[:120], "hash": ledger[i].hash[:12]}
                    for i in f.evidence_ids if i in ledger
                ]
                st.dataframe(rows, hide_index=True, width="stretch")
    with right:
        if result.contact_search_error:
            st.subheader("Who to call instead")
            st.write(f"The contact search could not run: {result.contact_search_error}. Use your own vendor records.")
        if result.callback_pack:
            st.subheader("Who to call instead")
            for s in result.callback_pack:
                st.markdown(f"- {show(s.text)} `{' '.join(s.evidence_ids[:3])}`")
        if result.resolution and result.resolution.status == "AMBIGUOUS":
            st.subheader("Candidates (none picked)")
            st.dataframe([c.model_dump(exclude={"evidence_ids"}) for c in result.resolution.candidates], hide_index=True, width="stretch")
        if result.document_requests:
            st.subheader("Documents to request")
            for r in result.document_requests:
                st.markdown(f"- {r}")
        for draft in result.drafts:
            st.subheader(f"Draft: {draft.status} (never sent)")
            st.text(show(f"To: {', '.join(r.name for r in draft.recipients)}\nSubject: {draft.subject}\n\n{draft.body}"))
            for c in draft.checks:
                st.markdown(f"{'✅' if c['passed'] else '❌'} {c['check']}")
        st.subheader("What would change this decision?")
        st.write(show(d.what_would_change_this))

    with st.expander("Full report"):
        st.markdown(to_markdown(result, masked))
    with st.expander("Evidence ledger"):
        st.dataframe(
            [{"id": e.id, "source": e.source, "endpoint": e.endpoint, "field": e.field_path, "value": show(str(e.value))[:120],
              "credits": e.credits, "prev_hash": e.prev_hash[:12], "hash": e.hash[:12]} for e in result.ledger],
            hide_index=True, width="stretch",
        )
    st.caption("Bank accounts are not verified. Profile data can lag. These are risk findings, not accusations. The callback is a human step.")
