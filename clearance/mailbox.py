"""Inbox sources: the sample inbox, uploads and a real mailbox.

The IMAP connection is read-only by construction: the folder is opened read-only and
messages are fetched with BODY.PEEK, so nothing is marked read, moved or deleted. This
project has no code that sends mail. Credentials come only from .env.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import os
import re
import socket
from dataclasses import dataclass, field
from email import policy as email_policy
from pathlib import Path

from .extract import sender_line
from .intake import message_to_intake

PAYMENT_WORDS = ("invoice", "payment", "remit", "bank details", "bank account", "wire", "purchase order", "amount due", "overdue", "receipt", "billing")
FETCH_LIMIT = 200


@dataclass
class MailItem:
    id: str
    sender: str
    subject: str
    text: str
    origin: str  # "sample", "upload" or "mailbox"
    warnings: list[str] = field(default_factory=list)
    received: str = ""


def _item_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8", "replace")).hexdigest()[:12]


def _subject(text: str) -> str:
    match = re.search(r"^[ \t>*]*subject[ \t]*:[ \t]*(.+?)[ \t]*$", text, re.I | re.M)
    return match.group(1) if match else ""


def item_from_text(text: str, origin: str, label: str = "", warnings: list[str] | None = None) -> MailItem:
    sender = sender_line(text)
    return MailItem(
        id=_item_id(origin, label, text), sender=sender or "Unknown sender",
        subject=_subject(text) or label or "(no subject)", text=text, origin=origin, warnings=warnings or [],
    )


def sample_inbox(packets_dir: Path) -> list[MailItem]:
    return [item_from_text(p.read_text(), "sample") for p in sorted(packets_dir.glob("*.txt"))]


def looks_payment_related(subject: str, text: str) -> bool:
    haystack = f"{subject}\n{text}".lower()
    return any(word in haystack for word in PAYMENT_WORDS)


def mailbox_settings() -> dict | None:
    host, user, password = (os.environ.get(k) for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"))
    if not (host and user and password):
        return None
    return {"host": host, "user": user, "password": password, "folder": os.environ.get("IMAP_FOLDER") or "INBOX"}


def fetch_mailbox(
    settings: dict, limit: int = 25, last_uid: int = 0, imap_class=imaplib.IMAP4_SSL,
) -> tuple[list[MailItem], str | None, int]:
    """New payment-related mail, newest first, an error to show, and the newest UID seen.

    Never raises for mail or login failures. The first sync looks back over FETCH_LIMIT messages.
    """
    conn = None
    try:
        conn = imap_class(settings["host"], timeout=20)
        conn.login(settings["user"], settings["password"])
        status, _ = conn.select(settings["folder"], readonly=True)
        if status != "OK":
            return [], f"Could not open the folder {settings['folder']}.", last_uid
        status, data = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*" if last_uid else "ALL")
        if status != "OK" or not data or not data[0]:
            return [], None, last_uid
        # "n:*" always returns the newest message, even when it is older than n.
        uids = [u for u in data[0].split() if int(u) > last_uid]
        newest = max((int(u) for u in uids), default=last_uid)
        items: list[MailItem] = []
        for uid in reversed(uids[-FETCH_LIMIT:]):
            status, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None) if status == "OK" else None
            if not raw:
                continue
            intake = message_to_intake(email.message_from_bytes(raw, policy=email_policy.default))
            if not looks_payment_related(intake.subject, intake.text):
                continue
            items.append(MailItem(
                id=_item_id("mailbox", settings["folder"], str(int(uid))), sender=intake.sender or "Unknown sender",  # Identical re-sends stay separate.
                subject=intake.subject or "(no subject)", text=intake.text, origin="mailbox", warnings=intake.warnings,
                received=intake.received,
            ))
            if len(items) >= limit:
                break
        return items, None, newest
    except imaplib.IMAP4.error:
        # The server's text can echo account details, so it is not shown.
        return [], "The mail server refused the login or request. Check IMAP_USER and IMAP_PASSWORD; Gmail needs an app password.", last_uid
    except (OSError, socket.timeout, imaplib.IMAP4.abort):
        return [], "Could not reach the mail server. Check IMAP_HOST and your connection.", last_uid
    finally:
        if conn is not None:
            try:
                conn.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
