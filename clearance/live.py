"""One live-inbox cycle: fetch new mail, store it, then scan within the daily credit limit."""
from __future__ import annotations

import imaplib
from dataclasses import dataclass
from typing import Callable, Optional

from .mailbox import MailItem, fetch_mailbox
from .schemas import CaseResult
from .store import Store

SCAN_BATCH = 5  # Per cycle, so a flood of mail cannot stall the page or the credit budget.


@dataclass
class SyncReport:
    added: int = 0
    scanned: int = 0
    error: Optional[str] = None
    limit_reached: bool = False
    failed: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.scanned)


def sync_and_scan(
    store: Store, settings: dict, check: Callable[[MailItem], CaseResult], auto_scan: bool, daily_limit: float,
    imap_class=imaplib.IMAP4_SSL,
) -> SyncReport:
    report = SyncReport()
    last_uid = int(store.meta("last_uid", "0") or 0)
    items, report.error, newest = fetch_mailbox(settings, last_uid=last_uid, imap_class=imap_class)
    for item in reversed(items):  # Oldest first, so newer mail sorts above it.
        report.added += store.add(item)
    if newest > last_uid:
        store.set_meta("last_uid", str(newest))
    if auto_scan:
        for item in store.unscanned(("mailbox",))[:SCAN_BATCH]:
            if store.credits_on() >= daily_limit:
                report.limit_reached = True
                break
            try:
                store.save_result(item.id, check(item))
                report.scanned += 1
            except Exception:  # One bad email must not stop the rest. It stays unscanned for a retry.
                report.failed += 1
    return report
