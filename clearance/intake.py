"""Turns uploaded files and raw emails into packet text. All input is untrusted.

PDFs are read for their text layer only: nothing in them is executed or rendered, and
scanned PDFs return a clear warning because there is no OCR.
"""
from __future__ import annotations

import email
import io
from dataclasses import dataclass, field
from email import policy as email_policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from datetime import timezone
from html.parser import HTMLParser
from pathlib import PurePath

from pypdf import PdfReader
from pypdf.errors import PyPdfError

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_TEXT_CHARS = 60_000
MIN_PDF_TEXT_CHARS = 20
TEXT_TYPES = {".txt", ".md", ".text"}


@dataclass
class Intake:
    text: str = ""
    warnings: list[str] = field(default_factory=list)
    sender: str = ""
    subject: str = ""
    received: str = ""


class _TextOnly(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BLOCK = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextOnly()
    try:
        parser.feed(html)
    except (ValueError, AssertionError):
        return ""
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def pdf_to_text(data: bytes) -> Intake:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return Intake(warnings=["This PDF is password-protected, so it could not be read."])
        pages = reader.pages[:MAX_PDF_PAGES]
        text = "\n".join(page.extract_text() or "" for page in pages).strip()
    except (PyPdfError, ValueError, KeyError, TypeError, IndexError, RecursionError, OSError, AttributeError):
        return Intake(warnings=["This PDF could not be read. The file may be damaged."])
    warnings = []
    if len(reader.pages) > MAX_PDF_PAGES:
        warnings.append(f"Only the first {MAX_PDF_PAGES} pages were read.")
    if len(text) < MIN_PDF_TEXT_CHARS:
        warnings.append("This PDF has almost no readable text, so it is probably a scan. OCR is not supported.")
    return Intake(text=text, warnings=warnings)


def eml_to_text(data: bytes) -> Intake:
    """Keeps the headers rules need, the body and PDF attachment text. Links are never followed."""
    try:
        msg = email.message_from_bytes(data, policy=email_policy.default)
    except (ValueError, TypeError, IndexError):
        return Intake(warnings=["This email could not be read."])
    return message_to_intake(msg)


def _safe_header(msg: EmailMessage, name: str) -> str:
    try:
        return " ".join(str(msg.get(name, "")).split())
    except (ValueError, TypeError, IndexError, AttributeError):
        return ""


def _content(part: EmailMessage) -> str:
    try:
        content = part.get_content()
    except (LookupError, ValueError, KeyError, AttributeError):
        return ""
    return content if isinstance(content, str) else ""


def _received(msg: EmailMessage) -> str:
    try:
        return parsedate_to_datetime(_safe_header(msg, "Date")).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, IndexError, OverflowError):
        return ""


def message_to_intake(msg: EmailMessage) -> Intake:
    sender, subject = _safe_header(msg, "From"), _safe_header(msg, "Subject")
    plain, html, warnings, attachments = [], [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        kind = part.get_content_type()
        filename = part.get_filename() or ""
        if filename or part.get_content_disposition() == "attachment":
            if kind == "application/pdf" or filename.lower().endswith(".pdf"):
                payload = part.get_payload(decode=True) or b""
                if len(payload) > MAX_FILE_BYTES:
                    warnings.append("A PDF attachment was too large to read.")
                    continue
                pdf = pdf_to_text(payload)
                warnings += pdf.warnings
                if pdf.text:
                    attachments.append(f"--- Attachment ---\n{pdf.text}")
            else:
                warnings.append("A non-PDF attachment was skipped.")
        elif kind == "text/plain":
            plain.append(_content(part))
        elif kind == "text/html":
            html.append(html_to_text(_content(part)))
    body = "\n".join(p for p in plain if p.strip()) or "\n".join(h for h in html if h.strip())
    head = [f"{label}: {value}" for label, value in (("From", sender), ("Subject", subject)) if value]
    text = "\n".join(head) + "\n\n" + body + ("\n\n" + "\n\n".join(attachments) if attachments else "")
    return Intake(text=text.strip()[:MAX_TEXT_CHARS], warnings=warnings, sender=sender, subject=subject, received=_received(msg))


def read_upload(filename: str, data: bytes) -> Intake:
    if len(data) > MAX_FILE_BYTES:
        return Intake(warnings=["This file is larger than the 10 MB limit."])
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".pdf":
        result = pdf_to_text(data)
    elif suffix == ".eml":
        result = eml_to_text(data)
    elif suffix in TEXT_TYPES:
        result = Intake(text=data.decode("utf-8", errors="replace"))
    else:
        return Intake(warnings=[f"{suffix or 'This file type'} is not supported. Use PDF, TXT or EML."])
    result.text = result.text[:MAX_TEXT_CHARS]
    return result
