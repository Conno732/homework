"""G1: does the diff disable, skip, exclude, or delete existing tests?

Adding or strengthening tests is fine. Fires on: an added skip/exclude marker,
a deleted test file, or a test file that loses more tests/assertions than it gains.
"""

from __future__ import annotations

import re

from .common import CheckResult, PRContext, cli

_TEST_DEF = re.compile(r"^\s*(def test_\w+|@Test\b|@ParameterizedTest\b)")
_ASSERT = re.compile(r"\bassert\w*\b")


def run(ctx: PRContext, params: dict) -> CheckResult:
    evidence = []
    for fc in ctx.files:
        cls = ctx.file_class(fc.path)
        if cls == "docs":
            continue
        for n, text in fc.added:
            for pattern in params.get("added_line_patterns", []):
                if pattern in text:
                    evidence.append({"file": fc.path, "line": n, "why": f"adds '{pattern}'"})
        if cls != "tests":
            continue
        if fc.is_deleted and params.get("detect_deleted_tests"):
            evidence.append({"file": fc.path, "why": "test file deleted"})
        elif params.get("detect_removed_assertions"):
            for label, rx in (("tests", _TEST_DEF), ("assertions", _ASSERT)):
                lost = sum(1 for _, t in fc.removed if rx.search(t))
                gained = sum(1 for _, t in fc.added if rx.search(t))
                if lost > gained:
                    evidence.append({"file": fc.path, "why": f"removes {lost} {label}, adds {gained}"})
    return CheckResult(bool(evidence), f"{len(evidence)} test-weakening change(s)", evidence)


if __name__ == "__main__":
    cli(__file__, run)
