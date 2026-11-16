"""Escalation: a separate job that runs after the gate.

Matches the decision against escalation_rules in owners.json and "posts" to
Slack (stubbed: returns the messages; the runner writes them to an outbox file).
It reads the decision; it never changes it.
"""

from __future__ import annotations

from .checks.common import HARNESS_DIR, load_json


def _sensitive_domains(decision: dict) -> set:
    domains = set()
    for r in decision.get("results", []):
        if r["id"].startswith("S1") and r["status"] == "fired":
            for e in r.get("evidence", []):
                domains.update(e.get("domains", []))
    return domains


def escalations(decision: dict, finding: dict | None = None, override: bool = False) -> list:
    """Return the Slack messages to send for this decision (possibly none)."""
    owners = load_json(HARNESS_DIR / "owners.json")
    services = decision.get("pr", {}).get("services", [])
    unowned = [s for s in services if not owners["services"].get(s, {}).get("owner")]
    domains = _sensitive_domains(decision)

    matched = []
    for rule in owners["escalation_rules"]:
        when = rule["when"]
        hit = False
        if "sensitivity_domains_any" in when:
            hit = bool(domains & set(when["sensitivity_domains_any"]))
        if "outcome" in when:
            hit = decision["outcome"] in when["outcome"]
        if "linked_finding_severity" in when and finding:
            sla = finding.get("sla_days_remaining")
            hit = (finding.get("severity") in when["linked_finding_severity"]
                   or (sla is not None and sla <= when.get("or_sla_days_remaining_lte", -10**9)))
        if "owner_resolved" in when:
            hit = bool(unowned)
        if "override" in when:
            hit = override
        if hit:
            matched.append(rule["id"])
    if not matched:
        return []

    fallback = owners["fallback"]["slack_channel"]
    channels = set()
    for s in services or [None]:
        channels.add(owners["services"].get(s, {}).get("slack_channel") or fallback)
    if unowned:
        channels.add(fallback)

    fired = [r["id"] for r in decision.get("results", []) if r["counted"]]
    return [{
        "channel": ch,
        "pr": decision["pr"]["id"],
        "outcome": decision["outcome"],
        "escalation_rules": matched,
        "fired_checks": fired,
        "finding": finding.get("id") if finding else None,
    } for ch in sorted(channels)]
