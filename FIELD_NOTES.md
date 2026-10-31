# Crustdata field notes

Confirmed by real calls on 31 Oct 2026 unless marked. Saved responses are in `fixtures/`, with people pseudonymized and unused profile fields removed by `scripts/anonymize_fixtures.py`, so they no longer show every field described below.

## Key access (`GET /account/endpoints`, free)

| Endpoint | Status | Effect on Clearance |
|---|---|---|
| `/company/identify`, `/company/enrich` | enabled | Used as planned |
| `/person/search` | enabled | Sender check and callback contacts |
| `/person/identify` | **403** | No email reverse lookup; see R2 in the README |
| `/person/contact/enrich` | disabled | No addresses; drafts use `.invalid` and defer to vendor records |
| Live person search and enrich | disabled | Not used |
| `/watch/*` | not listed | Watcher (P2) cut |

## Coverage probe (10 vendors): all usable

| Domain | Headcount | Employees in graph | Finance staff found |
|---|---|---|---|
| browserstack.com | 1717 | 1598 | 8 |
| chargebee.com | 1305 | 1172 | 17 |
| postman.com | 3583 | 2217 | 15 |
| razorpay.com | 4027 | 3592 | 71 |
| retool.com | 413 | 397 | 8 |
| gusto.com | 4882 | 3679 | 52 |
| mintlify.com | 86 | 85 | 1 |
| serverobotics.com | 454 | 429 | 6 |
| crustdata.com | 47 | 54 | 1 |
| tomocredit.ai | 21 | 21 | 2 |

All resolved by domain with confidence 1.0. **Decision:** no shift to an "UNVERIFIABLE-first" framing. Demo vendors are Retool (A), Serve Robotics (B, has an AP coordinator to call), Atlas Logistics QA (C) and the name "Apex Consulting" (D, 25 matches, identify returns at most 25).

## Real shapes (differ from or add to the docs)

- **Revenue** is at `revenue.estimated.{lower_bound_usd, upper_bound_usd}`, with a `timeseries` alongside. Bands are wide (500M to 1B is common) and sometimes 0.
- **Web traffic** is at `web_traffic.domain_traffic.<domain>.monthly_visitors`. A value of `0.0` appears for small companies and may mean untracked.
- **Person search** returns `profiles`, `next_cursor`, `total_count`, `total_count_relation`, `remarks`.
- **`function_category`** is often an empty string (18 of 50 roles sampled), so R4 also reads title and seniority. `seniority_level` can mislead: one "Global Corporate Controller" is labelled "Entry Level".
- **`experience.recently_changed_jobs`** is accessible on this key, though the docs call it gated.
- **Name-based identify** often gives many candidates at confidence 1.0, so confidence alone cannot disambiguate.

## Costs seen

Identify is free. Enrich is 2 credits. Person search with `experience` is 0.23 per result (0.13 with name only at limit 1); a search with 0 results is free. A full run costs about 3.5 credits. Exploration used about 45 credits in total.
