"""S1: does the diff touch a path tagged in the sensitivity registry, or change
a dependency that tagged code uses?
"""

from __future__ import annotations

from .common import CheckResult, PRContext, cli, matches_any
from .deps import changed_packages
from .usage import usages


def tags_for(ctx: PRContext, path: str) -> set:
    service = ctx.service_of(path)
    found = set()
    for entry in ctx.sensitivity["entries"]:
        if entry["service"] == "*":
            if matches_any(path, entry["paths"]):
                found.update(entry["domains"])
        elif entry["service"] == service:
            rel = path[len(f"services/{service}/"):]
            if matches_any(rel, entry["paths"]):
                found.update(entry["domains"])
    return found


def run(ctx: PRContext, params: dict) -> CheckResult:
    evidence = []
    for path in ctx.changed_paths:
        if (domains := tags_for(ctx, path)):
            evidence.append({"file": path, "domains": sorted(domains), "why": "changed directly"})
    if "manifest" in ctx.classes_touched:
        for ch in changed_packages(ctx):
            for path, line in usages(ctx, ch.service, ch.package):
                if (domains := tags_for(ctx, path)):
                    evidence.append({"file": path, "line": line, "domains": sorted(domains),
                                     "why": f"uses changed dependency {ch.package}"})
    domains = sorted({d for e in evidence for d in e["domains"]})
    return CheckResult(bool(evidence), "sensitive domains: " + (", ".join(domains) or "none"), evidence)


if __name__ == "__main__":
    cli(__file__, run)
