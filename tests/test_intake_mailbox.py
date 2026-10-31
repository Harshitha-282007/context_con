"""Uploads, the read-only mailbox and the app itself. No network."""
import imaplib
from email.message import EmailMessage

import pytest
from streamlit.testing.v1 import AppTest

from clearance.config import ROOT
from clearance.crustdata import CrustdataClient
from clearance.extract import extract_regex
from clearance.intake import MAX_FILE_BYTES, eml_to_text, html_to_text, pdf_to_text, read_upload
from clearance.mailbox import fetch_mailbox, item_from_text, looks_payment_related, mailbox_settings, sample_inbox

INVOICE = ["Vendor: Retool", "Invoice: INV-9", "Total: $18,400.00", "Website: www.retool.com"]


def make_pdf(lines) -> bytes:
    """A minimal real PDF with a text layer."""
    stream = "BT /F1 12 Tf 40 760 Td 14 TL " + " ".join(f"({l}) Tj T*" for l in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = "%PDF-1.4\n", []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode()


def make_eml(html=False, attachment=None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Dana Sample <dana@retool.com>"
    msg["Subject"] = "Invoice INV-9"
    body = "\n".join(INVOICE)
    msg.set_content(body)
    if html:
        msg.add_alternative("<html><head><style>x{}</style></head><body><p>Total: $18,400.00</p><script>steal()</script></body></html>", subtype="html")
    if attachment:
        msg.add_attachment(attachment, maintype="application", subtype="pdf", filename="invoice.pdf")
    return bytes(msg)


def test_pdf_text_reaches_extraction():
    intake = read_upload("invoice.pdf", make_pdf(INVOICE))
    claim = extract_regex(intake.text)
    assert claim.vendor_name == "Retool" and claim.amount == 18400 and not intake.warnings


def test_pdf_without_text_warns_that_it_may_be_a_scan():
    assert "scan" in pdf_to_text(make_pdf([])).warnings[0]


def test_damaged_pdf_warns_instead_of_crashing():
    intake = read_upload("x.pdf", b"not a pdf at all")
    assert not intake.text and "could not be read" in intake.warnings[0]


def test_eml_keeps_real_sender_and_reads_pdf_attachment():
    intake = read_upload("m.eml", make_eml(attachment=make_pdf(["Amount due: $77,000.00"])))
    claim = extract_regex(intake.text)
    assert claim.sender_email == "dana@retool.com" and claim.vendor_name == "Retool"
    assert "77,000.00" in intake.text


def test_html_only_mail_loses_scripts_and_styles():
    assert html_to_text("<style>x{}</style><p>Total: $5</p><script>steal()</script>") == "Total: $5"


def test_non_pdf_attachment_is_skipped_with_a_warning():
    msg = EmailMessage()
    msg["From"] = "a@b.example"
    msg.set_content("hello")
    msg.add_attachment(b"MZ", maintype="application", subtype="octet-stream", filename="payload.exe")
    intake = eml_to_text(bytes(msg))
    assert "MZ" not in intake.text and "skipped" in intake.warnings[0]


def test_unsupported_and_oversized_files_are_refused():
    assert "not supported" in read_upload("run.exe", b"MZ").warnings[0]
    assert "10 MB" in read_upload("big.txt", b"x" * (MAX_FILE_BYTES + 1)).warnings[0]


class FakeImap:
    """Fails the test if anything but a read-only operation is attempted."""
    login_error = False
    last = None

    def __init__(self, host, timeout=None):
        self.calls = []
        FakeImap.last = self

    def login(self, user, password):
        if FakeImap.login_error:
            raise imaplib.IMAP4.error(f"bad login for {password}")

    def select(self, folder, readonly=False):
        self.calls.append(("select", readonly))
        return "OK", [b"3"]

    def uid(self, command, *args):
        if command == "SEARCH":
            self.calls.append(("search", args[1]))
            return "OK", [b"1 2 3"]
        assert command == "FETCH", f"Mailbox must stay read-only, but sent {command}"
        self.calls.append(("fetch", args[1]))
        bodies = {b"1": make_eml(), b"2": b"From: friend@x.example\nSubject: lunch?\n\nsee you", b"3": make_eml(html=True)}
        return "OK", [(b"header", bodies[args[0]])]

    def logout(self):
        self.calls.append(("logout",))

    def __getattr__(self, name):
        raise AssertionError(f"Mailbox must stay read-only, but called {name}")


SETTINGS = {"host": "imap.example", "user": "u", "password": "SECRET", "folder": "INBOX"}


def test_mailbox_is_read_only_and_keeps_only_payment_mail(monkeypatch):
    monkeypatch.setattr(FakeImap, "login_error", False)
    items, error, newest = fetch_mailbox(SETTINGS, imap_class=FakeImap)
    assert newest == 3 and error is None and len(items) == 2 and all("lunch" not in i.subject for i in items)
    assert ("select", True) in FakeImap.last.calls
    assert all("PEEK" in c[1] for c in FakeImap.last.calls if c[0] == "fetch")


def test_mailbox_login_failure_never_shows_server_text(monkeypatch):
    monkeypatch.setattr(FakeImap, "login_error", True)
    items, error, _ = fetch_mailbox(SETTINGS, imap_class=FakeImap)
    assert items == [] and "SECRET" not in error and "app password" in error


def test_mailbox_unreachable_gives_a_clear_error():
    def boom(host, timeout=None):
        raise OSError("no route")
    assert "reach the mail server" in fetch_mailbox(SETTINGS, imap_class=boom)[1]


def test_mailbox_settings_need_all_three(monkeypatch):
    for key in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    assert mailbox_settings() is None
    monkeypatch.setenv("IMAP_HOST", "h")
    monkeypatch.setenv("IMAP_USER", "u")
    assert mailbox_settings() is None
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    assert mailbox_settings()["folder"] == "INBOX"


def test_payment_filter():
    assert looks_payment_related("Your invoice", "")
    assert not looks_payment_related("Lunch?", "see you at noon")


def test_sample_inbox_reads_every_packet():
    items = sample_inbox(ROOT / "packets")
    assert len(items) == len(list((ROOT / "packets").glob("*.txt")))
    assert len({i.id for i in items}) == len(items)


def test_forwarded_item_shows_the_innermost_sender():
    item = item_from_text("From: clerk@o.example\nSubject: Fwd: x\n> From: real@v.example", "paste")
    assert item.sender == "real@v.example"


def test_private_runs_replay_public_data_but_write_privately(tmp_path):
    public, private = tmp_path / "public", tmp_path / "private"
    public.mkdir()
    CrustdataClient("k", public, session=type("S", (), {"post": lambda self, *a, **k: type("R", (), {"status_code": 200, "headers": {}, "json": lambda s: [1], "text": ""})()})()).post("/company/x", {"n": 1})
    client = CrustdataClient(None, private, replay_dirs=(public,))
    assert client.post("/company/x", {"n": 1}).data == [1]
    assert not private.exists() or not list(private.glob("*.json"))


@pytest.fixture
def app(monkeypatch, tmp_path):
    for key in ("ANTHROPIC_API_KEY", "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SEND_ALLOWLIST", "SEND_TO_SENDER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLEARANCE_DATA_DIR", str(tmp_path))
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()


def label_button(app, start):
    return next(b for b in app.button if b.label.startswith(start))


def test_app_opens_on_the_inbox(app):
    assert not app.exception
    assert sum(b.key.startswith("mail-") for b in app.button if b.key) == len(list((ROOT / "packets").glob("*.txt")))


def test_app_scans_the_sample_inbox_without_credits(app):
    label_button(app, "Scan").click().run()
    assert not app.exception
    labels = [b.label for b in app.button if b.key and b.key.startswith("mail-")]
    assert all(l[0] != "⚪" for l in labels)
    assert any(l[0] == "🔴" for l in labels) and any(l[0] == "🟡" for l in labels) and any(l[0] == "🟢" for l in labels)
    assert [m.value for m in app.metric if m.label == "Credits today"] == ["0"]


def test_verdicts_survive_a_new_session(app):
    label_button(app, "Scan").click().run()
    again = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    labels = [b.label for b in again.button if b.key and b.key.startswith("mail-")]
    assert not again.exception and all(l[0] != "⚪" for l in labels)


def test_reset_samples_clears_verdicts_for_a_fresh_scan(app):
    label_button(app, "Scan").click().run()
    label_button(app, "Reset samples").click().run()
    assert all(b.label[0] == "⚪" for b in app.button if b.key and b.key.startswith("mail-"))


def test_sending_is_off_without_an_allowlist(app):
    assert not app.exception
    assert not any(b.label == "Send email" for b in app.button)
    assert any("Sending is off" in c.value for c in app.caption)


def open_hero_email(app):
    label_button(app, "Scan").click().run()
    next(b for b in app.button if b.key and b.key.startswith("mail-") and "INV-7731" in b.label).click().run()


def test_simulated_send_needs_a_test_address(app):
    open_hero_email(app)
    assert not app.exception
    assert not any("simulated" in b.label for b in app.button)
    assert any("SEND_ALLOWLIST" in c.value and "No vendor" in c.value for c in app.caption)


def test_simulated_send_appears_with_a_test_address(monkeypatch, tmp_path):
    for key in ("ANTHROPIC_API_KEY", "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SEND_TO_SENDER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLEARANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEND_ALLOWLIST", "test@x.example")
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    open_hero_email(app)
    assert not app.exception
    assert any(b.label == "Send verification (simulated)" for b in app.button)


def _saved(folder, endpoint, retrieved_at="2026-10-31T00:00:00+00:00"):
    import json as js
    from clearance.crustdata import cache_key
    folder.mkdir(exist_ok=True)
    (folder / f"{cache_key('POST', endpoint, {})}.json").write_text(js.dumps({"status": 200, "endpoint": endpoint, "response": "saved", "credits": 0, "retrieved_at": retrieved_at}))


def test_real_checks_never_read_pseudonymized_people(tmp_path):
    public = tmp_path / "public"
    _saved(public, "/person/search")
    _saved(public, "/company/enrich")
    client = CrustdataClient(None, tmp_path / "private", replay_dirs=(public,))
    assert client.post("/company/enrich", {}).data == "saved"
    assert not client.post("/person/search", {}).ok


def test_offline_samples_still_replay_people(tmp_path):
    _saved(tmp_path, "/person/search")
    assert CrustdataClient(None, tmp_path, offline=True).post("/person/search", {}).data == "saved"


def test_old_answers_expire_for_live_checks(tmp_path):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    _saved(tmp_path, "/person/search", (now - timedelta(hours=2)).isoformat())
    assert CrustdataClient(None, tmp_path, expire=True).post("/person/search", {}).data == "saved"
    _saved(tmp_path, "/person/search", (now - timedelta(days=2)).isoformat())
    assert not CrustdataClient(None, tmp_path, expire=True).post("/person/search", {}).ok
    assert CrustdataClient(None, tmp_path).post("/person/search", {}).data == "saved"
    _saved(tmp_path, "/company/enrich", (now - timedelta(days=3)).isoformat())
    assert CrustdataClient(None, tmp_path, expire=True).post("/company/enrich", {}).data == "saved"


def test_two_person_approval_in_the_app(app):
    label_button(app, "Scan").click().run()
    next(b for b in app.button if b.key and b.key.startswith("mail-") and "AL-5520" in b.label).click().run()
    approver = next(t for t in app.text_input if t.label == "Your name")
    approver.set_value("Asha").run()
    label_button(app, "Confirmed legitimate").click().run()
    assert not app.exception and any("second approver" in i.value for i in app.info)
    next(t for t in app.text_input if t.label == "Your name").set_value("asha").run()
    label_button(app, "Confirmed legitimate").click().run()
    assert any("different person" in e.value for e in app.error)
    next(t for t in app.text_input if t.label == "Your name").set_value("Ravi").run()
    label_button(app, "Confirmed legitimate").click().run()
    assert not app.exception and any("RELEASED by two approvers" in i.value for i in app.info)
    assert not any(b.label == "Confirmed legitimate" for b in app.button)
