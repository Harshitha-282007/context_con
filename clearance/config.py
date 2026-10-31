from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "fixtures"
PRIVATE_FIXTURES_DIR = ROOT / ".private_fixtures"  # git-ignored: uploads, pastes and real mail
OUTBOX_DIR = ROOT / "outbox"
OUTPUTS_DIR = ROOT / "outputs"

load_dotenv(ROOT / ".env")


def data_dir() -> Path:
    return Path(os.environ.get("CLEARANCE_DATA_DIR") or ROOT / "data")


def supabase_settings() -> dict | None:
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    return {"url": url, "key": key} if url and key else None


def daily_credit_limit() -> float:
    return float(os.environ.get("CLEARANCE_DAILY_CREDIT_LIMIT") or 100)


def load_policy(path: Path | None = None) -> dict:
    with open(path or ROOT / "policy.yaml") as f:
        return yaml.safe_load(f)


def crustdata_api_key() -> str | None:
    return os.environ.get("CRUSTDATA_API_KEY") or None


def anthropic_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or None


def credit_cap() -> float:
    return float(os.environ.get("CLEARANCE_CREDIT_CAP") or 150)
