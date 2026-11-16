"""G2: does the diff add a credential value anywhere?

Looks at added lines only. Placeholders and secret-store references are fine.
Evidence never echoes the matched value.
"""

from __future__ import annotations

import re

from .common import CheckResult, PRContext, cli

_ASSIGNMENT = re.compile(
    r"(?i)\b[\w.\-]*(token|passwd|password|secret|api[_-]?key|private[_-]?key)[\w.\-]*"
    r"\s*[:=]\s*[\"']?([^\s\"']{12,})"
)
_KNOWN_FORMATS = [
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
# Values that reference a secret rather than contain one.
_PLACEHOLDER = re.compile(r"(\$\{\{|\$\{|\$[A-Z_]+|<|.*\bsecrets\.|.*\bvault\b|x{6,}|\*{6,})", re.I)


def run(ctx: PRContext, params: dict) -> CheckResult:
    evidence = []
    for fc in ctx.files:
        for n, text in fc.added:
            for label, rx in _KNOWN_FORMATS:
                if rx.search(text):
                    evidence.append({"file": fc.path, "line": n, "why": label})
            m = _ASSIGNMENT.search(text)
            if m and not _PLACEHOLDER.match(m.group(2)):
                evidence.append({"file": fc.path, "line": n,
                                 "why": f"literal value assigned to a '{m.group(1).lower()}' field"})
    return CheckResult(bool(evidence), f"{len(evidence)} possible secret(s) added", evidence)


if __name__ == "__main__":
    cli(__file__, run)
