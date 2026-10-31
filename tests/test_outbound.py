"""Outbound email guards, using a fake mail server. Nothing is sent."""
import smtplib

import pytest

from clearance.outbound import (
    detected_sender, needs_hold_acknowledgement, recipients_for, record_sent, reply_to_sender_enabled,
    send_allowlist, send_mail, sent_log, smtp_settings,
)
from clearance.store import SqliteStore

SETTINGS = {"host": "smtp.example", "port": 465, "user": "me@x.example", "password": "SECRET"}
ALLOW = ["test@x.example"]


class FakeSmtp:
    sent = []
    fail = None

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        if FakeSmtp.fail:
            raise FakeSmtp.fail

    def send_message(self, msg, to_addrs=None):
        FakeSmtp.sent.append((msg, to_addrs))


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(FakeSmtp, "sent", [])
    monkeypatch.setattr(FakeSmtp, "fail", None)


def send(to="test@x.example", subject="Hi", body="Hello", confirmed=True, allow=ALLOW, settings=SETTINGS):
    return send_mail(settings, allow, to, subject, body, confirmed, smtp_class=FakeSmtp)


def test_allowed_confirmed_mail_goes_only_to_that_recipient():
    assert send().ok
    msg, to = FakeSmtp.sent[0]
    assert to == ["test@x.example"] and msg["To"] == "test@x.example" and msg["From"] == "me@x.example"


@pytest.mark.parametrize("kwargs", [
    dict(to="stranger@x.example"),
    dict(to="test@x.example, stranger@x.example"),
    dict(to="test@x.example\nBcc: stranger@x.example"),
    dict(to=""),
    dict(confirmed=False),
    dict(allow=[]),
    dict(settings=None),
    dict(subject=" "),
    dict(body=" "),
    dict(subject="Hi\nBcc: stranger@x.example"),
    dict(body="x" * 20001),
])
def test_anything_else_is_refused_and_nothing_goes_out(kwargs):
    result = send(**kwargs)
    assert not result.ok and result.error and FakeSmtp.sent == []


def test_login_failure_never_shows_server_text():
    FakeSmtp.fail = smtplib.SMTPAuthenticationError(535, b"bad password SECRET")
    result = send()
    assert not result.ok and "SECRET" not in result.error


def test_network_failure_is_reported():
    FakeSmtp.fail = OSError("down SECRET")
    result = send()
    assert not result.ok and "SECRET" not in result.error


def test_allowlist_is_off_by_default_and_ignores_bad_entries(monkeypatch):
    monkeypatch.delenv("SEND_ALLOWLIST", raising=False)
    assert send_allowlist() == []
    monkeypatch.setenv("SEND_ALLOWLIST", " A@x.example ,not-an-address,, b@y.example")
    assert send_allowlist() == ["a@x.example", "b@y.example"]


def test_smtp_settings_default_to_gmail(monkeypatch):
    monkeypatch.setenv("IMAP_USER", "u@x.example")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    assert smtp_settings()["host"] == "smtp.gmail.com" and smtp_settings()["port"] == 465


def test_sent_log_records_each_send(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    assert sent_log(store, "m") == []
    record_sent(store, "m", "a@x.example", "One")
    record_sent(store, "m", "a@x.example", "Two")
    assert [e["subject"] for e in sent_log(store, "m")] == ["One", "Two"]


def test_the_agent_pipeline_cannot_send():
    from pathlib import Path
    for name in ("pipeline", "outreach", "handoff", "live", "report", "rules", "decide"):
        source = Path(f"clearance/{name}.py").read_text()
        assert "outbound" not in source and "smtplib" not in source


MAIL = "From: Real Sender <real@v.example>\nReply-To: other@evil.example\nSubject: Invoice\n\nHi"


def test_sender_comes_from_the_real_header_not_reply_to():
    assert detected_sender("mailbox", MAIL) == "real@v.example"


@pytest.mark.parametrize("origin", ["sample", "upload", "paste"])
def test_only_real_mailbox_mail_can_add_its_sender(origin):
    assert detected_sender(origin, MAIL) is None
    assert recipients_for(origin, MAIL, ["t@x.example"], True) == ["t@x.example"]


def test_sender_is_added_only_when_enabled():
    assert recipients_for("mailbox", MAIL, ["t@x.example"], True) == ["real@v.example", "t@x.example"]
    assert recipients_for("mailbox", MAIL, ["t@x.example"], False) == ["t@x.example"]
    assert recipients_for("mailbox", MAIL, [], False) == []


def test_sender_added_for_one_email_cannot_be_used_on_another():
    other = "From: B <b@v.example>\nSubject: x"
    allowed = recipients_for("mailbox", other, [], True)
    assert not send("real@v.example", allow=allowed).ok and FakeSmtp.sent == []


def test_a_mailbox_sender_can_be_replied_to_when_allowed():
    allowed = recipients_for("mailbox", MAIL, [], True)
    assert send("real@v.example", allow=allowed).ok
    assert FakeSmtp.sent[0][1] == ["real@v.example"]


@pytest.mark.parametrize("to,outcome,needed", [
    ("real@v.example", "HOLD", True),
    ("real@v.example", "ESCALATE", True),
    ("real@v.example", None, True),
    ("real@v.example", "APPROVE", False),
    ("t@x.example", "HOLD", False),
])
def test_held_or_unchecked_sender_needs_a_second_confirmation(to, outcome, needed):
    assert needs_hold_acknowledgement(to, "real@v.example", outcome) is needed


def test_reply_to_sender_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SEND_TO_SENDER", raising=False)
    assert not reply_to_sender_enabled()
    monkeypatch.setenv("SEND_TO_SENDER", "true")
    assert reply_to_sender_enabled()
