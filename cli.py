"""Usage: python cli.py --packet packets/case_b.txt [--offline] [--mask] [--as-of 2026-10-31]"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from clearance.config import FIXTURES_DIR, OUTPUTS_DIR, credit_cap, crustdata_api_key, load_policy
from clearance.crustdata import CrustdataClient
from clearance.pipeline import run_case
from clearance.report import mask, person_names, to_markdown, write_report


def main() -> int:
    ap = argparse.ArgumentParser(description="Clearance: counterparty trust agent")
    ap.add_argument("--packet", required=True, type=Path)
    ap.add_argument("--offline", action="store_true", help="replay saved responses; make no network calls")
    ap.add_argument("--mask", action="store_true", help="mask people's names and email addresses in output")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None)
    args = ap.parse_args()

    try:
        text = args.packet.read_text()
    except OSError as e:
        print(f"Cannot read packet: {e.strerror}", file=sys.stderr)
        return 2

    policy = load_policy()
    as_of = args.as_of or (policy["offline_as_of"] if args.offline else date.today())
    client = CrustdataClient(crustdata_api_key(), FIXTURES_DIR, offline=args.offline, credit_cap=credit_cap())
    lines: list[tuple[str, str]] = []

    def emit(stage: str, message: str) -> None:
        lines.append((stage, message))
        if not args.mask:
            print(f"[{stage:8}] {message}", flush=True)

    result = run_case(text, client, policy, as_of, case_id=args.packet.stem, emit=emit)
    if args.mask:
        names = person_names(result)
        for stage, message in lines:
            print(f"[{stage:8}] {mask(message, names)}")
    md, js = write_report(result, OUTPUTS_DIR, args.packet.stem, args.mask)
    print()
    print(to_markdown(result, args.mask))
    print(f"Saved {md} and {js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
