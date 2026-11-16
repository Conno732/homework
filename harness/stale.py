"""Stale-decision check.

A gate decision is only valid for the exact inputs it scored: this diff, on
this base commit, under these rules. If another PR merges first, the base
moves and the answers can change even though the diff didn't (tests removed, a
file tagged sensitive, a new caller added). A stale decision must not be used
to merge; the gate has to run again on the new base.

    python -m harness.stale --decision d.json --base-sha <target branch head> [--diff pr.diff]

Exit code 1 if stale.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .checks.common import HARNESS_DIR, rule_digests, sha256_file
from .gate import verify_seal


def reasons(decision: dict, current_base_sha: str | None, current_diff: str | None = None) -> list:
    """Why this decision can't be relied on any more. Empty list = still fresh."""
    out = []
    if not verify_seal(decision):
        out.append("decision record failed its integrity check")
    scored_base = decision.get("pr", {}).get("base_sha")
    if not scored_base:
        out.append("decision does not record the base commit it was scored against")
    elif not current_base_sha:
        out.append("current base commit is unknown")
    elif scored_base != current_base_sha:
        out.append(f"target branch moved: scored on {scored_base}, now {current_base_sha}")
    if decision["rubric"]["sha256"] != sha256_file(HARNESS_DIR / "rubric.json"):
        out.append("rubric changed since the decision")
    current = rule_digests()
    for name, digest in decision.get("registries", {}).items():
        if digest != current.get(name):
            out.append(f"{name} changed since the decision")
    if current_diff is not None:
        digest = hashlib.sha256(current_diff.replace("\r\n", "\n").encode("utf-8")).hexdigest()
        if digest != decision["pr"].get("diff_sha256"):
            out.append("the diff changed since the decision")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--decision", required=True)
    ap.add_argument("--base-sha", required=True, help="current head of the target branch")
    ap.add_argument("--diff", help="the PR's current diff")
    args = ap.parse_args(argv)
    decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
    diff = Path(args.diff).read_text(encoding="utf-8") if args.diff else None
    why = reasons(decision, args.base_sha, diff)
    print(json.dumps({"fresh": not why, "reasons": why}, indent=2))
    return 1 if why else 0


if __name__ == "__main__":
    sys.exit(main())
