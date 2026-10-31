"""Decision engine. Outcomes come only from rule findings + policy.yaml."""
from __future__ import annotations

from .schemas import Decision, Finding, Resolution


def independent_hard_count(hard: list[Finding], policy: dict) -> int:
    ids = {f.rule_id for f in hard}
    count = len(ids)
    for group in policy["decision"].get("correlated_hard_groups", []):
        overlap = len(ids & set(group))
        if overlap > 1:
            count -= overlap - 1
    return count


def decide(findings: list[Finding], resolution: Resolution | None, policy: dict) -> Decision:
    d = policy["decision"]
    flags = [f for f in findings if f.status == "FLAG"]
    hard = [f for f in flags if f.severity == "hard"]
    medium = [f for f in flags if f.severity == "medium"]
    # R12 only summarises the other gaps, so it does not count as one.
    gaps = [f for f in findings if f.status == "UNVERIFIABLE" and f.rule_id != "R12"]
    gap_ids = [f.rule_id for f in gaps]
    n_hard = independent_hard_count(hard, policy)
    ambiguous = resolution is not None and resolution.status == "AMBIGUOUS"
    flag_reasons = [f"{f.rule_id} ({f.severity}): {f.message}" for f in flags]

    if n_hard >= d["escalate_min_hard"] or (n_hard >= 1 and len(medium) >= d["escalate_hard_with_medium"]):
        outcome, reasons = "ESCALATE", flag_reasons
    elif n_hard >= 1:
        outcome, reasons = "HOLD", ["Callback required.", *flag_reasons]
    elif ambiguous:
        outcome, reasons = "HOLD", ["Vendor is ambiguous; candidates are listed and none was picked.", *flag_reasons]
    elif len(gaps) > d["max_unverifiable"]:
        outcome, reasons = "HOLD", [f"{len(gaps)} checks could not be verified: {', '.join(gap_ids)}.", *flag_reasons]
    elif flags:
        outcome, reasons = "HOLD", ["Flags need review.", *flag_reasons]
    elif resolution is None or resolution.status != "RESOLVED" or set(d["key_rules"]) & set(gap_ids):
        missing = [r for r in d["key_rules"] if r in gap_ids]
        outcome, reasons = "HOLD", [f"Insufficient evidence to approve; key checks unverified: {', '.join(missing) or 'vendor resolution'}."]
    else:
        outcome, reasons = "APPROVE", ["No flags, and all key checks were evaluated."]

    return Decision(
        outcome=outcome,
        reasons=reasons,
        findings=findings,
        unverifiable=gap_ids,
        what_would_change_this=_what_would_change(gaps),
    )


def _what_would_change(gaps: list[Finding]) -> str:
    if not gaps:
        return "Nothing outstanding: every check was evaluated on available evidence."
    return "Evidence for " + ", ".join(f.rule_id for f in gaps) + " (" + gaps[0].message + ")"
