"""Sender detection on real-world threads, both stores, and the live cycle. No network."""
from datetime import date

import pytest

from clearance.extract import extract_regex
from clearance.live import sync_and_scan
from clearance.mailbox import MailItem, fetch_mailbox
from clearance.schemas import CaseResult, Claim
from clearance.store import SqliteStore, SupabaseStore
from tests.test_intake_mailbox import SETTINGS, FakeImap, make_eml


@pytest.mark.parametrize("text,email", [
    ("From: New Person <new@v.example>\nSubject: Re: Invoice\n\nOk.\n\nOn Mon, Jan 5, 2026 at 9:00 AM Old Person <old@v.example> wrote:\n> From: Old Person <old@v.example>\n> Total: $9.00", "new@v.example"),
    ("From: New Person <new@v.example>\nDate: Mon\nSubject: Re: Invoice\n\nOk\n\nFrom: Old Person <old@v.example>\nSent: Friday\nTo: x\nTotal: $9.00", "new@v.example"),
    ("From: New <new@v.example>\n\nOk\n\n-----Original Message-----\nFrom: Old <old@v.example>", "new@v.example"),
    ("From: Clerk <c@o.example>\nSubject: Fwd: pay\n\n> From: Real <real@v.example>", "real@v.example"),
    ("From: Clerk <c@o.example>\n\n---------- Forwarded message ---------\nFrom: Real <real@v.example>\nSubject: pay", "real@v.example"),
    ("From: Clerk <c@o.example>\n\n---------- Forwarded message ---------\nFrom: Real <real@v.example>\nDate: Mon\n\nHi\n\nOn Tue Old <old@v.example> wrote:\n> From: Old <old@v.example>", "real@v.example"),
])
def test_sender_is_right_for_replies_and_forwards(text, email):
    assert extract_regex(text).sender_email == email


def test_quoted_older_invoice_does_not_set_the_amount():
    text = "From: A <a@v.example>\nSubject: Re: invoice\n\nThanks!\n\nOn Mon, Jan 5 at 9:00 B <b@v.example> wrote:\n> Total: $9,000.00"
    assert extract_regex(text).amount is None


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, tables, name):
        self.tables, self.name, self.op, self.filters, self.row, self.ignore = tables, name, "select", [], None, False

    def select(self, *_):
        return self

    def in_(self, column, values):
        self.filters.append(lambda r: r[column] in values)
        return self

    def eq(self, column, value):
        self.filters.append(lambda r: r[column] == value)
        return self

    def delete(self):
        self.op = "delete"
        return self

    def upsert(self, row, on_conflict, ignore_duplicates=False):
        self.op, self.row, self.key, self.ignore = "upsert", row, on_conflict, ignore_duplicates
        return self

    def execute(self):
        rows = self.tables.setdefault(self.name, [])
        if self.op == "upsert":
            hit = next((r for r in rows if r[self.key] == self.row[self.key]), None)
            if hit and self.ignore:
                return FakeResponse([])
            if hit:
                rows.remove(hit)
            rows.append(self.row)
            return FakeResponse([self.row])
        matched = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "delete":
            for r in matched:
                rows.remove(r)
            if self.name == "mail":  # ON DELETE CASCADE
                ids = {r["id"] for r in matched}
                self.tables["verdict"] = [v for v in self.tables.get("verdict", []) if v["mail_id"] not in ids]
        return FakeResponse(matched)


class FakeSupabase:
    def __init__(self):
        self.tables = {}

    def table(self, name):
        return FakeQuery(self.tables, name)


@pytest.fixture(params=["sqlite", "supabase"])
def store(request, tmp_path):
    return SqliteStore(tmp_path / "t.db") if request.param == "sqlite" else SupabaseStore(FakeSupabase())


def mail(id="m1", origin="mailbox", received="2026-10-31T10:00:00+00:00"):
    return MailItem(id=id, sender="a@v.example", subject="Invoice", text="From: a@v.example", origin=origin, warnings=["w"], received=received)


def result(credits=2.0):
    return CaseResult(claim=Claim(vendor_name="V"), credits_used=credits)


def test_store_add_is_idempotent_and_round_trips(store):
    assert store.add(mail()) is True and store.add(mail()) is False
    item = store.items(["mailbox"])["m1"]
    assert item.warnings == ["w"] and item.received.startswith("2026") and item.origin == "mailbox"


def test_store_orders_newest_first_and_filters_origin(store):
    store.add(mail("old", received="2026-01-01T00:00:00+00:00"))
    store.add(mail("new", received="2026-10-01T00:00:00+00:00"))
    store.add(mail("s", origin="sample", received=""))
    assert list(store.items(["mailbox"])) == ["new", "old"]
    assert list(store.items(["mailbox", "sample"]))[-1] == "s"


def test_store_tracks_results_credits_and_unscanned(store):
    store.add(mail("a"))
    store.add(mail("b"))
    store.save_result("a", result(2.5))
    assert store.results()["a"].claim.vendor_name == "V"
    assert [i.id for i in store.unscanned(["mailbox"])] == ["b"]
    assert store.credits_on() == 2.5 and store.credits_on(date(2000, 1, 1)) == 0


def test_saving_again_does_not_double_charge_but_a_recheck_adds(store):
    store.add(mail("a"))
    store.save_result("a", result(2.0))
    store.save_result("a", result(2.0))
    assert store.credits_on() == 2.0
    store.recheck_result("a", result(1.5))
    assert store.credits_on() == 3.5


def test_store_sample_sync_and_reset(store):
    store.sync_samples([mail("s1", "sample"), mail("s2", "sample")])
    store.save_result("s1", result(0))
    store.sync_samples([mail("s1", "sample")])
    assert list(store.items(["sample"])) == ["s1"] and "s1" in store.results()
    store.reset_samples()
    assert store.results() == {}


def test_store_meta(store):
    assert store.meta("x", "d") == "d"
    store.set_meta("x", "1")
    store.set_meta("x", "2")
    assert store.meta("x") == "2"


def checker(credits=1.0, calls=None):
    def check(item):
        if calls is not None:
            calls.append(item.id)
        return result(credits)
    return check


def test_live_cycle_imports_scans_and_remembers_position(store):
    report = sync_and_scan(store, SETTINGS, checker(), True, 100, imap_class=FakeImap)
    assert report.added == 2 and report.scanned == 2 and report.changed and store.meta("last_uid") == "3"
    assert ("search", "ALL") in FakeImap.last.calls


def test_next_cycle_asks_only_for_new_mail_and_never_rescans(store):
    sync_and_scan(store, SETTINGS, checker(), True, 100, imap_class=FakeImap)
    calls = []
    report = sync_and_scan(store, SETTINGS, checker(calls=calls), True, 100, imap_class=FakeImap)
    assert ("search", "UID 4:*") in FakeImap.last.calls
    assert report.added == 0 and calls == [] and not report.changed


def test_stale_uid_is_not_fetched_again():
    items, error, newest = fetch_mailbox(SETTINGS, last_uid=3, imap_class=FakeImap)
    assert items == [] and error is None and newest == 3
    assert not any(c[0] == "fetch" for c in FakeImap.last.calls)


def test_daily_limit_stops_scanning_but_still_imports(store):
    report = sync_and_scan(store, SETTINGS, checker(credits=5), True, 5, imap_class=FakeImap)
    assert report.added == 2 and report.scanned == 1 and report.limit_reached
    assert len(store.unscanned(["mailbox"])) == 1


def test_auto_scan_off_spends_nothing(store):
    calls = []
    report = sync_and_scan(store, SETTINGS, checker(calls=calls), False, 100, imap_class=FakeImap)
    assert report.added == 2 and report.scanned == 0 and calls == []


def test_mailbox_failure_keeps_position_for_a_retry(store):
    store.set_meta("last_uid", "7")
    def boom(host, timeout=None):
        raise OSError("down")
    report = sync_and_scan(store, SETTINGS, checker(), True, 100, imap_class=boom)
    assert "reach the mail server" in report.error and store.meta("last_uid") == "7"


@pytest.mark.parametrize("line,last4", [
    ("New account number: 000123456789", "6789"),
    ("Account No. 50100998877665", "7665"),
    ("Account #: 12345678", "5678"),
    ("A/C No: 987654321", "4321"),
    ("new IBAN: DE89 3704 0044 0532 0130 00", "3000"),
    ("Subject: Bank Account Update - Invoice INV-2026-2081", None),
    ("Your account manager will call about order 12345678", None),
    ("Bank account details changed, invoice 20262081", None),
])
def test_bank_digits_need_a_real_account_label(line, last4):
    assert extract_regex(line).bank_last4 == last4
