"""Dependency-update proposer (the workflow that opens PRs).

Decides what to try. It has no say in what merges: its only output is a PR
for the gate, or an escalation when it refuses.

The agent is stubbed: a "proposal" is a recorded diff under fixtures/prs/.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .checks.common import HARNESS_DIR, PRContext, Unknown, load_json
from .checks.deps import canonical, known_vulnerabilities, resolved

PRS_DIR = HARNESS_DIR / "fixtures" / "prs"


def load_pr(pr_dir: Path) -> dict:
    """A recorded PR: metadata, the diff, and recorded judge rollouts."""
    pr_dir = Path(pr_dir)
    meta = load_json(pr_dir / "pr.json")
    judge_file = pr_dir / "judge.json"
    return {**meta, "dir": str(pr_dir),
            "diff": (pr_dir / "change.diff").read_text(encoding="utf-8"),
            "judge": load_json(judge_file) if judge_file.is_file() else {}}


class RecordedAgent:
    """Stand-in for the LLM agent: returns the recorded PR for a finding."""

    def __init__(self, prs_dir: Path = PRS_DIR):
        self.prs_dir = Path(prs_dir)

    def propose(self, finding: dict, target_version: str) -> dict | None:
        for meta in sorted(self.prs_dir.glob("*/pr.json")):
            pr = load_json(meta)
            if pr.get("proposer") == "dependency-update" and pr.get("linked_finding") == finding["id"]:
                return load_pr(meta.parent)
        return None


def reverify(finding: dict, ctx: PRContext) -> dict:
    """Check the scanner's claims against the repo and the vulnerability database."""
    notes: list = []
    out = {"installed_version": None, "db_fixed_version": None, "notes": notes}
    try:
        tree = resolved(ctx, finding["service"], "base")
    except Unknown as exc:
        notes.append(f"could not resolve dependencies: {exc}")
        return out
    pkg = next((n for n in tree if canonical(n) == canonical(finding["package"])), None)
    if pkg is None:
        notes.append("package is no longer a dependency")
        return out
    info = tree[pkg]
    out["installed_version"] = info["version"]
    if info["version"] != finding["installed_version"]:
        notes.append(f"scanner says {finding['installed_version']} is installed, repo resolves {info['version']}")

    vulns = [v for v in known_vulnerabilities(ctx, info["ecosystem"], pkg, info["version"])
             if v.get("cve") == finding["cve"]]
    out["still_vulnerable"] = bool(vulns)
    if not vulns:
        notes.append(f"vulnerability database does not list {finding['cve']} for {pkg} {info['version']}")
        return out
    vuln = vulns[0]
    out["db_fixed_version"] = vuln.get("fixed_in")
    out["advisory"] = vuln["id"]
    if vuln.get("fixed_in") != finding.get("fixed_version"):
        notes.append(f"scanner says fixed_version={finding.get('fixed_version')}, "
                     f"vulnerability database says {vuln.get('fixed_in')}")
    if vuln["severity"] != finding["severity"]:
        notes.append(f"scanner severity '{finding['severity']}', database severity '{vuln['severity']}'")
    return out


def run(findings_doc: dict, agent=None, now: datetime | None = None,
        repo_root=None, data_dir=None) -> list:
    """Return one action per finding: a PR to gate, a refusal, or a skip."""
    agent = agent or RecordedAgent()
    now = now or datetime.now(timezone.utc)
    kwargs = {k: v for k, v in (("repo_root", repo_root), ("data_dir", data_dir)) if v}
    ctx = PRContext("", **kwargs)  # empty diff: the repo as it is today

    generated = datetime.strptime(findings_doc["generated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age_days = (now - generated).days

    actions = []
    for finding in findings_doc["findings"]:
        check = reverify(finding, ctx)
        action = {"finding": finding, "feed_age_days": age_days, "reverification": check}

        if check["installed_version"] is None:
            action.update(action="skip", reason="; ".join(check["notes"]))
        elif check.get("still_vulnerable") is False:
            action.update(action="escalate",
                          reason="scanner finding is not confirmed by the vulnerability database; "
                                 "a person should confirm before anything changes")
        else:
            # The database outranks the scanner's note when they disagree.
            target = check["db_fixed_version"]
            if target is None:
                action.update(action="refuse",
                              reason="no fixed version exists; remediation is a migration or a "
                                     "risk-acceptance decision for the owner, not a dependency fix")
            else:
                pr = agent.propose(finding, target)
                if pr is None:
                    action.update(action="escalate", reason="agent produced no proposal")
                else:
                    action.update(action="pr", target_version=target, pr=pr)
        actions.append(action)
    return actions
