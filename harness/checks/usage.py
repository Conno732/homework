"""Usage, test, and excluded-test indexes, computed from in-org source only."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

from .common import PRContext, Unknown, glob_to_regex
from .deps import canonical

_SOURCE_EXT = (".py", ".java")
_SKIP_MARKERS = ("@Disabled", "@Ignore", "pytest.mark.skip", "pytest.mark.xfail", "unittest.skip")


def _is_test(ctx: PRContext, path: str) -> bool:
    return ctx.file_class(path) == "tests"


def usages(ctx: PRContext, service: str, package: str) -> list:
    """[(path, line)] where non-test in-org code imports the package."""
    imports = ctx.data("package_imports")
    patterns = next((v for k, v in imports.items() if canonical(k) == canonical(package)), None)
    if patterns is None:
        raise Unknown(f"no import mapping for package '{package}'")
    regexes = [re.compile(p) for p in patterns]
    hits = []
    for path in ctx.walk(f"services/{service}"):
        if not path.endswith(_SOURCE_EXT) or _is_test(ctx, path):
            continue
        for n, line in enumerate((ctx.base(path) or "").splitlines(), 1):
            if any(r.search(line) for r in regexes):
                hits.append((path, n))
    return hits


def tests_for(ctx: PRContext, source_path: str) -> list:
    """Test files that reference the given source file's module or class."""
    service = ctx.service_of(source_path)
    if service is None:
        return []
    stem = PurePosixPath(source_path).stem
    if source_path.endswith(".py"):
        ref = re.compile(rf"^\s*(?:from|import)\b.*\b{re.escape(stem)}\b")
    else:
        ref = re.compile(rf"\b{re.escape(stem)}\b")
    out = []
    for path in ctx.walk(f"services/{service}"):
        if not path.endswith(_SOURCE_EXT) or not _is_test(ctx, path):
            continue
        if any(ref.search(line) for line in (ctx.read(path) or "").splitlines()):
            out.append(path)
    return out


def excluded_tests(ctx: PRContext, service: str) -> list:
    """Tests excluded by build config or skip markers, as of head."""
    out = []
    root = f"services/{service}"
    tests = [p for p in ctx.walk(root) if p.endswith(_SOURCE_EXT) and _is_test(ctx, p)]

    pom = ctx.read(f"{root}/pom.xml")
    if pom:
        try:
            tree = ET.fromstring(pom)
        except ET.ParseError as exc:
            raise Unknown(f"pom.xml does not parse: {exc}") from exc
        for el in tree.iter():
            el.tag = el.tag.split("}", 1)[-1]
        for ex in tree.iter("exclude"):
            pattern = (ex.text or "").strip()
            for t in tests:
                if pattern and glob_to_regex(pattern).match(t):
                    out.append({"test": t, "mechanism": "surefire exclude",
                                "location": f"{root}/pom.xml", "pattern": pattern})

    for t in tests:
        for n, line in enumerate((ctx.read(t) or "").splitlines(), 1):
            if any(m in line for m in _SKIP_MARKERS):
                out.append({"test": t, "mechanism": "skip marker", "location": f"{t}:{n}"})
                break
    return out
