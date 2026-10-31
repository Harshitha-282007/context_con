"""M0 second probe: endpoint access, email lookup, name search, name collisions.

Prints shapes and statuses rather than people's details.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clearance.config import FIXTURES_DIR, crustdata_api_key
from clearance.crustdata import CrustdataClient

COLLISIONS = ["Apex Consulting", "Summit Solutions", "Meridian Services", "Atlas Logistics", "Pinnacle Group", "Northwind"]
RETOOL = 633593
CURRENT_ID = "experience.employment_details.current.company_id"


def main() -> int:
    c = CrustdataClient(crustdata_api_key(), FIXTURES_DIR, credit_cap=15)

    eps = c.get("/account/endpoints").data
    items = eps if isinstance(eps, list) else next(v for v in eps.values() if isinstance(v, list))
    print("== access")
    for e in items:
        if any(k in e["path"] for k in ("person", "watch", "/web/")) and "batch" not in e["path"]:
            print(f"{e['status']:9} {e['path']} base={e.get('base_credits')} rpm={e.get('effective_rate_limit_rpm')}")

    print("\n== /person/identify")
    r = c.post("/person/identify", {"emails": ["nobody.clearance.probe@retool.com"]})
    print("status", r.status, "error", r.error, "credits", r.credits, json.dumps(r.data)[:300])

    print("\n== name search at company: real name vs fictional")
    finance = c.post("/person/search", {
        "filters": {"op": "and", "conditions": [
            {"field": CURRENT_ID, "type": "=", "value": RETOOL},
            {"field": "experience.employment_details.current.title", "type": "(.)", "value": "Controller"},
        ]}, "limit": 1, "fields": ["basic_profile.name"],
    })
    real = (finance.data.get("profiles") or [{}])[0].get("basic_profile", {}).get("name") if finance.ok else None
    for label, name in (("real", real), ("fictional", "Zorvath Quillan")):
        if not name:
            continue
        s = c.post("/person/search", {
            "filters": {"op": "and", "conditions": [
                {"field": CURRENT_ID, "type": "=", "value": RETOOL},
                {"field": "basic_profile.name", "type": "(.)", "value": name},
            ]}, "limit": 5,
            "fields": ["basic_profile.name", "basic_profile.current_title", "contact.has_business_email", "experience.employment_details.current"],
        })
        print(label, "status", s.status, "total", (s.data or {}).get("total_count"), "credits", s.credits, s.error or "")

    print("\n== recently_changed_jobs access")
    s = c.post("/person/search", {
        "filters": {"field": CURRENT_ID, "type": "=", "value": RETOOL}, "limit": 1,
        "fields": ["experience.recently_changed_jobs"],
    })
    print("status", s.status, s.error or "", json.dumps(s.data)[:200])

    print("\n== name collisions (identify is free)")
    for name in COLLISIONS:
        r = c.post("/company/identify", {"names": [name]})
        matches = r.data[0].get("matches", []) if r.ok and r.data else []
        print(name, "->", len(matches), "matches")
        for m in matches[:4]:
            b = m["company_data"].get("basic_info", {})
            print("   ", m.get("confidence_score"), b.get("name"), b.get("primary_domain"), b.get("employee_count_range"))

    print("\ncredits spent:", round(c.credits_spent, 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
