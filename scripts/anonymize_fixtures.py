"""One-off: pseudonymize people in saved /person/* responses so fixtures can be public.

Company data stays real. People keep their real role data, under made-up names and IDs,
and every field the app does not read is dropped. Files are re-keyed because the cache
key covers the request, which can contain a name. The old-to-new name mapping is never written.

Usage: python scripts/anonymize_fixtures.py <file listing real names, written by this script>
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clearance.crustdata import cache_key

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
ROLE_FIELDS = {"title", "function_category", "seniority_level", "start_date", "end_date", "crustdata_company_id", "is_default", "business_email_verified"}
FIRST = ["Alex", "Blair", "Casey", "Drew", "Emery", "Finley", "Gray", "Harper", "Indigo", "Jordan", "Kai", "Logan", "Marlo", "Nico", "Oakley", "Parker", "Quinn", "Reese", "Sage", "Tatum"]
SYLLABLES = ["bar", "cor", "dun", "fel", "gor", "hal", "jen", "kor", "lum", "mor", "nel", "pel", "quor", "ros", "sen", "tor", "vel", "wen", "yor", "zan"]


def fake_name(real: str, used: set[str]) -> str:
    salt = 0
    while True:
        h = hashlib.sha256(f"{real}|{salt}".encode()).digest()
        surname = (SYLLABLES[h[0] % 20] + SYLLABLES[h[1] % 20] + SYLLABLES[h[2] % 20]).capitalize()
        name = f"{FIRST[h[3] % 20]} {surname}"
        if surname not in {u.split()[-1] for u in used}:
            used.add(name)
            return name
        salt += 1


def slug(name: str) -> str:
    return ".".join(re.findall(r"[a-z]+", name.lower()))


def tokens(name: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", name)


def prune_profile(p: dict, person_ids: dict) -> dict:
    exp = p.get("experience") or {}
    details = exp.get("employment_details") or {}
    out: dict = {"crustdata_person_id": person_ids.setdefault(p.get("crustdata_person_id"), 90000000 + len(person_ids))}
    basic = p.get("basic_profile") or {}
    out["basic_profile"] = {k: basic[k] for k in ("name", "current_title") if k in basic}
    if "contact" in p:
        out["contact"] = {"has_business_email": (p["contact"] or {}).get("has_business_email")}
    kept: dict = {}
    if "recently_changed_jobs" in exp:
        kept["recently_changed_jobs"] = exp["recently_changed_jobs"]
    roles = {}
    for key in ("current", "past"):
        if key in details:
            roles[key] = [{k: v for k, v in r.items() if k in ROLE_FIELDS} for r in details[key] or [] if isinstance(r, dict)]
    if roles:
        kept["employment_details"] = roles
    if kept:
        out["experience"] = kept
    return out


def main() -> int:
    names_out = Path(sys.argv[1])
    paths = sorted(p for p in FIXTURES.glob("*.json") if p.name != "probe_summary.json")
    records = {p: json.loads(p.read_text()) for p in paths}

    real_names: set[str] = set()
    for rec in records.values():
        if rec.get("endpoint") == "/person/search" and isinstance(rec.get("response"), dict):
            for p in rec["response"].get("profiles") or []:
                name = (p.get("basic_profile") or {}).get("name")
                if name:
                    real_names.add(name)

    used: set[str] = set()
    mapping = {n: fake_name(n, used) for n in sorted(real_names)}
    surnames = {tokens(n)[-1].lower(): tokens(f)[-1] for n, f in mapping.items() if tokens(n)}
    ordered = sorted(mapping, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(n) for n in ordered), re.I) if ordered else None
    lookup = {n.lower(): f for n, f in mapping.items()}
    slugs = {slug(n): slug(f) for n, f in mapping.items()}
    person_ids: dict = {}

    def swap(text: str) -> str:
        return pattern.sub(lambda m: lookup[m.group(0).lower()], text) if pattern else text

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return swap(node) if isinstance(node, str) else node

    def fix_request(body):
        def visit(node):
            if isinstance(node, dict):
                if node.get("field") == "basic_profile.name" and isinstance(node.get("value"), str):
                    v = swap(node["value"])
                    node["value"] = surnames.get(v.lower(), v)
                for v in node.values():
                    visit(v)
            elif isinstance(node, list):
                for v in node:
                    visit(v)
        visit(body)
        if isinstance(body.get("emails"), list):
            body["emails"] = [
                slugs.get(e.split("@")[0], e.split("@")[0]) + "@" + e.split("@")[1] if "@" in e else e
                for e in body["emails"]
            ]
        return body

    changed = 0
    for path, rec in records.items():
        if not str(rec.get("endpoint", "")).startswith("/person/"):
            continue
        response = rec.get("response")
        if rec["endpoint"] == "/person/search" and isinstance(response, dict):
            rec["response"] = {
                "profiles": [prune_profile(p, person_ids) for p in response.get("profiles") or []],
                "total_count": response.get("total_count"),
            }
        rec["response"] = walk(rec["response"])
        rec["request"] = fix_request(rec["request"])
        path.unlink()
        new_key = cache_key(rec["method"], rec["endpoint"], rec["request"])
        (FIXTURES / f"{new_key}.json").write_text(json.dumps(rec, indent=1, sort_keys=True, ensure_ascii=False))
        changed += 1

    names_out.write_text("\n".join(sorted(real_names)) + "\n")
    print(f"people pseudonymized: {len(mapping)}; files rewritten: {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
