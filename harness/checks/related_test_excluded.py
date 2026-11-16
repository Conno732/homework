"""D3: is a test that exercises the affected code currently excluded or skipped?

Affected code = changed source files, plus in-org files that use a changed
dependency. If such a test is switched off, green CI says nothing about it.
"""

from __future__ import annotations

from .common import CheckResult, PRContext, cli
from .deps import changed_packages
from .usage import excluded_tests, tests_for, usages


def run(ctx: PRContext, params: dict) -> CheckResult:
    affected = {p for p in ctx.changed_paths
                if ctx.file_class(p) == "source" and ctx.service_of(p)}
    if "manifest" in ctx.classes_touched:
        for ch in changed_packages(ctx):
            affected.update(path for path, _ in usages(ctx, ch.service, ch.package))

    evidence = []
    excluded_by_service: dict = {}
    for path in sorted(affected):
        service = ctx.service_of(path)
        if service not in excluded_by_service:
            excluded_by_service[service] = excluded_tests(ctx, service)
        related = set(tests_for(ctx, path))
        for ex in excluded_by_service[service]:
            if ex["test"] in related:
                evidence.append({"affected_file": path, **ex})
    return CheckResult(bool(evidence), f"{len(evidence)} related test(s) excluded", evidence)


if __name__ == "__main__":
    cli(__file__, run)
