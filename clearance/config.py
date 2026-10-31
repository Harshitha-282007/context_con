from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "fixtures"
OUTBOX_DIR = ROOT / "outbox"
OUTPUTS_DIR = ROOT / "outputs"

load_dotenv(ROOT / ".env")


def load_policy(path: Path | None = None) -> dict:
    with open(path or ROOT / "policy.yaml") as f:
        return yaml.safe_load(f)


def crustdata_api_key() -> str | None:
    return os.environ.get("CRUSTDATA_API_KEY") or None


def anthropic_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or None


def credit_cap() -> float:
    return float(os.environ.get("CLEARANCE_CREDIT_CAP") or 150)
