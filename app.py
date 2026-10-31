"""Streamlit mailbox UI: python -m streamlit run app.py

All mail content is untrusted, so it is shown as plain text and links are never clickable.
Runs from this UI save responses to the git-ignored private folder, never to public fixtures.
"""
from __future__ import annotations

import re
from datetime import date

import streamlit as st

from clearance.config import FIXTURES_DIR, OUTBOX_DIR, PRIVATE_FIXTURES_DIR, ROOT, credit_cap, crustdata_api_key, load_policy
from clearance.crustdata import CrustdataClient
from clearance.extract import extract_regex
from clearance.handoff import ACTIONS, apply_action, write_case
from clearance.intake import read_upload
from clearance.mailbox import MailItem, fetch_mailbox, item_from_text, mailbox_settings, sample_inbox
from clearance.pipeline import run_case
from clearance.report import mask, offline_warning, person_names, to_markdown
from clearance.schemas import CaseResult

STATUS_ICON = {"PASS": "✅", "FLAG": "🚩", "UNVERIFIABLE": "❔"}
OUTCOME_ICON = {"APPROVE": "🟢", "HOLD": "🟡", "ESCALATE": "🔴"}
OUTCOME_STYLE = {"APPROVE": st.success, "HOLD": st.warning, "ESCALATE": st.error}
ORIGIN_LABEL = {"sample": "Sample", "upload": "Uploaded", "mailbox": "Mailbox", "paste": "Pasted"}
VIEWS = ["All", "Needs review", "Escalated", "Approved", "Not scanned"]

st.set_page_config(page_title="Clearance", page_icon="🛡️", layout="wide")
st.markdown(
    """<style>
    .st-key-inbox button {justify-content:flex-start; text-align:left; min-height:3.2rem;}
    .st-key-inbox button > div {justify-content:flex-start; width:100%;}
    .st-key-inbox button p {white-space:normal; line-height:1.3; font-size:0.86rem; text-align:left;}
    .st-key-inbox button [data-testid="stMarkdownContainer"] {width:100%;}
    .block-container {padding-top:2.2rem;}
    </style>""",
    unsafe_allow_html=True,
)

state = st.session_state
if "items" not in state:
    state["items"] = {item.id: item for item in sample_inbox(ROOT / "packets")}
    state["results"] = {}
    state["selected"] = next(iter(state["items"]), None)


def add_item(item: MailItem) -> None:
    state["items"] = {item.id: item, **{k: v for k, v in state["items"].items() if k != item.id}}
    state["selected"] = item.id


def plain(text: str, limit: int = 70) -> str:
    """Safe for a button label or Markdown: no links, emphasis or HTML from mail content."""
    cleaned = re.sub(r"[\[\]()<>*_`~$#|\\\"]", " ", text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def escaped(text: str, limit: int = 800) -> str:
    """Keeps punctuation such as [.] and (hard) visible while making Markdown inert."""
    return re.sub(r"([\\`*_{}\[\]()<>#+!|~$])", r"\\\1", text[:limit])


def item_names(item: MailItem) -> list[str]:
    claim = extract_regex(item.text)
    display = re.sub(r"<[^>]*>", " ", item.sender).strip(" \"'")
    names = [claim.sender_name, claim.signature_name, display if display != "Unknown sender" else None]
    return [n for n in names if n]


def check(item: MailItem, replay_samples: bool) -> CaseResult:
    policy = load_policy()
    offline = item.origin == "sample" and replay_samples
    as_of = policy["offline_as_of"] if offline else date.today()
    if offline:
        client = CrustdataClient(None, FIXTURES_DIR, offline=True)
    else:
        client = CrustdataClient(crustdata_api_key(), PRIVATE_FIXTURES_DIR, credit_cap=credit_cap(), replay_dirs=(FIXTURES_DIR,))
    return run_case(item.text, client, policy, as_of, case_id=item.id, outbox=OUTBOX_DIR)


def view_of(item: MailItem) -> str:
    result = state["results"].get(item.id)
    if not result:
        return "Not scanned"
    return {"APPROVE": "Approved", "HOLD": "Needs review", "ESCALATE": "Escalated"}[result.decision.outcome]


with st.sidebar:
    st.markdown("## 🛡️ Clearance")
    st.caption("Is the person asking for this payment real, and who should you call instead?")
    replay = st.toggle("Replay samples offline", value=True, help="Sample mail uses saved responses and spends no credits. Uploaded, pasted and mailbox mail always runs live.")
    masked = st.toggle("Mask people's names", value=True)

    st.divider()
    st.markdown("**Mailbox**")
    settings = mailbox_settings()
    if settings:
        st.caption(f"Read-only connection to {plain(settings['folder'])}. Nothing is sent, moved or marked as read.")
        if st.button("Sync payment emails", width="stretch"):
            with st.spinner("Reading mailbox…"):
                fetched, error = fetch_mailbox(settings)
            if error:
                st.error(error)
            else:
                for item in reversed(fetched):
                    add_item(item)
                st.success(f"Added {len(fetched)} payment-related email(s).")
    else:
        st.caption("Not connected. Add IMAP_HOST, IMAP_USER and IMAP_PASSWORD to .env and restart. Gmail needs an app password.")

    st.divider()
    st.markdown("**Add a document**")
    uploads = st.file_uploader("PDF, TXT or EML", type=["pdf", "txt", "md", "eml"], accept_multiple_files=True)
    seen = state.setdefault("seen_uploads", set())
    for upload in uploads or []:
        key = (upload.name, upload.size)
        if key in seen:
            continue
        seen.add(key)
        intake = read_upload(upload.name, upload.getvalue())
        if intake.text.strip():
            add_item(item_from_text(intake.text, "upload", upload.name, intake.warnings))
        else:
            st.error(f"{plain(upload.name)}: {' '.join(intake.warnings) or 'No readable text.'}")
    with st.expander("Paste text"):
        pasted = st.text_area("Email or invoice text", height=140, label_visibility="collapsed")
        if st.button("Add to inbox", width="stretch", disabled=not pasted.strip()):
            add_item(item_from_text(pasted, "paste", "Pasted text"))

items: dict[str, MailItem] = state["items"]
results: dict[str, CaseResult] = state["results"]

count = {v: sum(view_of(i) == v for i in items.values()) for v in VIEWS[1:]}
metrics = st.columns([1, 1, 1, 1, 1.4])
metrics[0].metric("Needs review", count["Needs review"])
metrics[1].metric("Escalated", count["Escalated"])
metrics[2].metric("Approved", count["Approved"])
metrics[3].metric("Credits spent", f"{sum(r.credits_used for r in results.values()):g}")
with metrics[4]:
    pending = [i for i in items.values() if i.id not in results]
    live_pending = [i for i in pending if not (i.origin == "sample" and replay)]
    note = f" ({len(live_pending)} live)" if live_pending else ""
    if st.button(f"Scan {len(pending)} email(s){note}", type="primary", width="stretch", disabled=not pending):
        bar = st.progress(0.0, "Scanning…")
        spent = 0.0
        for n, item in enumerate(pending, 1):
            if spent >= credit_cap():
                st.warning("Stopped: the credit cap was reached. Remaining emails were not scanned.")
                break
            results[item.id] = check(item, replay)
            spent += results[item.id].credits_used
            bar.progress(n / len(pending), f"Scanned {n} of {len(pending)}")
        bar.empty()
        st.rerun()


def render_result(result: CaseResult) -> None:
    names = person_names(result) if masked else []
    show = (lambda s: escaped(mask(s, names))) if masked else escaped
    d = result.decision
    if result.offline_misses:
        st.error(offline_warning(result))
    OUTCOME_STYLE[d.outcome](f"**{OUTCOME_ICON[d.outcome]} {d.outcome}**\n\n" + "\n\n".join(show(r) for r in d.reasons))

    verdict, evidence, callback = st.tabs(["Finance handoff", "Findings and evidence", "Callback and draft"])
    with verdict:
        case = result.finance_case
        if case:
            st.caption("Simulated: no message was sent and no payment system was touched.")
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
                if column.button(action, key=f"{case.id}-{action}", disabled=case.resolution is not None, width="stretch"):
                    apply_action(case, action)
                    write_case(case, OUTBOX_DIR)
                    st.rerun()
            if case.resolution:
                st.info(f"Recorded: {case.resolution}. Payment: {case.payment_status}.")
        else:
            st.write("No finance handoff is needed: the payment continues, with this evidence attached (simulated).")
        cols = st.columns(4)
        cols[0].metric("Credits", f"{result.credits_used:g}")
        cols[1].metric("Evidence records", len(result.ledger))
        cols[2].metric("Hash chain", "verified" if result.chain_ok else "BROKEN")
        cols[3].metric("Messages sent", 0)
    with evidence:
        ledger = {e.id: e for e in result.ledger}
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
        st.markdown("**What would change this decision?**")
        st.write(show(d.what_would_change_this))
        with st.expander("Full report"):
            st.code(to_markdown(result, masked), language=None, wrap_lines=True)
    with callback:
        if result.contact_search_error:
            st.write(f"The contact search could not run: {result.contact_search_error}. Use your own vendor records.")
        for s in result.callback_pack:
            st.markdown(f"- {show(s.text)} `{' '.join(s.evidence_ids[:3])}`")
        if result.resolution and result.resolution.status == "AMBIGUOUS":
            st.markdown("**Candidates (none picked)**")
            st.dataframe([c.model_dump(exclude={"evidence_ids"}) for c in result.resolution.candidates], hide_index=True, width="stretch")
        if result.document_requests:
            st.markdown("**Documents to request**")
            for r in result.document_requests:
                st.markdown(f"- {r}")
        for draft in result.drafts:
            st.markdown(f"**Draft: {draft.status}, never sent**")
            text = f"To: {', '.join(r.name for r in draft.recipients)}\nSubject: {draft.subject}\n\n{draft.body}"
            st.code(mask(text, names) if masked else text, language=None, wrap_lines=True)
            for c in draft.checks:
                st.markdown(f"{'✅' if c['passed'] else '❌'} {c['check']}")
        if not (result.callback_pack or result.drafts or result.document_requests or result.contact_search_error):
            st.write("Nothing to do: this request was approved.")
    st.caption("Bank accounts are not verified. Profile data can lag. These are risk findings, not accusations.")


inbox, pane = st.columns([1, 2.1], gap="medium")
with inbox:
    view = st.segmented_control("View", VIEWS, default="All", label_visibility="collapsed") or "All"
    with st.container(key="inbox", height=640, border=False):
        visible = [i for i in items.values() if view == "All" or view_of(i) == view]
        if not visible:
            st.caption("No email in this view.")
        for item in visible:
            result = results.get(item.id)
            icon = OUTCOME_ICON[result.decision.outcome] if result else "⚪"
            names = item_names(item) if masked else []
            sender = plain(mask(item.sender, names) if masked else item.sender, 34)
            subject = plain(mask(item.subject, names) if masked else item.subject, 62)
            if st.button(f"{icon} {sender}  \n{subject}", key=f"mail-{item.id}", width="stretch",
                         type="primary" if item.id == state["selected"] else "secondary"):
                state["selected"] = item.id
                st.rerun()

with pane:
    item = items.get(state["selected"])
    if not item:
        st.caption("Select an email.")
    else:
        names = item_names(item) if masked else []
        safe = (lambda s: mask(s, names)) if masked else (lambda s: s)
        st.markdown(f"#### {plain(safe(item.subject), 110)}")
        st.caption(f"{plain(safe(item.sender), 90)}  ·  {ORIGIN_LABEL[item.origin]}")
        for warning in item.warnings:
            st.warning(plain(warning, 200))
        result = results.get(item.id)
        if result:
            render_result(result)
        else:
            live = not (item.origin == "sample" and replay)
            if st.button("Check this email" + (" (live, spends credits)" if live else ""), type="primary"):
                with st.spinner("Checking the sender against the graph…"):
                    results[item.id] = check(item, replay)
                st.rerun()
        with st.expander("Original email", expanded=result is None):
            st.code(safe(item.text), language=None, wrap_lines=True)
            st.caption("Shown as plain text. Links in this message are not clickable.")
