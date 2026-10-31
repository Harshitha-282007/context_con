"""M0 coverage probe. Reads live data, saves raw responses to fixtures/, prints a table.

Usage: .venv/bin/python scripts/probe.py [--cap 60] [--domains a.com b.com]
Vendor JSON is read loosely here on purpose: this script exists to discover shapes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clearance.config import FIXTURES_DIR, ROOT, crustdata_api_key
from clearance.crustdata import CrustdataClient

CANDIDATES = [
    "browserstack.com", "chargebee.com", "postman.com", "razorpay.com", "retool.com",
    "gusto.com", "mintlify.com", "serverobotics.com", "crustdata.com", "tomocredit.ai",
]
FINANCE_TITLES = ["finance", "accounts payable", "accounting", "controller", "CFO"]
ENRICH_FIELDS = ["basic_info", "headcount", "revenue", "web_traffic", "locations"]
PERSON_FIELDS = [
    "basic_profile.name", "basic_profile.current_title", "contact.has_business_email",
    "experience.employment_details.current",
]
CURRENT_ID = "experience.employment_details.current.company_id"
CURRENT_TITLE = "experience.employment_details.current.title"


def first_match(result):
    if result.ok and isinstance(result.data, list) and result.data:
        matches = result.data[0].get("matches") or []
        return matches[0] if matches else None
    return None


def probe_one(client: CrustdataClient, domain: str) -> dict:
    row = {"domain": domain}
    identify = client.post("/company/identify", {"domains": [domain], "exact_match": True})
    row["identify_status"] = identify.status
    match = first_match(identify)
    if not match:
        row["error"] = identify.error or "no_match"
        return row
    company_id = match["company_data"].get("crustdata_company_id")
    row.update(id=company_id, confidence=match.get("confidence_score"), name=match["company_data"].get("basic_info", {}).get("name"))

    enrich = client.post("/company/enrich", {"crustdata_company_ids": [company_id], "fields": ENRICH_FIELDS})
    row["enrich_status"] = enrich.status
    em = first_match(enrich)
    data = em["company_data"] if em else {}
    row["headcount"] = (data.get("headcount") or {}).get("total")
    row["revenue"] = ((data.get("revenue") or {}).get("estimated"))
    row["web_traffic_keys"] = list((data.get("web_traffic") or {}).keys())[:5]

    everyone = client.post("/person/search", {
        "filters": {"field": CURRENT_ID, "type": "=", "value": company_id},
        "limit": 1, "fields": ["basic_profile.name"],
    })
    row["search_status"] = everyone.status
    row["employees_in_graph"] = everyone.data.get("total_count") if everyone.ok and isinstance(everyone.data, dict) else None

    finance = client.post("/person/search", {
        "filters": {"op": "and", "conditions": [
            {"field": CURRENT_ID, "type": "=", "value": company_id},
            {"op": "or", "conditions": [{"field": CURRENT_TITLE, "type": "(.)", "value": t} for t in FINANCE_TITLES]},
        ]},
        "limit": 5, "fields": PERSON_FIELDS,
    })
    if finance.ok and isinstance(finance.data, dict):
        row["finance_total"] = finance.data.get("total_count")
        row["finance_sample"] = [
            ((p.get("basic_profile") or {}).get("current_title")) for p in finance.data.get("profiles", [])
        ]
    else:
        row["finance_error"] = finance.error
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=float, default=60)
    ap.add_argument("--domains", nargs="*", default=CANDIDATES)
    args = ap.parse_args()

    key = crustdata_api_key()
    if not key:
        print("CRUSTDATA_API_KEY is not set. Add it to .env.")
        return 2
    client = CrustdataClient(key, FIXTURES_DIR, credit_cap=args.cap)

    balance = client.get("/account/credits")
    print("credits:", balance.status, json.dumps(balance.data)[:400])
    endpoints = client.get("/account/endpoints")
    print("endpoints:", endpoints.status, "(saved to fixtures)" if endpoints.ok else endpoints.error)

    rows = [probe_one(client, d) for d in args.domains]
    out = ROOT / "fixtures" / "probe_summary.json"
    out.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    print(f"credits spent: {client.credits_spent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
