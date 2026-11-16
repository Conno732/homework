"""D4: do non-generated changed lines exceed the limit? A weak signal by design."""

from __future__ import annotations

from .common import CheckResult, PRContext, cli, matches_any


def run(ctx: PRContext, params: dict) -> CheckResult:
    limit = params.get("max_lines", 300)
    skip = params.get("exclude_globs", [])
    total = sum(
        len(fc.added) + len(fc.removed)
        for fc in ctx.files
        if ctx.file_class(fc.path) in ("source", "tests") and not matches_any(fc.path, skip)
    )
    return CheckResult(total > limit, f"{total} changed line(s), limit {limit}",
                       [{"changed_lines": total, "limit": limit}])


if __name__ == "__main__":
    cli(__file__, run)
