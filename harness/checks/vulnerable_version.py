"""G3: for every dependency the diff changes, is the resolved version after the
change still known-vulnerable at or above the blocking severity?

Only packages the diff changes are checked, so an unrelated open finding
doesn't block every PR in the repo.
"""

from __future__ import annotations

from .common import CheckResult, PRContext, cli
from .deps import changed_packages, known_vulnerabilities

_SEVERITY = ["low", "moderate", "medium", "high", "critical"]


def run(ctx: PRContext, params: dict) -> CheckResult:
    floor = _SEVERITY.index(params.get("blocking_severity", "high"))
    evidence = []
    for ch in changed_packages(ctx):
        if ch.head_version is None:
            continue  # dependency removed
        for vuln in known_vulnerabilities(ctx, ch.ecosystem, ch.package, ch.head_version):
            if _SEVERITY.index(vuln["severity"]) >= floor:
                evidence.append({"service": ch.service, "package": ch.package,
                                 "version": ch.head_version, "advisory": vuln["id"],
                                 "cve": vuln.get("cve"), "severity": vuln["severity"],
                                 "fixed_in": vuln.get("fixed_in")})
    return CheckResult(bool(evidence), f"{len(evidence)} changed package(s) still vulnerable", evidence)


if __name__ == "__main__":
    cli(__file__, run)
