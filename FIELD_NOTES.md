# Crustdata field notes

Status: **from the docs only. Nothing below is confirmed by a real call yet.** The probe (`scripts/probe.py`) confirms or corrects these.

## Answers to PRD §19 from the docs

| Question | Answer from docs |
|---|---|
| Email lookup body | **Differs from the PRD.** `/person/enrich` accepts only `professional_network_profile_urls`. Reverse lookup is `POST /person/identify` with `{"emails": [...]}` (`business_emails` returns 400). Enabled per account, so it may return 403. |
| Credit costs | Company Identify free; Company Enrich 2 per record; Person Identify 1 per match (unmatched free); Person Search 0.03 per result plus premium groups (`experience` is premium, price in `/account/endpoints`); Person Enrich 1+. Every response carries `X-Credits-Used`. |
| Watcher enabled? | Unknown. Check the `GET /account/endpoints` response (free). |

## Documented shapes

- **Company Identify / Enrich:** top-level array of `{matched_on, match_type, matches: [{confidence_score, company_data}]}`. No match is 200 with `matches: []`; also handle 404. Enrich `fields` defaults to `basic_info` only.
- **Enrich sections:** `headcount.total`, `headcount.growth_percent.six_months`, `revenue.estimated.{lower_bound_usd, upper_bound_usd}`, `web_traffic` (per-domain `monthly_visitors`, exact path to confirm), `locations.headquarters`, `basic_info.{primary_domain, all_domains}`.
- **Person Search:** `{filters, fields, sorts, limit, cursor}`, returning `total_count` and `next_cursor`. Employer filter: `experience.employment_details.current.company_id`. Operators include `=`, `in`, `(.)` (all words), `[.]` (phrase); there is no `contains`. `=>` and `=<`, not `>=`.
- **Person Identify email matching caveat:** an address not on file can match *by the name in the address at the company that owns the domain*, with `confidence_score` 0.5–1.0. A low score is not proof of identity; treat it accordingly in `people.py`.
- **Access-gated field:** `experience.recently_changed_jobs` returns 403 in `fields` without access, so request it separately and treat denial as missing data.
