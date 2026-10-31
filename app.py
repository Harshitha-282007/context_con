"""Streamlit mailbox UI: python -m streamlit run app.py

All mail content is untrusted, so it is shown as plain text and links are never clickable.
Runs from this UI save responses to the git-ignored private folder, never to public fixtures.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime

import streamlit as st

from clearance.config import FIXTURES_DIR, OUTBOX_DIR, PRIVATE_FIXTURES_DIR, ROOT, credit_cap, crustdata_api_key, daily_credit_limit, load_policy
from clearance.crustdata import CrustdataClient
from clearance.draft_llm import contextual_reply
from clearance.extract import extract_regex
from clearance.handoff import ACTIONS, apply_action, write_case
from clearance.intake import read_upload
from clearance.live import SyncReport, sync_and_scan
from clearance.mailbox import MailItem, item_from_text, mailbox_settings, sample_inbox
from clearance.outbound import (
    detected_sender, needs_hold_acknowledgement, neutral_reply, record_sent, recipients_for, reply_to_sender_enabled,
    send_allowlist, send_mail, sent_log, simulated_verification, smtp_settings,
)
from clearance.pipeline import run_case
from clearance.report import mask, offline_warning, person_names, to_markdown
from clearance.schemas import CaseResult
from clearance.store import make_store

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

SYNC_SECONDS = 60
state = st.session_state
if "store" not in state:
    try:
        state["store"] = make_store()
        state["store"].sync_samples(sample_inbox(ROOT / "packets"))
    except Exception as e:  # Any backend failure. The type alone is shown, because a message can echo keys or URLs.
        del state["store"]
        st.error(f"Could not open storage ({type(e).__name__}). Check the SUPABASE settings in .env and that supabase/schema.sql has been run.")
        st.stop()
store = state["store"]
state.setdefault("selected", None)


def add_item(item: MailItem) -> None:
    store.add(item)
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
        client = CrustdataClient(crustdata_api_key(), PRIVATE_FIXTURES_DIR, credit_cap=credit_cap(), replay_dirs=(FIXTURES_DIR,), expire=True)
    return run_case(item.text, client, policy, as_of, case_id=item.id, outbox=OUTBOX_DIR)


def view_of(item: MailItem) -> str:
    result = results.get(item.id)
    if not result:
        return "Not scanned"
    return {"APPROVE": "Approved", "HOLD": "Needs review", "ESCALATE": "Escalated"}[result.decision.outcome]


def run_cycle(settings: dict, auto_scan: bool) -> SyncReport:
    try:
        report = sync_and_scan(store, settings, lambda mail: check(mail, False), auto_scan, daily_credit_limit())
    except Exception as e:  # Storage or network. The type alone is shown, because a message can echo keys.
        report = SyncReport(error=f"The sync stopped ({type(e).__name__}). It will try again.")
    state["last_sync"] = time.time()
    state["last_report"] = report
    return report


@st.fragment(run_every=f"{SYNC_SECONDS}s")
def live_status(settings: dict, auto_scan: bool) -> None:
    """Runs on a timer while this page is open. There is no background worker yet."""
    if time.time() - state.get("last_sync", 0) >= SYNC_SECONDS - 5:
        if run_cycle(settings, auto_scan).changed:
            st.rerun(scope="app")
    report = state.get("last_report")
    if report:
        st.caption(f"Last checked {datetime.fromtimestamp(state['last_sync']):%H:%M:%S}. Checks every {SYNC_SECONDS} seconds while this page is open.")
        if report.error:
            st.error(report.error)
        if report.limit_reached:
            st.warning(f"Daily credit limit of {daily_credit_limit():g} reached. New mail is still imported but not scanned.")


with st.sidebar:
    st.markdown("## 🛡️ Clearance")
    st.caption("Is the person asking for this payment real, and who should you call instead?")
    replay = st.toggle("Replay samples offline", value=True, help="Sample mail uses saved responses and spends no credits. Uploaded, pasted and mailbox mail always runs live.")
    masked = st.toggle("Mask people's names", value=True)

    st.divider()
    st.markdown("**Inbox**")
    settings = mailbox_settings()
    mode = "Samples"
    if settings:
        mode = st.radio("Inbox", ["Live mailbox", "Samples"], horizontal=True, label_visibility="collapsed")
    else:
        st.caption("Showing samples. Add IMAP_HOST, IMAP_USER and IMAP_PASSWORD to .env and restart for a live mailbox. Gmail needs an app password.")
    if mode == "Live mailbox":
        st.caption(f"Reads {plain(settings['folder'])} over a read-only connection. Email only goes out when a person sends it below.")
        auto_scan = st.toggle("Auto-scan new mail", value=True, help=f"Scans new payment emails automatically, up to {daily_credit_limit():g} credits a day.")
        live_status(settings, auto_scan)
        if st.button("Sync now", width="stretch"):
            with st.spinner("Reading mailbox…"):
                run_cycle(settings, auto_scan)
            st.rerun()
    elif st.button("Reset samples", width="stretch", help="Clears the sample verdicts so you can scan them again."):
        store.reset_samples()
        st.rerun()
    st.caption(f"Storage: {store.backend}")

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

origins = ("mailbox", "upload", "paste") if mode == "Live mailbox" else ("sample", "upload", "paste")
try:
    items: dict[str, MailItem] = store.items(origins)
    results: dict[str, CaseResult] = store.results()
except Exception as e:  # The type alone is shown, because a message can echo keys or URLs.
    st.error(f"Could not read storage ({type(e).__name__}). Your mail is unchanged. Refresh to try again.")
    st.stop()
if state["selected"] not in items:
    state["selected"] = next(iter(items), None)

count = {v: sum(view_of(i) == v for i in items.values()) for v in VIEWS[1:]}
metrics = st.columns([1, 1, 1, 1, 1.4])
metrics[0].metric("Needs review", count["Needs review"])
metrics[1].metric("Escalated", count["Escalated"])
metrics[2].metric("Approved", count["Approved"])
metrics[3].metric("Credits today", f"{store.credits_on():g}")
with metrics[4]:
    pending = [i for i in items.values() if i.id not in results]
    live_pending = [i for i in pending if not (i.origin == "sample" and replay)]
    note = f" ({len(live_pending)} live)" if live_pending else ""
    if st.button(f"Scan {len(pending)} email(s){note}", type="primary", width="stretch", disabled=not pending):
        bar = st.progress(0.0, "Scanning…")
        for n, item in enumerate(pending, 1):
            if store.credits_on() >= daily_credit_limit():
                st.warning("Stopped: the daily credit limit was reached. Remaining emails were not scanned.")
                time.sleep(2)
                break
            store.save_result(item.id, check(item, replay))
            bar.progress(n / len(pending), f"Scanned {n} of {len(pending)}")
        bar.empty()
        st.rerun()


def reply_body(item: MailItem, result: CaseResult | None) -> str:
    """Written once per email from its checked fields, then reused, so Groq is not called again."""
    key = f"reply:{item.id}"
    saved = store.meta(key)
    if saved:
        return saved
    claim = result.claim if result else extract_regex(item.text)
    written = contextual_reply(claim, item.subject)
    if written:
        store.set_meta(key, written)
        return written
    return neutral_reply(claim)


def render_compose(item: MailItem, result: CaseResult | None) -> None:
    reply_enabled = reply_to_sender_enabled()
    sender = detected_sender(item.origin, item.text) if reply_enabled else None
    allowed = recipients_for(item.origin, item.text, send_allowlist(), reply_enabled)
    outcome_now = result.decision.outcome if result else None
    log = sent_log(store, item.id)
    with st.expander(f"Reply or send from Clearance ({len(log)} sent)" if log else "Reply or send from Clearance"):
        if not allowed:
            st.caption("Sending is off for this email. To turn it on, add SEND_TO_SENDER=true or SEND_ALLOWLIST to .env and restart. Replies to senders only work for live mailbox mail. The event rules do not allow messages to real outside people.")
            return
        if sender and outcome_now != "APPROVE":
            st.warning("This sender is not verified. If this is fraud, a reply tells them they were noticed. To confirm a request, use the phone number in your own vendor records.")
        with st.form(f"compose-{item.id}", clear_on_submit=False):
            labels = lambda a: (mask(a, []) if masked else a) + (" (sender)" if a == sender else "")
            to = st.selectbox("To", allowed, format_func=labels)
            subject = st.text_input("Subject", value=f"Re: {plain(item.subject, 120)}")
            body = st.text_area("Message", value=reply_body(item, result), height=260)
            confirmed = st.checkbox("I checked the recipient and message, and I am sending this myself.")
            held_ok = True
            if sender and outcome_now != "APPROVE":
                held_ok = st.checkbox("If sending to the sender: I understand this person has not been verified.")
            sent = st.form_submit_button("Send email", type="primary")
        if sent:
            ready = confirmed and (held_ok or not needs_hold_acknowledgement(to, sender, outcome_now))
            outcome = send_mail(smtp_settings(), allowed, to, subject, body, ready)
            if outcome.ok:
                record_sent(store, item.id, to, subject)
                st.success(f"Sent to {mask(to, []) if masked else to}.")
            else:
                st.error(outcome.error)
        for entry in sent_log(store, item.id):
            shown = mask(entry["to"], []) if masked else entry["to"]
            st.caption(f"Sent to {shown} at {entry['at'][:16].replace('T', ' ')} UTC: {plain(entry['subject'], 80)}")


def render_simulated_send(draft, names: list[str]) -> None:
    """Delivers the draft to a test address only, to show the flow without contacting a vendor."""
    allowed = send_allowlist()
    if draft.status != "DRAFT_ONLY" or not draft.recipients:
        return
    st.markdown("**Simulated send**")
    if not allowed:
        st.caption("Add SEND_ALLOWLIST with your test address to .env and restart to deliver this draft to yourself. No vendor is contacted.")
        return
    to = st.selectbox("Test address", allowed, key=f"sim-to-{draft.id}", format_func=(lambda a: mask(a, [])) if masked else None)
    st.caption("Goes only to this test address, labelled as simulated. The vendor contact is not emailed, because the event rules do not allow it and this key has no contact addresses.")
    if st.button("Send verification (simulated)", key=f"sim-{draft.id}"):
        subject, body = simulated_verification(draft, (lambda s: mask(s, names)) if masked else (lambda s: s))
        outcome = send_mail(smtp_settings(), allowed, to, subject, body, True)
        if outcome.ok:
            record_sent(store, state["selected"], to, subject)
            st.success("Sent to your test address. Check that inbox.")
        else:
            st.error(outcome.error)


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
            st.caption("Simulated: finance was not contacted and no payment system was touched.")
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
            for entry in case.approvals:
                shown = mask(entry["name"], [entry["name"]]) if masked else entry["name"]
                st.caption(f"{plain(entry['action'])}, by {plain(shown, 60)}, {entry['at'][:16].replace('T', ' ')} UTC")
            if case.closed:
                st.info(f"Closed. Payment: {case.payment_status}.")
            else:
                st.write("Record the outcome of the callback:")
                approver = st.text_input("Your name", key=f"{case.id}-approver", help="Typed for this demo. Production would use a login, so names could not be faked.")
                buttons = st.columns(len(ACTIONS))
                for column, action in zip(buttons, ACTIONS):
                    if column.button(action, key=f"{case.id}-{action}", width="stretch"):
                        try:
                            apply_action(case, action, approver)
                        except ValueError as refused:
                            st.error(str(refused))
                        else:
                            write_case(case, OUTBOX_DIR)
                            store.save_result(state["selected"], result)
                            st.rerun()
                if case.resolution:
                    st.info(f"Recorded: {case.resolution}. Payment: {case.payment_status}.")
        else:
            st.write("No finance handoff is needed: the payment continues, with this evidence attached (simulated).")
        cols = st.columns(4)
        cols[0].metric("Credits", f"{result.credits_used:g}")
        cols[1].metric("Evidence records", len(result.ledger))
        cols[2].metric("Hash chain", "verified" if result.chain_ok else "BROKEN")
        cols[3].metric("Sent by the agent", 0)
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
            st.markdown(f"**Draft: {draft.status}**")
            st.caption(f"Wording: {draft.drafted_by}")
            text = f"To: {', '.join(r.name for r in draft.recipients)}\nSubject: {draft.subject}\n\n{draft.body}"
            st.code(mask(text, names) if masked else text, language=None, wrap_lines=True)
            for c in draft.checks:
                st.markdown(f"{'✅' if c['passed'] else '❌'} {c['check']}")
            render_simulated_send(draft, names)
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
            if st.button("Check again", help="Runs a fresh check. A live check may spend credits."):
                with st.spinner("Checking again…"):
                    store.recheck_result(item.id, check(item, replay))
                st.rerun()
        else:
            live = not (item.origin == "sample" and replay)
            if st.button("Check this email" + (" (live, spends credits)" if live else ""), type="primary"):
                with st.spinner("Checking the sender against the graph…"):
                    store.save_result(item.id, check(item, replay))
                st.rerun()
        render_compose(item, result)
        with st.expander("Original email", expanded=result is None):
            st.code(safe(item.text), language=None, wrap_lines=True)
            st.caption("Shown as plain text. Links in this message are not clickable.")
