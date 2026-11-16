"""Shared plumbing for static checks: PR context, diff parsing, file classes.

Every check script exposes ``run(ctx, params) -> CheckResult`` and can also be
run on its own: ``python -m harness.checks.<name> --diff some.diff``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REPO = HARNESS_DIR.parent
DEFAULT_DATA = HARNESS_DIR / "fixtures" / "data"

# Vendored / third-party / build output. Never read by checks or shown to an LLM.
DENIED_DIRS = {
    "node_modules", "site-packages", ".venv", "venv", "target", "vendor",
    "build", ".git", "__pycache__", ".pytest_cache",
}


class Unknown(Exception):
    """A check could not be answered. The gate treats it as a fail."""


class PatchError(Exception):
    """The diff does not apply to the base tree."""


@dataclass
class CheckResult:
    fired: bool | None  # True = the risk is present, False = clear, None = unknown
    detail: str = ""
    evidence: list = field(default_factory=list)


# --------------------------------------------------------------------------- globs

def glob_to_regex(glob: str) -> re.Pattern:
    """Translate a path glob. ``**`` crosses directories, ``*`` does not."""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        elif glob[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile("^" + out + "$")


def matches_any(path: str, globs) -> bool:
    return any(glob_to_regex(g).match(path) for g in globs)


def is_denied(path: str) -> bool:
    return any(part in DENIED_DIRS for part in Path(path).parts)


# --------------------------------------------------------------------------- diffs

@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list  # (tag, text) with tag in " ", "+", "-"


@dataclass
class FileChange:
    path: str  # new path, or the old path when the file is deleted
    old_path: str | None
    is_new: bool
    is_deleted: bool
    hunks: list = field(default_factory=list)

    @property
    def added(self):
        """[(line number in the new file, text)]"""
        out = []
        for h in self.hunks:
            n = h.new_start
            for tag, text in h.lines:
                if tag == "+":
                    out.append((n, text))
                if tag in "+ ":
                    n += 1
        return out

    @property
    def removed(self):
        """[(line number in the old file, text)]"""
        out = []
        for h in self.hunks:
            n = h.old_start
            for tag, text in h.lines:
                if tag == "-":
                    out.append((n, text))
                if tag in "- ":
                    n += 1
        return out


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _clean_path(raw: str) -> str | None:
    raw = raw.split("\t")[0].strip()
    if raw == "/dev/null":
        return None
    return raw[2:] if raw[:2] in ("a/", "b/") else raw


def parse_diff(text: str) -> list:
    files, cur, hunk = [], None, None
    old_left = new_left = 0
    pending_old = None
    for line in text.splitlines():
        line = line.rstrip("\r")
        if hunk is not None and (old_left > 0 or new_left > 0):
            if line.startswith("\\"):  # "\ No newline at end of file"
                continue
            tag, body = (line[:1] or " "), line[1:]
            if tag not in " +-":
                raise PatchError(f"unexpected line inside hunk: {line!r}")
            hunk.lines.append((tag, body))
            if tag in " -":
                old_left -= 1
            if tag in " +":
                new_left -= 1
            continue
        if line.startswith("--- "):
            pending_old = _clean_path(line[4:])
            hunk = None
        elif line.startswith("+++ "):
            new = _clean_path(line[4:])
            cur = FileChange(
                path=new or pending_old,
                old_path=pending_old,
                is_new=pending_old is None,
                is_deleted=new is None,
            )
            files.append(cur)
        elif (m := _HUNK.match(line)) and cur is not None:
            hunk = Hunk(
                int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1), []
            )
            old_left, new_left = hunk.old_len, hunk.new_len
            cur.hunks.append(hunk)
    if old_left or new_left:
        raise PatchError("diff ends in the middle of a hunk")
    return files


def apply_hunks(base_lines: list, hunks: list) -> list:
    out, pos = [], 0
    for h in hunks:
        start = h.old_start - 1 if h.old_len else h.old_start
        if start < pos or start > len(base_lines):
            raise PatchError("hunk is out of range")
        out.extend(base_lines[pos:start])
        pos = start
        for tag, text in h.lines:
            if tag == "+":
                out.append(text)
                continue
            if pos >= len(base_lines) or base_lines[pos] != text:
                raise PatchError(f"hunk does not match base at line {pos + 1}")
            if tag == " ":
                out.append(text)
            pos += 1
    out.extend(base_lines[pos:])
    return out


# --------------------------------------------------------------------------- context

_CLASS_ORDER = ["manifest", "tests", "docs", "pipeline", "source"]


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    # Hash with normalized newlines so the digest is the same on every platform.
    text = Path(path).read_text(encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rule_digests() -> dict:
    """Digests of everything besides rubric.json that shapes a decision.
    A decision is stale if any of these change."""
    out = {name: sha256_file(HARNESS_DIR / f"{name}.json") for name in ("sensitivity", "owners", "tools")}
    h = hashlib.sha256()
    for p in sorted((HARNESS_DIR / "prompts").glob("*.md")):
        h.update(p.name.encode("utf-8") + b"\0")
        h.update(p.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8") + b"\0")
    out["prompts"] = h.hexdigest()
    return out


class PRContext:
    """Everything a check may look at: the diff, the repo at the base commit,
    the registries, and the fixture-backed data sources. Deliberately absent:
    PR author, title, description, linked issues, proposer rationale."""

    def __init__(self, diff_text: str, repo_root=DEFAULT_REPO, data_dir=DEFAULT_DATA,
                 rubric=None):
        self.repo_root = Path(repo_root)
        self.data_dir = Path(data_dir)
        self.diff_text = diff_text.replace("\r\n", "\n")
        self.diff_sha256 = hashlib.sha256(self.diff_text.encode("utf-8")).hexdigest()
        self.rubric = rubric or load_json(HARNESS_DIR / "rubric.json")
        self.sensitivity = load_json(HARNESS_DIR / "sensitivity.json")
        self.owners = load_json(HARNESS_DIR / "owners.json")
        self.files = parse_diff(self.diff_text)
        self.by_path = {f.path: f for f in self.files}
        self._head_cache: dict = {}
        self.cache: dict = {}  # shared by checks (dependency resolution etc.)

    # --- file access
    def base(self, path: str) -> str | None:
        p = self.repo_root / path
        if is_denied(path) or not p.is_file():
            return None
        return p.read_text(encoding="utf-8")

    def head(self, path: str) -> str | None:
        fc = self.by_path.get(path)
        if fc is None:
            return self.base(path)
        if fc.is_deleted:
            return None
        if path not in self._head_cache:
            if fc.is_new:
                base_lines = []
            else:
                base_text = self.base(fc.old_path or path)
                if base_text is None:
                    raise PatchError(f"{path}: modified in the diff but missing at base")
                base_lines = base_text.splitlines()
            self._head_cache[path] = "\n".join(apply_hunks(base_lines, fc.hunks)) + "\n"
        return self._head_cache[path]

    def read(self, path: str, side: str = "head") -> str | None:
        return self.head(path) if side == "head" else self.base(path)

    def validate(self) -> None:
        """Raise PatchError unless the whole diff applies to the base tree."""
        for fc in self.files:
            if fc.is_deleted:
                if self.base(fc.path) is None:
                    raise PatchError(f"{fc.path}: deleted in the diff but missing at base")
                apply_hunks(self.base(fc.path).splitlines(), fc.hunks)
            else:
                self.head(fc.path)

    # --- classification
    @property
    def changed_paths(self) -> list:
        return [f.path for f in self.files]

    def file_class(self, path: str) -> str:
        classes = self.rubric["file_classes"]
        for name in _CLASS_ORDER:
            if matches_any(path, classes[name]):
                return name
        return "source"

    @property
    def classes_touched(self) -> set:
        return {self.file_class(p) for p in self.changed_paths}

    @staticmethod
    def service_of(path: str) -> str | None:
        m = re.match(r"services/([^/]+)/", path)
        return m.group(1) if m else None

    @property
    def services_touched(self) -> set:
        return {s for p in self.changed_paths if (s := self.service_of(p))}

    # --- data sources (fixture-backed stand-ins for real integrations)
    def data(self, name: str):
        p = self.data_dir / f"{name}.json"
        if not p.is_file():
            raise Unknown(f"data source '{name}' is unavailable")
        return load_json(p)

    def walk(self, root: str):
        """Yield repo-relative paths of in-org files under root (base tree)."""
        top = self.repo_root / root
        if not top.is_dir():
            return
        for p in sorted(top.rglob("*")):
            rel = p.relative_to(self.repo_root).as_posix()
            if p.is_file() and not is_denied(rel):
                yield rel


# --------------------------------------------------------------------------- CLI

def cli(script_file: str, run) -> None:
    """Run one check on its own and print its result as JSON."""
    ap = argparse.ArgumentParser(description=(sys.modules[run.__module__].__doc__ or "").strip())
    ap.add_argument("--diff", required=True, help="unified diff file to check")
    ap.add_argument("--repo", default=str(DEFAULT_REPO), help="repo checked out at the base commit")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="data source directory")
    args = ap.parse_args()

    ctx = PRContext(Path(args.diff).read_text(encoding="utf-8"), args.repo, args.data)
    script = "checks/" + Path(script_file).name
    rule = next((r for r in all_rules(ctx.rubric) if r.get("script") == script), {})
    try:
        ctx.validate()
        res = run(ctx, rule.get("params", {}))
    except (Unknown, PatchError) as exc:
        res = CheckResult(None, str(exc))
    status = {True: "fired", False: "clear", None: "unknown"}[res.fired]
    print(json.dumps({"id": rule.get("id"), "status": status, "detail": res.detail,
                      "evidence": res.evidence}, indent=2))


def all_rules(rubric: dict) -> list:
    return rubric["gates"] + rubric["forced_review"]["triggers"] + rubric["scoring"]["checks"]
