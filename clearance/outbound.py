"""Person-initiated email from the portal. The agent itself never calls this.

Sending is off unless SEND_ALLOWLIST lists recipients, and a recipient outside that list is
refused here rather than only hidden in the UI. Each send needs a person to confirm it.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

from .store import Store

ADDRESS = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")
MAX_BODY_CHARS = 20_000


@dataclass
class SendResult:
    ok: bool
    error: Optional[str] = None


def send_allowlist() -> list[str]:
    raw = os.environ.get("SEND_ALLOWLIST") or ""
    return [a.strip().lower() for a in raw.split(",") if ADDRESS.fullmatch(a.strip())]


def smtp_settings() -> Optional[dict]:
    user, password = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
    if not (user and password):
        return None
    return {"host": os.environ.get("SMTP_HOST") or "smtp.gmail.com", "port": int(os.environ.get("SMTP_PORT") or 465), "user": user, "password": password}


def send_mail(settings: Optional[dict], allowlist: list[str], to: str, subject: str, body: str, confirmed: bool, smtp_class=smtplib.SMTP_SSL) -> SendResult:
    if not settings:
        return SendResult(False, "Sending needs IMAP_USER and IMAP_PASSWORD in .env.")
    if not confirmed:
        return SendResult(False, "Confirm the recipient and message before sending.")
    if not ADDRESS.fullmatch(to or "") or to.lower() not in allowlist:
        return SendResult(False, "This recipient is not in SEND_ALLOWLIST, so the message was not sent.")
    if not subject.strip() or not body.strip():
        return SendResult(False, "Add a subject and a message.")
    if len(body) > MAX_BODY_CHARS:
        return SendResult(False, "This message is too long.")
    msg = EmailMessage()
    try:
        msg["From"], msg["To"], msg["Subject"] = settings["user"], to, subject
    except ValueError:  # Line breaks in a header could add recipients.
        return SendResult(False, "The subject contains characters that cannot be sent.")
    msg.set_content(body)
    try:
        with smtp_class(settings["host"], settings["port"], timeout=20) as server:
            server.login(settings["user"], settings["password"])
            server.send_message(msg, to_addrs=[to])
    except smtplib.SMTPAuthenticationError:
        return SendResult(False, "The mail server refused the login. Check IMAP_USER and IMAP_PASSWORD.")
    except (smtplib.SMTPException, OSError):  # Server text can echo account details, so it is not shown.
        return SendResult(False, "The message could not be sent. Check your connection and SMTP settings.")
    return SendResult(True)


def _key(mail_id: str) -> str:
    return f"sent:{mail_id}"


def sent_log(store: Store, mail_id: str) -> list[dict]:
    raw = store.meta(_key(mail_id))
    return json.loads(raw) if raw else []


def record_sent(store: Store, mail_id: str, to: str, subject: str) -> None:
    entries = sent_log(store, mail_id)
    entries.append({"to": to, "subject": subject, "at": datetime.now(timezone.utc).isoformat()})
    store.set_meta(_key(mail_id), json.dumps(entries))
