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

The app opens as a mailbox with the sample emails loaded. Press **Scan** to check them all: samples replay offline and spend no credits. You can also upload PDF, TXT or EML files, or paste text. Uploaded, pasted and mailbox mail always runs live.

### Connect a real mailbox (optional)

Add these to `.env` and restart. For Gmail, turn on 2-step verification and use an app password, not your account password.

```
IMAP_HOST=imap.gmail.com
IMAP_USER=you@example.com
IMAP_PASSWORD=your_app_password
```

Choose **Live mailbox** in the sidebar. While the page is open, the app checks for new mail every 60 seconds, imports payment-related messages and scans them automatically, up to `CLEARANCE_DAILY_CREDIT_LIMIT` credits a day (default 100). Samples are hidden in this mode. Turn off **Auto-scan new mail** to review before spending credits.

The connection is read-only: the folder opens read-only and messages are fetched without marking them read, so reading it should not change anything in the mailbox. The first sync looks back over the newest 200 messages and imports up to 25 payment-related ones. Checks send the sender's name and domain and the vendor's name to Crustdata, and send the full email text to Anthropic only if `ANTHROPIC_API_KEY` is set.

To feed it company mail, use a dedicated mailbox and forward to it. In Microsoft 365, use a **Redirect** rule, because **Forward** makes your employee look like the sender.

### Send from the portal (off by default)

The agent never sends anything. A person can send an email from the **Reply or send from Clearance** panel, but only to allowed addresses, and only after confirming each message:

```
SEND_ALLOWLIST=your.test.address@example.com,second.test@example.com
```

To allow replies to whoever emailed the mailbox, add `SEND_TO_SENDER=true`. The sender is read from the email's `From` header, and is only added for live mailbox mail, for that one email. If the email was not approved, sending to its sender needs a second confirmation, because a reply tells a fraudster they were noticed.

Leave both out to keep sending off, which is what the event rules require for real outside people. Mail goes out through Gmail's SMTP server using the same app password; set `SMTP_HOST` and `SMTP_PORT` for other providers. To verify a held request, call the vendor using your own records instead of replying. Replies are new messages with a `Re:` subject, so they may not join the original thread.

**Simulated verification:** on a flagged email with a verified draft, **Send verification (simulated)** delivers that draft to an address in `SEND_ALLOWLIST`, labelled with the person Crustdata identified. The vendor contact is never emailed: the event rules forbid it, and `/person/contact/enrich` is not enabled on the event key, so the graph gives a name and role but no address.

### Storage

Mail and verdicts are saved so they survive a refresh. By default they go to a git-ignored SQLite file in `data/`. To use Supabase instead, run [supabase/schema.sql](supabase/schema.sql) once in the Supabase SQL editor and add these to `.env`:

```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your_service_role_key
```

The service key bypasses row level security, so keep it on the server and never put it in a browser or in Git. The tables have row level security on with no policies, so the public anon key cannot read them. They can contain real email, so treat the project as sensitive.

**Not production-ready yet:** the app has no login, so do not host it publicly with a real mailbox connected, and syncing only runs while the page is open.

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

**Saved data is pseudonymized.** This repo is public, so people in `fixtures/` and the sample packets have made-up names and IDs, and unused profile fields are removed. Company data and the role data behind each person (title, function, dates) came from real API responses. The results for samples with an employee sender, such as `case_a`, `test_1` and `test_2`, are therefore replays of real role data under a made-up name: those names will not match anyone in a fresh lookup. To see a real match, paste a packet naming a real employee with offline replay off. Responses saved by your own runs are not anonymized, so review them before committing.

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
- PDFs are read for their text layer only. Scanned PDFs get a warning, because there is no OCR. A PDF alone has no sender, so sender checks stay UNVERIFIABLE and the result is HOLD; upload the `.eml` to check the sender too.
- A forged `From` header is not detected, because SPF, DKIM and DMARC results are not checked yet.
- Profile data can lag: a recent leaver may still appear, and a new hire may be missing or trigger R3.
- `/person/contact/enrich` is disabled on the event key, so drafts name a graph-sourced person, but the address must come from your own vendor records.
- Zero web traffic in the data may mean untracked rather than truly none, which is why R7b is only a medium flag.
- Outputs are risk findings, not accusations. The analysis needs no human, but the callback is a human step.
- Watcher endpoints are not available on the event key, so monitoring is not built.
- The ledger is a tamper-evident trail, not cryptographic proof of what the source said.

See [FIELD_NOTES.md](FIELD_NOTES.md) for what real API calls showed.
