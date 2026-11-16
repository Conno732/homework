"""Evidence attestation: what an auditor is handed for one finding.

Joins the gate's decision with build, deploy, and runtime-verification records
(all three mocked here). The rule that matters: a finding is only "remediated
in production" from the moment a runtime check shows zero vulnerable instances.
A merged PR or a successful deploy is not enough.
"""

from __future__ import annotations

from .checks.common import HARNESS_DIR, load_json
from .gate import verify_seal


def build(finding: dict, decision: dict, pipeline: dict, provenance: dict | None = None) -> dict:
    """pipeline = {"approval", "change", "build", "deployment", "runtime_verification"}.
    provenance says who proposed the change. It is recorded, never relied on."""
    owners = load_json(HARNESS_DIR / "owners.json")
    owner = owners["services"].get(finding["service"], {}).get("owner") or owners["fallback"]["owner"]
    approval = pipeline.get("approval") or {}
    change, runtime = pipeline.get("change") or {}, pipeline.get("runtime_verification") or {}

    problems = []
    if not verify_seal(decision):
        problems.append("gate decision record failed its integrity check")
    if decision["outcome"] == "reject" and not approval.get("override"):
        problems.append("gate rejected this change and no owner override is recorded")
    if decision["outcome"] == "review" and approval.get("approver_team") != owner:
        problems.append(f"review required from {owner}, but no approval from them is recorded")
    if change.get("merged_diff_sha256") != decision["pr"].get("diff_sha256"):
        problems.append("the merged diff is not the diff the gate scored")
    scored_base = decision["pr"].get("base_sha")
    if not scored_base or change.get("base_sha_at_merge") != scored_base:
        problems.append("the target branch moved after the gate scored this change (stale decision)")
    if not runtime:
        problems.append("no runtime verification yet")
    elif runtime.get("instances_with_vulnerable_version", 1) != 0 \
            or runtime.get("previous_vulnerable_digests_running", 1) != 0:
        problems.append("vulnerable instances are still running")

    return {
        "attestation_version": "1.0",
        "finding": {k: finding.get(k) for k in
                    ("id", "cve", "service", "package", "installed_version", "fixed_version")},
        "provenance": provenance or {},
        "gate_decision": {
            "pr": decision["pr"]["id"],
            "base_sha": scored_base,
            "diff_sha256": decision["pr"].get("diff_sha256"),
            "rubric_version": decision["rubric"]["version"],
            "rubric_sha256": decision["rubric"]["sha256"],
            "outcome": decision["outcome"],
            "score": decision["score"],
            "lowered_by": decision["lowered_by"],
            "decided_at": decision["decided_at"],
            "record_sha256": decision["record_sha256"],
        },
        "approval": {"required": decision["outcome"] != "approve",
                     "required_from": owner if decision["outcome"] != "approve" else None,
                     **approval},
        "change": change,
        "build": pipeline.get("build") or {},
        "deployment": pipeline.get("deployment") or {},
        "runtime_verification": runtime,
        "remediated_in_production_at": None if problems else runtime["checked_at"],
        "not_remediated_because": problems,
    }
