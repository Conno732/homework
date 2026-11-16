"""D5: does every service the diff touches have an owner in owners.json?

Paths outside services/ are repo-wide and belong to the fallback owner.
"""

from __future__ import annotations

from .common import CheckResult, PRContext, cli


def run(ctx: PRContext, params: dict) -> CheckResult:
    evidence = []
    for service in sorted(ctx.services_touched):
        if not ctx.owners["services"].get(service, {}).get("owner"):
            evidence.append({"service": service, "why": "no owner in owners.json"})
    repo_wide = [p for p in ctx.changed_paths if ctx.service_of(p) is None]
    if repo_wide and not ctx.owners.get("fallback", {}).get("owner"):
        evidence.append({"service": None, "why": "repo-wide change and no fallback owner"})
    return CheckResult(bool(evidence), f"{len(evidence)} unowned area(s)", evidence)


if __name__ == "__main__":
    cli(__file__, run)
