"""Run the harness.

  python -m harness.run                    findings -> proposer -> gate -> notify
  python -m harness.run --all-prs          gate every recorded PR (any proposer)
  python -m harness.run --pr <dir>         gate one recorded PR
  python -m harness.run --diff <file>      gate a raw diff with no judge (CI mode)

Nothing here calls an LLM or the network. The agent and the judge replay
recorded fixtures.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import attest, gate, notify, proposer
from .checks.common import DEFAULT_REPO, HARNESS_DIR, load_json
from .judge import RecordedJudge, UnavailableJudge

OUT = HARNESS_DIR / "out"


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _fired(decision: dict) -> str:
    parts = []
    for r in decision.get("results", []):
        if r["counted"]:
            parts.append(r["id"].split("_")[0] + ("?" if r["status"] == "unknown" else ""))
    return " ".join(parts) or "-"


def gate_pr(pr: dict, now, base_sha=None, finding=None) -> tuple:
    decision = gate.decide(pr["diff"], RecordedJudge(pr.get("judge")), pr_id=pr["id"], now=now,
                           base_sha=base_sha)
    messages = notify.escalations(decision, finding)
    _write(OUT / "decisions" / f"{pr['id']}.json", decision)
    return decision, messages


def _row(label: str, action: str, decision: dict | None, messages: list) -> str:
    if decision is None:
        return f"{label:<32} {action:<18} {'-':<8} {'-':>5}  -"
    slack = ",".join(m["channel"] for m in messages) or "-"
    return (f"{label:<32} {action:<18} {decision['outcome'].upper():<8} "
            f"{str(decision['score']):>5}  {_fired(decision)}   slack: {slack}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--findings", default=str(DEFAULT_REPO / "findings" / "findings.json"))
    ap.add_argument("--all-prs", action="store_true")
    ap.add_argument("--pr")
    ap.add_argument("--diff")
    ap.add_argument("--now", help="ISO time to use as 'now' (reproducible output)")
    ap.add_argument("--base-sha", help="commit the repo is checked out at (recorded for the stale check)")
    args = ap.parse_args(argv)
    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now
           else datetime.now(timezone.utc))

    header = f"{'':<32} {'proposer':<18} {'gate':<8} {'score':>5}  fired (? = unknown, counted as fired)"
    outbox = []

    if args.diff:  # CI mode: no judge, so judge checks are unknown and fail closed
        decision = gate.decide(Path(args.diff).read_text(encoding="utf-8"), UnavailableJudge(),
                               pr_id=Path(args.diff).stem, now=now, base_sha=args.base_sha)
        print(json.dumps(decision, indent=2))
        return 1 if decision["outcome"] == "reject" else 0

    print(header)
    if args.pr or args.all_prs:
        dirs = [Path(args.pr)] if args.pr else sorted(p.parent for p in proposer.PRS_DIR.glob("*/pr.json"))
        for d in dirs:
            pr = proposer.load_pr(d)
            decision, messages = gate_pr(pr, now, args.base_sha)
            outbox += messages
            print(_row(pr["id"], pr.get("proposer", "-"), decision, messages))
    else:
        findings_doc = load_json(Path(args.findings))
        actions = proposer.run(findings_doc, now=now)
        if actions:
            print(f"  (findings feed is {actions[0]['feed_age_days']} days old; every finding was re-verified)")
        for a in actions:
            f = a["finding"]
            label = f"{f['id']} {f['package'].split(':')[-1]}"
            for note in a["reverification"]["notes"]:
                print(f"  ! {f['id']}: {note}")
            if a["action"] == "pr":
                decision, messages = gate_pr(a["pr"], now, args.base_sha, finding=f)
                outbox += messages
                print(_row(label, "PR", decision, messages))
            else:
                _write(OUT / "escalations" / f"{f['id']}.json",
                       {"finding": f, "action": a["action"], "reason": a["reason"],
                        "reverification": a["reverification"]})
                print(_row(label, a["action"].upper(), None, []))
                print(f"  -> {a['reason']}")

        # Sample evidence: join one decision with mocked pipeline records.
        sample = load_json(HARNESS_DIR / "fixtures" / "pipeline" / "F-40219.json")
        act = next((a for a in actions if a["finding"]["id"] == "F-40219" and a["action"] == "pr"), None)
        if act:
            decision = load_json(OUT / "decisions" / f"{act['pr']['id']}.json")
            sample["change"]["merged_diff_sha256"] = decision["pr"]["diff_sha256"]
            sample["change"]["base_sha_at_merge"] = decision["pr"]["base_sha"]
            provenance = {"proposer": act["pr"]["proposer"], "agent": "recorded fixture (no model call)",
                          "target_version": act["target_version"],
                          "reverification": act["reverification"]}
            _write(OUT / "attestation.F-40219.json",
                   attest.build(act["finding"], decision, sample, provenance))

    _write(OUT / "slack_outbox.json", outbox)
    print(f"\ndecision records: {OUT.relative_to(DEFAULT_REPO).as_posix()}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
