"""D2: does a changed dependency cross a major version boundary?"""

from __future__ import annotations

from .common import CheckResult, PRContext, cli
from .deps import changed_packages, version_tuple


def run(ctx: PRContext, params: dict) -> CheckResult:
    evidence = []
    for ch in changed_packages(ctx):
        if ch.base_version is None or ch.head_version is None:
            continue
        if version_tuple(ch.base_version)[0] != version_tuple(ch.head_version)[0]:
            evidence.append({"service": ch.service, "package": ch.package,
                             "from": ch.base_version, "to": ch.head_version})
    return CheckResult(bool(evidence), f"{len(evidence)} major version jump(s)", evidence)


if __name__ == "__main__":
    cli(__file__, run)
