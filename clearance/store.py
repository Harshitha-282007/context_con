"""Storage for mail and verdicts, so they survive a refresh or restart.

Uses Supabase when SUPABASE_URL and SUPABASE_SERVICE_KEY are set, otherwise a local SQLite
file. Either can hold real email: the SQLite file is git-ignored, and the Supabase tables
have row level security on with no public policies (see supabase/schema.sql).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .config import data_dir, supabase_settings
from .mailbox import MailItem
from .schemas import CaseResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail (
    id TEXT PRIMARY KEY, origin TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL,
    text TEXT NOT NULL, warnings TEXT NOT NULL, received TEXT NOT NULL, added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verdict (
    mail_id TEXT PRIMARY KEY, result TEXT NOT NULL, credits REAL NOT NULL, scanned_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _item(row: dict) -> MailItem:
    warnings = row["warnings"]
    return MailItem(
        id=row["id"], sender=row["sender"], subject=row["subject"], text=row["text"], origin=row["origin"],
        warnings=json.loads(warnings) if isinstance(warnings, str) else warnings, received=row["received"],
    )


def _ordered(rows: list[dict]) -> dict[str, MailItem]:
    """Real mail first, newest first; samples keep their file order."""
    real = sorted((r for r in rows if r["origin"] != "sample"), key=lambda r: r["received"] or r["added_at"], reverse=True)
    samples = sorted((r for r in rows if r["origin"] == "sample"), key=lambda r: r["added_at"])
    return {r["id"]: _item(r) for r in real + samples}


class Store:
    """Shared logic. Subclasses implement the row operations."""
    backend = ""

    def add(self, item: MailItem) -> bool:
        raise NotImplementedError

    def items(self, origins: Iterable[str]) -> dict[str, MailItem]:
        raise NotImplementedError

    def sync_samples(self, samples: list[MailItem]) -> None:
        raise NotImplementedError

    def reset_samples(self) -> None:
        raise NotImplementedError

    def save_result(self, mail_id: str, result: CaseResult) -> None:
        """Saving again, such as after a handoff action, keeps the original charge and date."""
        raise NotImplementedError

    def recheck_result(self, mail_id: str, result: CaseResult) -> None:
        """A fresh check adds its own charge to what the email has already cost."""
        raise NotImplementedError

    def results(self) -> dict[str, CaseResult]:
        raise NotImplementedError

    def credits_on(self, day: Optional[date] = None) -> float:
        raise NotImplementedError

    def meta(self, key: str, default: str = "") -> str:
        raise NotImplementedError

    def set_meta(self, key: str, value: str) -> None:
        raise NotImplementedError

    def unscanned(self, origins: Iterable[str]) -> list[MailItem]:
        done = self.results()
        return [item for item in self.items(origins).values() if item.id not in done]


def _row(item: MailItem) -> dict:
    return {
        "id": item.id, "origin": item.origin, "sender": item.sender, "subject": item.subject, "text": item.text,
        "warnings": item.warnings, "received": item.received, "added_at": datetime.now(timezone.utc).isoformat(),
    }


class SqliteStore(Store):
    backend = "Local file"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def add(self, item: MailItem) -> bool:
        row = _row(item)
        row["warnings"] = json.dumps(row["warnings"])
        with closing(self._connect()) as db, db:
            cur = db.execute("INSERT OR IGNORE INTO mail VALUES (:id, :origin, :sender, :subject, :text, :warnings, :received, :added_at)", row)
            return cur.rowcount > 0

    def items(self, origins: Iterable[str]) -> dict[str, MailItem]:
        origins = list(origins)
        marks = ",".join("?" * len(origins))
        with closing(self._connect()) as db:
            rows = db.execute(f"SELECT * FROM mail WHERE origin IN ({marks}) ORDER BY rowid", origins).fetchall()
        return _ordered([dict(r) for r in rows])

    def sync_samples(self, samples: list[MailItem]) -> None:
        current = [s.id for s in samples]
        marks = ",".join("?" * len(current)) or "''"
        with closing(self._connect()) as db, db:
            db.execute(f"DELETE FROM verdict WHERE mail_id IN (SELECT id FROM mail WHERE origin='sample' AND id NOT IN ({marks}))", current)
            db.execute(f"DELETE FROM mail WHERE origin='sample' AND id NOT IN ({marks})", current)
        for sample in samples:
            self.add(sample)

    def reset_samples(self) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM verdict WHERE mail_id IN (SELECT id FROM mail WHERE origin='sample')")

    def _write(self, mail_id: str, result: CaseResult, add_charge: bool) -> None:
        with closing(self._connect()) as db, db:
            prior = db.execute("SELECT credits, scanned_on FROM verdict WHERE mail_id=?", (mail_id,)).fetchone()
            if prior and not add_charge:
                credits, day = prior["credits"], prior["scanned_on"]
            else:
                credits, day = (prior["credits"] if prior else 0) + result.credits_used, date.today().isoformat()
            db.execute("INSERT OR REPLACE INTO verdict VALUES (?, ?, ?, ?)", (mail_id, result.model_dump_json(), credits, day))

    def save_result(self, mail_id: str, result: CaseResult) -> None:
        self._write(mail_id, result, add_charge=False)

    def recheck_result(self, mail_id: str, result: CaseResult) -> None:
        self._write(mail_id, result, add_charge=True)

    def results(self) -> dict[str, CaseResult]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT mail_id, result FROM verdict").fetchall()
        return {r["mail_id"]: CaseResult.model_validate_json(r["result"]) for r in rows}

    def credits_on(self, day: Optional[date] = None) -> float:
        with closing(self._connect()) as db:
            row = db.execute("SELECT COALESCE(SUM(credits), 0) AS total FROM verdict WHERE scanned_on=?", ((day or date.today()).isoformat(),)).fetchone()
        return float(row["total"])

    def meta(self, key: str, default: str = "") -> str:
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


class SupabaseStore(Store):
    """Server-side only: the service key bypasses row level security, so it must never reach a browser."""
    backend = "Supabase"

    def __init__(self, client):
        self.db = client

    def add(self, item: MailItem) -> bool:
        res = self.db.table("mail").upsert(_row(item), on_conflict="id", ignore_duplicates=True).execute()
        return bool(res.data)

    def items(self, origins: Iterable[str]) -> dict[str, MailItem]:
        return _ordered(self.db.table("mail").select("*").in_("origin", list(origins)).execute().data or [])

    def sync_samples(self, samples: list[MailItem]) -> None:
        current = {s.id for s in samples}
        existing = {r["id"] for r in self.db.table("mail").select("id").eq("origin", "sample").execute().data or []}
        stale = list(existing - current)
        if stale:  # The verdict rows go too, through ON DELETE CASCADE.
            self.db.table("mail").delete().in_("id", stale).execute()
        for sample in samples:
            if sample.id not in existing:
                self.add(sample)

    def reset_samples(self) -> None:
        ids = [r["id"] for r in self.db.table("mail").select("id").eq("origin", "sample").execute().data or []]
        if ids:
            self.db.table("verdict").delete().in_("mail_id", ids).execute()

    def _write(self, mail_id: str, result: CaseResult, add_charge: bool) -> None:
        rows = self.db.table("verdict").select("credits, scanned_on").eq("mail_id", mail_id).execute().data or []
        prior = rows[0] if rows else None
        if prior and not add_charge:
            credits, day = prior["credits"], prior["scanned_on"]
        else:
            credits, day = float(prior["credits"] if prior else 0) + result.credits_used, date.today().isoformat()
        self.db.table("verdict").upsert(
            {"mail_id": mail_id, "result": result.model_dump(mode="json"), "credits": credits, "scanned_on": day}, on_conflict="mail_id",
        ).execute()

    def save_result(self, mail_id: str, result: CaseResult) -> None:
        self._write(mail_id, result, add_charge=False)

    def recheck_result(self, mail_id: str, result: CaseResult) -> None:
        self._write(mail_id, result, add_charge=True)

    def results(self) -> dict[str, CaseResult]:
        rows = self.db.table("verdict").select("mail_id, result").execute().data or []
        return {r["mail_id"]: CaseResult.model_validate(r["result"]) for r in rows}

    def credits_on(self, day: Optional[date] = None) -> float:
        rows = self.db.table("verdict").select("credits").eq("scanned_on", (day or date.today()).isoformat()).execute().data or []
        return float(sum(float(r["credits"]) for r in rows))

    def meta(self, key: str, default: str = "") -> str:
        rows = self.db.table("meta").select("value").eq("key", key).execute().data or []
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.table("meta").upsert({"key": key, "value": value}, on_conflict="key").execute()


def make_store() -> Store:
    settings = supabase_settings()
    if settings:
        from supabase import create_client
        return SupabaseStore(create_client(settings["url"], settings["key"]))
    return SqliteStore(data_dir() / "clearance.db")
