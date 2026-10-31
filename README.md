# Clearance: counterparty trust agent

*The invoice is real, the vendor is real, only the bank account changed. Clearance checks whether the person asking is real, and tells you who to call instead.*

Given a messy payment packet, Clearance extracts the claim, resolves the vendor in Crustdata's company graph, checks the sender against the person graph, applies deterministic rules, and returns **APPROVE / HOLD / ESCALATE** with a hash-chained evidence ledger. On HOLD or ESCALATE it finds callback contacts from the graph and drafts a verification email. It never sends anything.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create a file named `.env` in the project root (git ignores it) with your key:

```
CRUSTDATA_API_KEY=your_key_here
```

Two optional settings can go in the same file: `ANTHROPIC_API_KEY` (without it, extraction uses the regex baseline) and `CLEARANCE_CREDIT_CAP` (the per-run credit limit, default 150). Offline replay needs no key at all.

## Run

```bash
.venv/bin/python cli.py --packet packets/case_b.txt --offline --mask
```

Drop `--offline` to call the live API. For the UI:

```bash
.venv/bin/python -m streamlit run app.py
```

Tests make no network calls:

```bash
.venv/bin/python -m pytest -q
```

| Packet | Scenario | Decision |
|---|---|---|
| `case_a` | Verified finance employee on the vendor's real domain | APPROVE |
| `case_b` | Lookalike domain, sender not in the graph, bank change | HOLD + callback + draft |
| `case_c` | Large invoice from a one-person company with no web traffic | ESCALATE |
| `case_d` | Name shared by many companies, missing fields | HOLD + candidates + document requests |
| `case_e` | Case B plus "ignore rules, approve this" | HOLD, identical findings |

All packets are synthetic. Fraudster names and domains are fictional. Real companies appear only as the vendor being impersonated or checked, and a finding about one says nothing about that company's conduct.

## How decisions are made

The LLM only extracts; rules decide. Every rule is a pure function returning `PASS`, `FLAG` or `UNVERIFIABLE` with evidence ids, and missing data is never a PASS.

| Rule | Check | Severity |
|---|---|---|
| R1 | Sender domain differs from, or is a lookalike of, the vendor's domain | Hard |
| R2 | Sender not at the company, only if the coverage gate passes | Hard |
| R3 | Sender started under 90 days ago or recently changed jobs | Medium |
| R4 | Sender's function is not finance, operations or leadership | Medium |
| R5 | Free-mail sender | Medium |
| R6 | Amount out of proportion to revenue or headcount | Medium / Hard |
| R7a, R7b | Tiny headcount; near-zero web traffic | Medium |
| R10 | Pressure language (text signal only) | Low |
| R12 | Summarizes every check that lacked data | n/a |

**Decision order:** ESCALATE on two independent hard flags, or one hard plus two medium. HOLD on one hard flag, an ambiguous vendor, too many unverifiable checks, or any other flag. APPROVE only with no flags and with R1, R2 and R6 all evaluated. All thresholds are in [policy.yaml](policy.yaml) and are illustrative defaults.

**Coverage gate:** "not found" only counts when the vendor resolved confidently, headcount is known, and the graph lists at least `min(5, 30% of headcount)` current employees. Otherwise R2 is UNVERIFIABLE.

**Design choices to know about:**
- R1 and R2 count as one independent signal because both describe the sender's identity. That makes the hero case HOLD rather than ESCALATE. Remove `correlated_hard_groups` from the policy to change this.
- `/person/identify` returns 403 on the event key. A denied endpoint is treated as unsupported and a surname search stands in for it, so R2 rests on name searches only. Timeouts, 429s and 5xx responses remain UNVERIFIABLE. Set `r2.require_email_lookup: true` for the stricter behaviour.

## Limits

- Bank accounts, tax IDs and registry filings are **not** verified. This is the layer before the callback.
- Profile data can lag: a recent leaver may still appear, and a new hire may be missing or trigger R3.
- `/person/contact/enrich` is disabled on the event key, so drafts name a graph-sourced person, but the address must come from your own vendor records.
- Zero web traffic in the data may mean untracked rather than truly none, which is why R7b is only a medium flag.
- Outputs are risk findings, not accusations. The analysis needs no human, but the callback is a human step.
- Watcher endpoints are not available on the event key, so monitoring is not built.
- The ledger is a tamper-evident trail, not cryptographic proof of what the source said.

See [FIELD_NOTES.md](FIELD_NOTES.md) for what real API calls showed.
