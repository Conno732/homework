"""D1: is a changed dependency transitive or version-managed by a parent/BOM?"""

from __future__ import annotations

from .common import CheckResult, PRContext, Unknown, cli
from .deps import changed_packages


def run(ctx: PRContext, params: dict) -> CheckResult:
    fires_when = set(params.get("fires_when", ["transitive", "parent_managed"]))
    evidence = []
    for ch in changed_packages(ctx):
        if ch.base_version is None:
            continue  # newly added dependency: nothing was managing it
        if ch.relationship == "unknown":
            raise Unknown(f"relationship of {ch.package} is not known")
        if ch.relationship in fires_when:
            evidence.append({"service": ch.service, "package": ch.package,
                             "relationship": ch.relationship})
    return CheckResult(bool(evidence), f"{len(evidence)} non-direct dependency change(s)", evidence)


if __name__ == "__main__":
    cli(__file__, run)
