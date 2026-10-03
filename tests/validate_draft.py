#!/usr/bin/env python3
"""Validate a canon draft JSON file for schema completeness and rule quality.

Called by the test harnesses to assert that `canon draft` output meets
minimum structural standards before running `canon test` on it.

Exit 0 = valid.  Exit 1 = missing fields or counts below threshold.

Usage:
    python3 tests/validate_draft.py PATH \\
        [--min-rules N] [--min-fp N] [--min-facts N] [--check-concrete]
"""
import argparse
import json
import sys


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path", help="path to the draft JSON file")
    p.add_argument("--min-rules",  type=int, default=3,
                   help="minimum number of voice_rules required (default: 3)")
    p.add_argument("--min-fp",     type=int, default=3,
                   help="minimum number of forbidden_phrasings required (default: 3)")
    p.add_argument("--min-facts",  type=int, default=2,
                   help="minimum number of canonical_facts required (default: 2)")
    p.add_argument("--check-concrete", action="store_true",
                   help="fail if no forbidden_phrasing contains a concrete example "
                        "(quote marks or 'e.g.') — guards against vague Tier 4/5 rules")
    args = p.parse_args()

    try:
        data = json.load(open(args.path, encoding="utf-8"))
    except Exception as exc:
        print(f"could not load {args.path}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, dict) or not data:
        print("expected a non-empty JSON object", file=sys.stderr)
        return 1

    entry = list(data.values())[0]
    if not isinstance(entry, dict):
        print("top-level value must be a dict (the bible entry)", file=sys.stderr)
        return 1

    required_fields = [
        "aliases", "label", "role", "channel", "bias_lens",
        "voice_rules", "forbidden_phrasings", "canonical_facts",
    ]
    missing = [f for f in required_fields if not entry.get(f)]
    if missing:
        print(f"missing or empty fields: {missing}", file=sys.stderr)
        return 1

    vr = entry.get("voice_rules", [])
    fp = entry.get("forbidden_phrasings", [])
    cf = entry.get("canonical_facts", [])

    errors: list[str] = []
    if len(vr) < args.min_rules:
        errors.append(f"too few voice_rules ({len(vr)} < {args.min_rules})")
    if len(fp) < args.min_fp:
        errors.append(f"too few forbidden_phrasings ({len(fp)} < {args.min_fp})")
    if len(cf) < args.min_facts:
        errors.append(f"too few canonical_facts ({len(cf)} < {args.min_facts})")
    if args.check_concrete:
        has_concrete = any(
            '"' in f or "e.g." in f or "'" in f or "(" in f
            for f in fp
        )
        if not has_concrete:
            errors.append(
                "no forbidden_phrasing contains a concrete example "
                "(quote, parenthetical, or 'e.g.') — Tier 4/5 quality risk; "
                "tighten at least one rule before committing"
            )

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1

    concrete_note = " [concrete fp: yes]" if args.check_concrete else ""
    print(
        f"schema OK — {len(vr)} voice_rules, "
        f"{len(fp)} forbidden_phrasings, "
        f"{len(cf)} canonical_facts"
        f"{concrete_note}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
